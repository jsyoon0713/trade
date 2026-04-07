"""
단타 트레이더 v2 — 거래량 폭발 모멘텀 전략

핵심 철학:
  - 장 시작과 동시에 거래량이 폭발하는 종목을 포착, 추세에 올라타 반복 매매
  - 개별 종목 손절/익절 없음 — 모멘텀 소멸 시 매도
  - 당일 목표 수익률 달성을 최우선 목표로 매수/매도 반복
  - 적극도(적극/보통/소극)로 투자 공격성 조절

흐름:
  08:30  morning_prep — 목표·적극도 입력받고 후보 종목 준비
  09:01~ run (매 1분) — 거래량 폭발 포착 → 진입 → 모멘텀 약화 시 청산 → 재진입 반복
  15:20  force_close_all — 미청산 포지션 전량 강제청산
"""
import logging
import select
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..broker.ls_broker import LSBroker
from ..broker.models import Order, Position
from ..monitor.portfolio import PortfolioMonitor
from ..notification.telegram_notifier import TelegramNotifier
from ..scanner.stock_scanner import StockScanner
from ..strategy.volume_momentum_strategy import VolumeMomentumStrategy

logger = logging.getLogger(__name__)

_PROMPT_TIMEOUT_SECS = 60   # 아침 입력 대기 시간


# ── 적극도 정의 ────────────────────────────────────────────────────────────────

class AggressivenessLevel(str, Enum):
    AGGRESSIVE   = "적극"
    NORMAL       = "보통"
    CONSERVATIVE = "소극"


# 적극도별 파라미터
_AGGRESSIVENESS_CONFIG: dict[AggressivenessLevel, dict] = {
    AggressivenessLevel.AGGRESSIVE: {
        "volume_spike_ratio":  2.0,   # 낮은 진입 기준 → 더 자주 진입
        "exit_volume_ratio":   1.2,   # 거래량 조금 감소해도 청산
        "momentum_candles":    2,
        "momentum_threshold":  0.003, # 0.3% 상승 시 진입
        "consecutive_down":    2,     # 2봉 연속 하락 시 청산
        "rsi_overbought":      78.0,
        "capital_ratio":       0.50,  # 1회 투입 = 시드의 50%
        "max_positions":       2,
        "rescan_interval_min": 3,
    },
    AggressivenessLevel.NORMAL: {
        "volume_spike_ratio":  3.0,
        "exit_volume_ratio":   1.5,
        "momentum_candles":    3,
        "momentum_threshold":  0.005, # 0.5%
        "consecutive_down":    3,
        "rsi_overbought":      75.0,
        "capital_ratio":       0.30,
        "max_positions":       3,
        "rescan_interval_min": 5,
    },
    AggressivenessLevel.CONSERVATIVE: {
        "volume_spike_ratio":  5.0,
        "exit_volume_ratio":   2.0,
        "momentum_candles":    3,
        "momentum_threshold":  0.010, # 1.0%
        "consecutive_down":    4,
        "rsi_overbought":      72.0,
        "capital_ratio":       0.20,
        "max_positions":       4,
        "rescan_interval_min": 10,
    },
}


# ── 내부 포지션 ────────────────────────────────────────────────────────────────

@dataclass
class DayPosition:
    symbol:     str
    quantity:   int
    avg_price:  float
    entry_time: datetime = field(default_factory=datetime.now)


# ── 메인 클래스 ────────────────────────────────────────────────────────────────

class DayTrader:
    def __init__(
        self,
        broker: LSBroker,
        portfolio_monitor: PortfolioMonitor,
        notifier: TelegramNotifier,
        scan_top_n: int = 20,
        capital: int = 2_000_000,
        daily_target_pct: float = 1.5,
        aggressiveness: str = "보통",
        candle_interval: str = "5",
        force_close_time: str = "15:20",
    ):
        self.broker           = broker
        self.portfolio_monitor = portfolio_monitor
        self.notifier         = notifier
        self.scanner          = StockScanner(broker)
        self.scan_top_n       = scan_top_n
        self.capital          = capital
        self.candle_interval  = candle_interval
        self.force_close_hour, self.force_close_min = map(int, force_close_time.split(":"))

        # 설정값 (morning_prep에서 변경 가능)
        self.daily_target_pct = daily_target_pct
        self.aggressiveness   = self._resolve_aggressiveness(aggressiveness)

        # 상태
        self._strategy: VolumeMomentumStrategy | None = None
        self._cfg: dict = {}
        self._watchlist:    list[str]              = []
        self._positions:    dict[str, DayPosition] = {}
        self._daily_realized_pnl: float            = 0.0
        self._last_rescan_time: datetime | None    = None
        self._trade_count: int                     = 0    # 당일 총 체결 횟수

        self._apply_aggressiveness()

    # ── 설정 적용 ──────────────────────────────────────────────────────────────

    def _resolve_aggressiveness(self, value: str) -> AggressivenessLevel:
        for lv in AggressivenessLevel:
            if lv.value == value or lv.name == value.upper():
                return lv
        return AggressivenessLevel.NORMAL

    def _apply_aggressiveness(self) -> None:
        self._cfg = _AGGRESSIVENESS_CONFIG[self.aggressiveness]
        self._strategy = VolumeMomentumStrategy(
            volume_spike_ratio=self._cfg["volume_spike_ratio"],
            exit_volume_ratio=self._cfg["exit_volume_ratio"],
            momentum_candles=self._cfg["momentum_candles"],
            momentum_threshold=self._cfg["momentum_threshold"],
            consecutive_down=self._cfg["consecutive_down"],
            rsi_overbought=self._cfg["rsi_overbought"],
        )
        logger.info(
            f"[단타] 적극도: {self.aggressiveness.value} | "
            f"거래량기준: {self._cfg['volume_spike_ratio']}x | "
            f"모멘텀: {self._cfg['momentum_threshold']*100:.1f}% | "
            f"1회투입: {self._cfg['capital_ratio']*100:.0f}% | "
            f"최대포지션: {self._cfg['max_positions']}"
        )

    # ── 목표 수익률 프로퍼티 ────────────────────────────────────────────────────

    @property
    def daily_target_amount(self) -> float:
        return self.capital * self.daily_target_pct / 100

    @property
    def target_achieved(self) -> bool:
        return self.daily_target_pct > 0 and self._daily_realized_pnl >= self.daily_target_amount

    def _pnl_progress_str(self) -> str:
        if self.daily_target_pct <= 0:
            return f"당일손익: {self._daily_realized_pnl:+,.0f}원"
        pct_done = self._daily_realized_pnl / self.daily_target_amount * 100 if self.daily_target_amount else 0
        return (
            f"당일손익: {self._daily_realized_pnl:+,.0f}원 / "
            f"목표: {self.daily_target_amount:,.0f}원 "
            f"({pct_done:.0f}%{'✓' if self.target_achieved else ''})"
        )

    # ── 08:30 아침 준비 ────────────────────────────────────────────────────────

    def morning_prep(self) -> None:
        """
        08:30 단타 준비
        1) 현재 설정 표시 + 목표 수익률·적극도 입력받기
        2) 거래량 상위 후보 스캔
        3) 워치리스트 설정
        """
        self._positions.clear()
        self._daily_realized_pnl = 0.0
        self._last_rescan_time   = None
        self._trade_count        = 0

        print("\n" + "=" * 60)
        print("[단타] 08:30 매매 준비")
        print("=" * 60)
        print(f"  현재 설정 → 목표: {self.daily_target_pct}% | 적극도: {self.aggressiveness.value}")
        print(f"  시드머니: {self.capital:,}원 | 1회투입: {self.capital * self._cfg['capital_ratio']:,.0f}원")
        print()

        # ── 목표 수익률 입력 ──────────────────────────────────────────────
        print(f"[1] 당일 목표 수익률 (%) [Enter = {self.daily_target_pct}% 유지]:")
        print(f"    예) 1.5  → {self.capital * 1.5 / 100:,.0f}원 목표")
        target_input = self._read_input("    입력: ", _PROMPT_TIMEOUT_SECS).strip()
        if target_input:
            try:
                new_target = float(target_input)
                if 0 < new_target <= 20:
                    self.daily_target_pct = new_target
                    print(f"    ✓ 목표 수익률: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)")
                else:
                    print(f"    (범위 초과 — 기존값 {self.daily_target_pct}% 유지)")
            except ValueError:
                print(f"    (잘못된 입력 — 기존값 {self.daily_target_pct}% 유지)")
        else:
            print(f"    ✓ 기존 유지: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)")

        # ── 적극도 선택 ───────────────────────────────────────────────────
        print()
        print(f"[2] 적극도 선택 [Enter = {self.aggressiveness.value} 유지]:")
        print("    [1] 적극 — 거래량 2x, 0.3% 모멘텀, 시드 50% 투입, 최대 2포지션")
        print("    [2] 보통 — 거래량 3x, 0.5% 모멘텀, 시드 30% 투입, 최대 3포지션")
        print("    [3] 소극 — 거래량 5x, 1.0% 모멘텀, 시드 20% 투입, 최대 4포지션")
        agg_input = self._read_input("    입력: ", _PROMPT_TIMEOUT_SECS).strip()
        mapping = {"1": AggressivenessLevel.AGGRESSIVE, "적극": AggressivenessLevel.AGGRESSIVE,
                   "2": AggressivenessLevel.NORMAL,     "보통": AggressivenessLevel.NORMAL,
                   "3": AggressivenessLevel.CONSERVATIVE,"소극": AggressivenessLevel.CONSERVATIVE}
        if agg_input in mapping:
            self.aggressiveness = mapping[agg_input]
            self._apply_aggressiveness()
            print(f"    ✓ 적극도: {self.aggressiveness.value}")
        else:
            print(f"    ✓ 기존 유지: {self.aggressiveness.value}")

        # ── 거래량 상위 스캔 ──────────────────────────────────────────────
        print()
        print(f"[단타] 거래량 상위 {self.scan_top_n}개 종목 스캔 중...")
        candidates = self.scanner.get_top_volume_stocks(self.scan_top_n)
        self._watchlist = candidates
        self._last_rescan_time = datetime.now()

        print(f"\n[단타] 초기 후보 종목 ({len(self._watchlist)}개):")
        for sym in self._watchlist:
            print(f"  {sym}")

        # ── 요약 알림 ─────────────────────────────────────────────────────
        print()
        print("=" * 60)
        print(f"[단타] 준비 완료 — 09:01 장 시작 시 자동 매매 시작")
        print(f"  목표: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)")
        print(f"  적극도: {self.aggressiveness.value} | 후보: {len(self._watchlist)}개")
        print("=" * 60)
        print()

        self.notifier.notify_portfolio(
            f"[단타] 준비 완료\n"
            f"목표: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)\n"
            f"적극도: {self.aggressiveness.value}\n"
            f"후보 종목: {len(self._watchlist)}개"
        )

    # ── 장중 실행 (매 1분) ────────────────────────────────────────────────────

    def run(self) -> None:
        """
        매 1분 실행:
        1) 목표 달성 체크 → 신규 매수 중단
        2) 워치리스트 재스캔 (N분마다)
        3) 보유 종목 모멘텀 약화 체크 → 청산
        4) 신규 진입 체크
        """
        if not self._watchlist and not self._positions:
            logger.debug("[단타] 워치리스트 없음 — 스킵")
            return

        deployed = self._deployed_capital()
        avail    = self.capital - deployed
        logger.info(
            f"[단타] {self._pnl_progress_str()} | "
            f"투입: {deployed:,}원 | 가용: {avail:,}원 | "
            f"포지션: {len(self._positions)} | 거래: {self._trade_count}회"
        )

        try:
            # 목표 달성 시 보유 종목 리스크 체크만 계속
            if self.target_achieved:
                logger.info("[단타] 목표 달성 — 신규 매수 중단, 보유 종목 모니터링만 계속")
                self._check_exits()
                return

            # 워치리스트 재스캔
            self._maybe_rescan()

            # 보유 종목 모멘텀 약화 체크
            self._check_exits()

            # 신규 진입 체크
            if len(self._positions) < self._cfg["max_positions"]:
                self._check_entries()

        except Exception as e:
            logger.exception(f"[단타] 실행 오류: {e}")
            self.notifier.notify_error(f"[단타] {e}")

    # ── 재스캔 ────────────────────────────────────────────────────────────────

    def _maybe_rescan(self) -> None:
        """N분마다 거래량 상위 재스캔 — 더 강한 종목으로 워치리스트 교체"""
        if self._last_rescan_time is None:
            self._last_rescan_time = datetime.now()
            return

        elapsed = (datetime.now() - self._last_rescan_time).total_seconds() / 60
        if elapsed < self._cfg["rescan_interval_min"]:
            return

        logger.info(f"[단타] 거래량 재스캔 ({self._cfg['rescan_interval_min']}분 주기)")
        try:
            fresh = self.scanner.get_top_volume_stocks(self.scan_top_n)
            held  = set(self._positions.keys())

            # 보유 중인 종목은 유지, 새 종목 추가
            new_list = list(held) + [s for s in fresh if s not in held]
            added   = [s for s in fresh if s not in self._watchlist]
            removed = [s for s in self._watchlist if s not in new_list and s not in held]

            self._watchlist        = new_list
            self._last_rescan_time = datetime.now()

            if added or removed:
                logger.info(f"[단타] 워치리스트 갱신 | 추가: {added} | 제거: {removed}")
        except Exception as e:
            logger.warning(f"[단타] 재스캔 실패: {e}")

    # ── 모멘텀 약화 → 청산 ───────────────────────────────────────────────────

    def _check_exits(self) -> None:
        for symbol, pos in list(self._positions.items()):
            try:
                ohlcv = self.broker.get_ohlcv(symbol, period=self.candle_interval, count=30)
                if not ohlcv:
                    continue

                result = self._strategy.check_exit(symbol, ohlcv, entry_price=pos.avg_price)

                if result.signal.value == "sell":
                    self._execute_sell(symbol, pos, result.reason)

            except Exception as e:
                logger.warning(f"[단타][{symbol}] 청산 체크 오류: {e}")

    # ── 거래량 폭발 → 진입 ───────────────────────────────────────────────────

    def _check_entries(self) -> None:
        try:
            balance = self.portfolio_monitor.get_balance()
        except Exception as e:
            logger.warning(f"[단타] 잔고 조회 실패: {e}")
            return

        for symbol in self._watchlist:
            if len(self._positions) >= self._cfg["max_positions"]:
                break
            if symbol in self._positions:
                continue

            try:
                ohlcv = self.broker.get_ohlcv(symbol, period=self.candle_interval, count=30)
                if not ohlcv:
                    continue

                result = self._strategy.generate_signal(symbol, ohlcv)
                ind    = result.indicator_values

                logger.info(
                    f"[단타][{symbol}] "
                    f"vol={ind.get('vol_ratio','?')}x "
                    f"momentum={ind.get('momentum_pct','?')}% "
                    f"RSI={ind.get('rsi','?')} "
                    f"→ {result.signal.value}"
                )

                if result.signal.value == "buy":
                    order_amount = self._calc_order_amount()
                    avail        = self.capital - self._deployed_capital()

                    if avail < order_amount:
                        logger.info(f"[단타] 가용 자금 부족 ({avail:,} < {order_amount:,}) — 진입 대기")
                        break

                    current_price = ohlcv[-1]["close"]
                    qty           = int(order_amount / current_price)
                    if qty <= 0 or balance.cash < order_amount:
                        continue

                    order = self.broker.buy_market(symbol, qty, memo=result.reason)
                    self.notifier.notify_order(order)
                    self._positions[symbol] = DayPosition(
                        symbol=symbol,
                        quantity=qty,
                        avg_price=current_price,
                    )
                    self._trade_count += 1
                    logger.info(
                        f"[단타][{symbol}] 진입 {qty}주 @ {current_price:,}원 "
                        f"(투입 {order_amount:,}원) | {result.reason}"
                    )

            except Exception as e:
                logger.warning(f"[단타][{symbol}] 진입 체크 오류: {e}")

    # ── 청산 실행 ────────────────────────────────────────────────────────────

    def _execute_sell(self, symbol: str, pos: DayPosition, reason: str) -> None:
        try:
            current_price = self.broker.get_price(symbol)
            pnl           = (current_price - pos.avg_price) * pos.quantity
            self._daily_realized_pnl += pnl
            self._trade_count += 1

            order = self.broker.sell_market(symbol, pos.quantity, memo=reason)
            self.notifier.notify_order(order)
            del self._positions[symbol]

            pnl_pct = (current_price - pos.avg_price) / pos.avg_price * 100
            logger.info(
                f"[단타][{symbol}] 청산 {pos.quantity}주 | "
                f"손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%) | {self._pnl_progress_str()}"
            )

            # 목표 달성 알림
            if self.target_achieved:
                msg = (
                    f"[단타] 당일 목표 달성!\n"
                    f"실현손익: {self._daily_realized_pnl:+,.0f}원\n"
                    f"목표: {self.daily_target_amount:,.0f}원\n"
                    f"거래횟수: {self._trade_count}회"
                )
                logger.info(msg)
                self.notifier.notify_portfolio(msg)

        except Exception as e:
            logger.error(f"[단타][{symbol}] 청산 실패: {e}")
            self.notifier.notify_error(f"[단타] {symbol} 청산 실패: {e}")

    # ── 15:20 강제청산 ────────────────────────────────────────────────────────

    def force_close_all(self) -> None:
        logger.info("[단타] 15:20 강제청산 시작")
        if not self._positions:
            logger.info("[단타] 강제청산할 포지션 없음")
            return

        closed: list[str] = []
        for symbol, pos in list(self._positions.items()):
            try:
                current_price = self.broker.get_price(symbol)
                pnl           = (current_price - pos.avg_price) * pos.quantity
                self._daily_realized_pnl += pnl
                self._trade_count += 1

                order = self.broker.sell_market(symbol, pos.quantity, memo="[단타] 장 마감 강제청산")
                self.notifier.notify_order(order)
                closed.append(symbol)
                logger.info(f"[단타][{symbol}] 강제청산 {pos.quantity}주")
            except Exception as e:
                logger.error(f"[단타][{symbol}] 강제청산 실패: {e}")
                self.notifier.notify_error(f"[단타] {symbol} 강제청산 실패: {e}")

        for sym in closed:
            self._positions.pop(sym, None)

        self._watchlist.clear()

        msg = (
            f"[단타] 강제청산 완료: {', '.join(closed)}\n"
            f"{self._pnl_progress_str()}\n"
            f"총 거래횟수: {self._trade_count}회"
        ) if closed else "[단타] 강제청산 완료 (없음)"
        logger.info(msg)
        self.notifier.notify_portfolio(msg)

    # ── 목표 수익률 실시간 변경 ───────────────────────────────────────────────

    def update_daily_target(self, new_target_pct: float) -> str:
        """
        운영 중 목표 수익률 변경.
        web.py 또는 CLI에서 호출 가능.
        """
        if new_target_pct <= 0 or new_target_pct > 20:
            return f"유효하지 않은 목표값: {new_target_pct}% (0 < 값 ≤ 20)"
        old = self.daily_target_pct
        self.daily_target_pct = new_target_pct
        msg = f"[단타] 목표 수익률 변경: {old}% → {new_target_pct}% ({self.daily_target_amount:,.0f}원)"
        logger.info(msg)
        self.notifier.notify_portfolio(msg)
        return msg

    def update_aggressiveness(self, level: str) -> str:
        """
        운영 중 적극도 변경.
        web.py 또는 CLI에서 호출 가능.
        """
        try:
            new_level = self._resolve_aggressiveness(level)
        except Exception:
            return f"유효하지 않은 적극도: {level}"
        old = self.aggressiveness.value
        self.aggressiveness = new_level
        self._apply_aggressiveness()
        msg = f"[단타] 적극도 변경: {old} → {self.aggressiveness.value}"
        logger.info(msg)
        self.notifier.notify_portfolio(msg)
        return msg

    # ── 내부 유틸 ────────────────────────────────────────────────────────────

    def _calc_order_amount(self) -> int:
        """1회 투입 금액 = 시드 × capital_ratio"""
        return int(self.capital * self._cfg["capital_ratio"])

    def _deployed_capital(self) -> int:
        return sum(int(p.avg_price * p.quantity) for p in self._positions.values())

    @staticmethod
    def _read_input(prompt: str, timeout_secs: int) -> str:
        """타임아웃 있는 stdin 읽기 (Windows에서는 빈 문자열 반환)"""
        if sys.platform == "win32":
            return ""
        print(prompt, end="", flush=True)
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout_secs)
            if ready:
                return sys.stdin.readline()
        except Exception:
            pass
        print(f"\n[단타] {timeout_secs}초 초과 → 자동 진행")
        return ""

    def get_positions_summary(self) -> str:
        """단타 포지션 현황 문자열 반환"""
        if not self._positions:
            return "[단타] 보유 포지션 없음"
        lines = ["[단타] 현재 포지션:"]
        for sym, pos in self._positions.items():
            try:
                price   = self.broker.get_price(sym)
                pct     = (price - pos.avg_price) / pos.avg_price * 100
                lines.append(
                    f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f} | "
                    f"현재 {price:,.0f} ({pct:+.2f}%)"
                )
            except Exception:
                lines.append(f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f}")
        lines.append(f"  {self._pnl_progress_str()}")
        lines.append(f"  총 거래횟수: {self._trade_count}회")
        return "\n".join(lines)
