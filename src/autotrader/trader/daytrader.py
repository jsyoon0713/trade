"""
단타 트레이더 v4 — 주도주 갭업 첫 눌림 전략

전략 개요:
  [필터1] 일봉 추세 : EMA(5) > EMA(20) → 기관이 지속 매수 중인 주도주만 선별
  [필터2] 갭업 확인 : 오늘 시초가 > 어제 종가 × 0.5%+ → 뉴스/실적 촉매 종목
  [진입]  VWAP 눌림 반등 : 갭업 후 첫 번째 기관 재매수 시점
  [손절]  진입가 - 1.0%
  [익절]  진입가 + 2.0%  → 손익비 1:2, 필요 승률 40%

차별화 포인트 (일반 VWAP봇 대비):
  - 순수 VWAP봇은 아무 종목이나 VWAP 반응 시 진입 (신호 과다, 노이즈 많음)
  - 이 전략은 일봉 추세(기관 수급) + 갭업(촉매) + VWAP 타이밍 3중 확인
  - 진입 빈도는 적지만 성공률이 높음

흐름:
  08:30  morning_prep — 목표·적극도 입력 + 후보 종목 준비
  09:00~ run (매 30초) — 갭업·추세 필터 → VWAP 눌림 진입 → 익절/손절/VWAP이탈 청산
  15:20  force_close_all — 강제청산
"""
import logging
import select
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from ..broker.ls_broker import LSBroker
from ..monitor.portfolio import PortfolioMonitor
from ..notification.telegram_notifier import TelegramNotifier
from ..scanner.stock_scanner import StockScanner
from ..strategy.vwap_pullback_strategy import VWAPPullbackStrategy

logger = logging.getLogger(__name__)

_PROMPT_TIMEOUT_SECS     = 60
_OHLCV_CACHE_TTL_SECS    = 30    # 분봉 OHLCV 캐시 유효 시간 (초)
_DAILY_CACHE_TTL_SECS    = 600   # 일봉 OHLCV 캐시 유효 시간 (10분)
_MARKET_OPEN_SKIP_MINS   = 10    # 개장 후 N분간 신규 진입 차단 (09:00~09:10)
_GAP_UP_MIN_PCT          = 0.005 # 갭업 최소 기준: 전일 종가 대비 0.5%+


# ── 적극도 정의 ────────────────────────────────────────────────────────────────

class AggressivenessLevel(str, Enum):
    AGGRESSIVE   = "적극"
    NORMAL       = "보통"
    CONSERVATIVE = "소극"


_AGGRESSIVENESS_CONFIG: dict[AggressivenessLevel, dict] = {
    # VWAP 눌림 반등 전략 파라미터
    # 손익비 1:2 → 필요 승률 40% (거래비용 포함 시 ≈ 45%)
    AggressivenessLevel.AGGRESSIVE: {
        # VWAP 전략 파라미터
        "vwap_touch_pct":       0.008,   # VWAP ±0.8% 이내 눌림 인정 (넓게)
        "max_above_vwap_pct":   0.030,   # VWAP 대비 3% 이내 진입 허용
        "vol_confirm_ratio":    1.2,     # 반등 봉 거래량 1.2x 이상
        "vwap_break_pct":       0.002,   # VWAP 아래 0.2% 이탈 시 청산
        # 리스크 관리
        "hard_stop_pct":        1.0,     # -1.0% 손절
        "take_profit_pct":      2.5,     # +2.5% 익절  (손익비 1:2.5)
        "min_hold_minutes":     3,
        "cooldown_minutes":     5,
        "capital_ratio":        0.50,
        "max_positions":        2,
        "rescan_interval_min":  3,
    },
    AggressivenessLevel.NORMAL: {
        # VWAP 전략 파라미터
        "vwap_touch_pct":       0.005,   # VWAP ±0.5% 이내 눌림
        "max_above_vwap_pct":   0.025,   # VWAP 대비 2.5% 이내 진입
        "vol_confirm_ratio":    1.3,     # 반등 봉 거래량 1.3x 이상
        "vwap_break_pct":       0.003,   # VWAP 아래 0.3% 이탈 시 청산
        # 리스크 관리
        "hard_stop_pct":        1.0,     # -1.0% 손절 (손익비 1:2)
        "take_profit_pct":      2.0,     # +2.0% 익절
        "min_hold_minutes":     5,
        "cooldown_minutes":     10,
        "capital_ratio":        0.30,
        "max_positions":        3,
        "rescan_interval_min":  5,
    },
    AggressivenessLevel.CONSERVATIVE: {
        # VWAP 전략 파라미터
        "vwap_touch_pct":       0.003,   # VWAP ±0.3% 이내만 눌림 인정 (엄격)
        "max_above_vwap_pct":   0.015,   # VWAP 대비 1.5% 이내만 진입
        "vol_confirm_ratio":    1.5,     # 반등 봉 거래량 1.5x 이상
        "vwap_break_pct":       0.003,
        # 리스크 관리
        "hard_stop_pct":        0.8,     # -0.8% 손절 (손익비 1:2.25)
        "take_profit_pct":      1.8,     # +1.8% 익절
        "min_hold_minutes":     10,
        "cooldown_minutes":     15,
        "capital_ratio":        0.20,
        "max_positions":        4,
        "rescan_interval_min":  10,
    },
}


# ── 내부 포지션 ────────────────────────────────────────────────────────────────

@dataclass
class DayPosition:
    symbol:     str
    quantity:   int
    avg_price:  float
    entry_time: datetime = field(default_factory=datetime.now)

    def held_minutes(self) -> float:
        return (datetime.now() - self.entry_time).total_seconds() / 60


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
        candle_interval: str = "1",
        force_close_time: str = "15:20",
    ):
        self.broker            = broker
        self.portfolio_monitor = portfolio_monitor
        self.notifier          = notifier
        self.scanner           = StockScanner(broker)
        self.scan_top_n        = scan_top_n
        self.capital           = capital
        self.candle_interval   = candle_interval
        self.force_close_hour, self.force_close_min = map(int, force_close_time.split(":"))

        self.daily_target_pct = daily_target_pct
        self.aggressiveness   = self._resolve_aggressiveness(aggressiveness)

        # 전략 / 설정
        self._strategy: VWAPPullbackStrategy | None = None
        self._cfg: dict = {}
        self._apply_aggressiveness()

        # 매매 상태
        self._watchlist:    list[str]               = []
        self._positions:    dict[str, DayPosition]  = {}
        self._cooldown:     dict[str, datetime]     = {}   # 청산 후 재진입 금지
        self._ohlcv_cache:  dict[str, tuple[datetime, list]] = {}  # 분봉 API 캐시 (60초)
        self._daily_cache:  dict[str, tuple[datetime, list]] = {}  # 일봉 API 캐시 (10분)
        self._daily_realized_pnl: float             = 0.0
        self._last_rescan_time: datetime | None     = None
        self._trade_count: int                      = 0

    # ── 설정 적용 ──────────────────────────────────────────────────────────────

    def _resolve_aggressiveness(self, value: str) -> AggressivenessLevel:
        for lv in AggressivenessLevel:
            if lv.value == value or lv.name == value.upper():
                return lv
        return AggressivenessLevel.NORMAL

    def _apply_aggressiveness(self) -> None:
        self._cfg = _AGGRESSIVENESS_CONFIG[self.aggressiveness]
        self._strategy = VWAPPullbackStrategy(
            vwap_touch_pct      = self._cfg["vwap_touch_pct"],
            max_above_vwap_pct  = self._cfg["max_above_vwap_pct"],
            vol_confirm_ratio   = self._cfg["vol_confirm_ratio"],
            vwap_break_pct      = self._cfg["vwap_break_pct"],
        )
        logger.info(
            f"[단타] 적극도={self.aggressiveness.value} | "
            f"VWAP눌림={self._cfg['vwap_touch_pct']*100:.1f}% | "
            f"손절=-{self._cfg['hard_stop_pct']}% | "
            f"익절=+{self._cfg['take_profit_pct']}% | "
            f"최소보유={self._cfg['min_hold_minutes']}분"
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

    # ── 장중 수동 즉시 시작 (웹 대시보드용) ───────────────────────────────────

    def quick_start(
        self,
        daily_target_pct: float | None = None,
        aggressiveness: str | None = None,
    ) -> dict:
        """
        대화형 입력 없이 즉시 단타 시작.
        웹 대시보드에서 장중 수동 시작 시 사용.
        """
        self._positions.clear()
        self._cooldown.clear()
        self._ohlcv_cache.clear()
        self._daily_realized_pnl = 0.0
        self._last_rescan_time   = None
        self._trade_count        = 0

        if daily_target_pct is not None and 0 < daily_target_pct <= 20:
            self.daily_target_pct = daily_target_pct

        if aggressiveness:
            new_lv = self._resolve_aggressiveness(aggressiveness)
            if new_lv != self.aggressiveness:
                self.aggressiveness = new_lv
                self._apply_aggressiveness()

        logger.info(
            f"[단타] 수동 시작 — 목표: {self.daily_target_pct}% "
            f"({self.daily_target_amount:,.0f}원) | 적극도: {self.aggressiveness.value}"
        )

        # 거래량 상위 스캔
        logger.info(f"[단타] 거래량 상위 {self.scan_top_n}개 스캔 중...")
        try:
            candidates = self.scanner.get_top_volume_stocks(self.scan_top_n)
            self._watchlist        = candidates
            self._last_rescan_time = datetime.now()
            logger.info(f"[단타] 워치리스트 설정 완료: {self._watchlist}")
        except Exception as e:
            logger.error(f"[단타] 스캔 실패: {e}")
            return {"ok": False, "msg": f"스캔 실패: {e}"}

        self.notifier.notify_portfolio(
            f"[단타] 수동 시작\n"
            f"목표: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)\n"
            f"적극도: {self.aggressiveness.value} | 후보: {len(self._watchlist)}개"
        )

        # 즉시 첫 번째 매매 판단 실행 (스케줄러 다음 분 틱을 기다리지 않음)
        logger.info("[단타] 즉시 첫 번째 매매 판단 시작")
        self.run()

        return {
            "ok": True,
            "msg": f"단타 시작 완료 — 목표 {self.daily_target_pct}%, 적극도 {self.aggressiveness.value}",
            "watchlist": self._watchlist,
            "daily_target_pct": self.daily_target_pct,
            "daily_target_amount": self.daily_target_amount,
            "aggressiveness": self.aggressiveness.value,
        }

    # ── 08:30 아침 준비 ────────────────────────────────────────────────────────

    def morning_prep(self, **kwargs) -> None:
        """08:30 대화형 준비 (macro_score 등 추가 인자는 무시)"""
        self._positions.clear()
        self._cooldown.clear()
        self._ohlcv_cache.clear()
        self._daily_realized_pnl = 0.0
        self._last_rescan_time   = None
        self._trade_count        = 0

        print("\n" + "=" * 60)
        print("[단타] 08:30 매매 준비")
        print("=" * 60)
        print(f"  현재 설정 → 목표: {self.daily_target_pct}% | 적극도: {self.aggressiveness.value}")
        print(f"  시드머니: {self.capital:,}원 | 1회투입: {self.capital * self._cfg['capital_ratio']:,.0f}원")
        print(f"  하드스탑: -{self._cfg['hard_stop_pct']}% | 최소보유: {self._cfg['min_hold_minutes']}분")
        print()

        # 목표 수익률 입력
        print(f"[1] 당일 목표 수익률 (%) [Enter = {self.daily_target_pct}% 유지]:")
        target_input = self._read_input("    입력: ", _PROMPT_TIMEOUT_SECS).strip()
        if target_input:
            try:
                new_target = float(target_input)
                if 0 < new_target <= 20:
                    self.daily_target_pct = new_target
                    print(f"    ✓ 목표: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)")
                else:
                    print(f"    (범위 초과 — 기존값 유지)")
            except ValueError:
                print(f"    (잘못된 입력 — 기존값 유지)")
        else:
            print(f"    ✓ 기존 유지: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)")

        # 적극도 선택
        print()
        print(f"[2] 적극도 선택 [Enter = {self.aggressiveness.value} 유지]:")
        print("    [1] 적극 — 거래량 2x, 하드스탑 3%, 최소보유 5분")
        print("    [2] 보통 — 거래량 3x, 하드스탑 2.5%, 최소보유 10분")
        print("    [3] 소극 — 거래량 5x, 하드스탑 2%, 최소보유 15분")
        agg_input = self._read_input("    입력: ", _PROMPT_TIMEOUT_SECS).strip()
        mapping = {
            "1": AggressivenessLevel.AGGRESSIVE,  "적극": AggressivenessLevel.AGGRESSIVE,
            "2": AggressivenessLevel.NORMAL,       "보통": AggressivenessLevel.NORMAL,
            "3": AggressivenessLevel.CONSERVATIVE, "소극": AggressivenessLevel.CONSERVATIVE,
        }
        if agg_input in mapping:
            self.aggressiveness = mapping[agg_input]
            self._apply_aggressiveness()
            print(f"    ✓ 적극도: {self.aggressiveness.value}")
        else:
            print(f"    ✓ 기존 유지: {self.aggressiveness.value}")

        # 거래량 상위 스캔
        print()
        print(f"[단타] 거래량 상위 {self.scan_top_n}개 종목 스캔 중...")
        candidates = self.scanner.get_top_volume_stocks(self.scan_top_n)
        self._watchlist = candidates
        self._last_rescan_time = datetime.now()

        print(f"\n[단타] 초기 후보 종목 ({len(self._watchlist)}개): {self._watchlist}")

        print()
        print("=" * 60)
        print(f"[단타] 준비 완료 — 09:{_MARKET_OPEN_SKIP_MINS:02d} 이후 자동 매매 시작")
        print(f"  목표: {self.daily_target_pct}% | 적극도: {self.aggressiveness.value}")
        print(f"  하드스탑: -{self._cfg['hard_stop_pct']}% | 쿨다운: {self._cfg['cooldown_minutes']}분")
        print("=" * 60)
        print()

        self.notifier.notify_portfolio(
            f"[단타] 준비 완료\n"
            f"목표: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)\n"
            f"적극도: {self.aggressiveness.value} | 후보: {len(self._watchlist)}개\n"
            f"하드스탑: -{self._cfg['hard_stop_pct']}%"
        )

    # ── 장중 실행 (매 1분) ────────────────────────────────────────────────────

    def run(self) -> None:
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
            if self.target_achieved:
                logger.info("[단타] 목표 달성 — 신규 매수 중단, 보유 종목 모니터링만 계속")
                self._check_exits()
                return

            self._maybe_rescan()
            self._check_exits()

            if len(self._positions) < self._cfg["max_positions"]:
                if self._is_market_open_rush():
                    logger.info(f"[단타] 개장 직후 {_MARKET_OPEN_SKIP_MINS}분 진입 차단 중")
                else:
                    self._check_entries()

        except Exception as e:
            logger.exception(f"[단타] 실행 오류: {e}")
            self.notifier.notify_error(f"[단타] {e}")

    # ── 개장 직후 체크 ────────────────────────────────────────────────────────

    def _is_market_open_rush(self) -> bool:
        """09:00 ~ 09:N 진입 차단"""
        now = datetime.now()
        return now.hour == 9 and now.minute < _MARKET_OPEN_SKIP_MINS

    # ── 쿨다운 체크 ───────────────────────────────────────────────────────────

    def _is_in_cooldown(self, symbol: str) -> bool:
        if symbol not in self._cooldown:
            return False
        elapsed = (datetime.now() - self._cooldown[symbol]).total_seconds() / 60
        return elapsed < self._cfg["cooldown_minutes"]

    # ── 재스캔 ────────────────────────────────────────────────────────────────

    def _maybe_rescan(self) -> None:
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
            new_list = list(held) + [s for s in fresh if s not in held]
            added   = [s for s in fresh if s not in self._watchlist]
            removed = [s for s in self._watchlist if s not in new_list and s not in held]
            self._watchlist        = new_list
            self._last_rescan_time = datetime.now()
            if added or removed:
                logger.info(f"[단타] 워치리스트 갱신 | 추가: {added} | 제거: {removed}")
        except Exception as e:
            logger.warning(f"[단타] 재스캔 실패: {e}")

    # ── 청산 체크 ────────────────────────────────────────────────────────────

    def _check_exits(self) -> None:
        for symbol, pos in list(self._positions.items()):
            try:
                ohlcv = self._get_ohlcv(symbol)
                if not ohlcv:
                    continue

                current_price = ohlcv[-1]["close"]
                pnl_pct = (current_price - pos.avg_price) / pos.avg_price * 100

                # ① 하드 스탑 (최소 보유 시간 무관하게 즉시 청산)
                if pnl_pct <= -self._cfg["hard_stop_pct"]:
                    reason = (
                        f"하드 스탑 {pnl_pct:.2f}% ≤ -{self._cfg['hard_stop_pct']}%"
                    )
                    logger.warning(f"[단타][{symbol}] {reason}")
                    self._execute_sell(symbol, pos, reason)
                    continue

                # ① 익절 (최소 보유 시간 무관하게 즉시 청산)
                take_profit = self._cfg.get("take_profit_pct", 0)
                if take_profit > 0 and pnl_pct >= take_profit:
                    reason = f"익절 {pnl_pct:.2f}% ≥ +{take_profit}%"
                    logger.info(f"[단타][{symbol}] {reason}")
                    self._execute_sell(symbol, pos, reason)
                    continue

                # ② 최소 보유 시간 미충족 → 모멘텀 청산 조건 무시
                if pos.held_minutes() < self._cfg["min_hold_minutes"]:
                    logger.debug(
                        f"[단타][{symbol}] 최소보유 미충족 "
                        f"({pos.held_minutes():.1f}분 < {self._cfg['min_hold_minutes']}분) — 유지"
                    )
                    continue

                # ③ 모멘텀 약화 청산
                result = self._strategy.check_exit(symbol, ohlcv, entry_price=pos.avg_price)
                if result.signal.value == "sell":
                    self._execute_sell(symbol, pos, result.reason)

            except Exception as e:
                logger.warning(f"[단타][{symbol}] 청산 체크 오류: {e}")

    # ── 진입 체크 ────────────────────────────────────────────────────────────

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

            # 쿨다운 체크
            if self._is_in_cooldown(symbol):
                remaining = self._cfg["cooldown_minutes"] - (
                    datetime.now() - self._cooldown[symbol]
                ).total_seconds() / 60
                logger.debug(f"[단타][{symbol}] 쿨다운 중 ({remaining:.0f}분 남음)")
                continue

            try:
                ohlcv = self._get_ohlcv(symbol)
                if not ohlcv:
                    logger.warning(f"[단타][{symbol}] 분봉 데이터 없음 (ETF 또는 API 오류) — 스킵")
                    continue

                # ── [필터1] 일봉 EMA 추세 확인 (기관 보유 주도주) ──────────
                if not self._is_daily_trend_bullish(symbol):
                    logger.debug(f"[단타][{symbol}] 일봉 하락추세(EMA5<EMA20) — 스킵")
                    continue

                # ── [필터2] 갭업 확인 (촉매 있는 종목) ────────────────────
                gap_pct = self._get_gap_pct(symbol, ohlcv)
                if gap_pct is not None and gap_pct < _GAP_UP_MIN_PCT:
                    logger.debug(f"[단타][{symbol}] 갭업 미달({gap_pct*100:.2f}% < {_GAP_UP_MIN_PCT*100:.1f}%) — 스킵")
                    continue

                result = self._strategy.generate_signal(symbol, ohlcv)
                ind    = result.indicator_values

                gap_str = f" gap={gap_pct*100:+.1f}%" if gap_pct is not None else ""
                logger.info(
                    f"[단타][{symbol}]{gap_str} "
                    f"VWAP={ind.get('vwap','?')} "
                    f"이격={ind.get('above_vwap','?')}% "
                    f"vol={ind.get('vol_ratio','?')}x "
                    f"→ {result.signal.value} ({result.reason})"
                )

                if result.signal.value == "buy":
                    order_amount = self._calc_order_amount()
                    avail        = self.capital - self._deployed_capital()

                    if avail < order_amount:
                        logger.info(f"[단타] 가용 자금 부족 ({avail:,} < {order_amount:,})")
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
            self._trade_count        += 1

            order = self.broker.sell_market(symbol, pos.quantity, memo=reason)
            self.notifier.notify_order(order)
            del self._positions[symbol]

            # 쿨다운 등록
            self._cooldown[symbol] = datetime.now()
            # 캐시 무효화
            self._ohlcv_cache.pop(symbol, None)

            pnl_pct = (current_price - pos.avg_price) / pos.avg_price * 100
            logger.info(
                f"[단타][{symbol}] 청산 {pos.quantity}주 | "
                f"손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%) | {self._pnl_progress_str()}"
            )

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
                self._trade_count        += 1

                order = self.broker.sell_market(symbol, pos.quantity, memo="[단타] 장 마감 강제청산")
                self.notifier.notify_order(order)
                closed.append(symbol)
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

    # ── 목표·적극도 실시간 변경 ───────────────────────────────────────────────

    def update_daily_target(self, new_target_pct: float) -> str:
        if new_target_pct <= 0 or new_target_pct > 20:
            return f"유효하지 않은 목표값: {new_target_pct}%"
        old = self.daily_target_pct
        self.daily_target_pct = new_target_pct
        msg = f"[단타] 목표 변경: {old}% → {new_target_pct}% ({self.daily_target_amount:,.0f}원)"
        logger.info(msg)
        self.notifier.notify_portfolio(msg)
        return msg

    def update_aggressiveness(self, level: str) -> str:
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

    def _get_ohlcv(self, symbol: str) -> list[dict]:
        """OHLCV 캐시 (30초) — API 과다 호출 방지
        count=400: 1분봉×400 = 400분 → 당일 시초가(9:00)부터 현재까지 커버
        → strategy의 opens[0]이 실제 당일 시초가 근사값이 됨
        """
        now = datetime.now()
        if symbol in self._ohlcv_cache:
            cached_time, cached_data = self._ohlcv_cache[symbol]
            if (now - cached_time).total_seconds() < _OHLCV_CACHE_TTL_SECS:
                return cached_data
        data = self.broker.get_ohlcv(symbol, period=self.candle_interval, count=400)
        if not data:
            logger.debug(f"[단타][{symbol}] OHLCV 빈 데이터 — 스킵 (ETF 또는 API 오류)")
        self._ohlcv_cache[symbol] = (now, data)
        return data

    def _get_daily_ohlcv(self, symbol: str) -> list[dict]:
        """일봉 OHLCV 캐시 (10분) — EMA 계산용, API 호출 최소화"""
        now = datetime.now()
        if symbol in self._daily_cache:
            cached_time, cached_data = self._daily_cache[symbol]
            if (now - cached_time).total_seconds() < _DAILY_CACHE_TTL_SECS:
                return cached_data
        try:
            data = self.broker.get_ohlcv(symbol, period="D", count=30)
        except Exception as e:
            logger.debug(f"[단타][{symbol}] 일봉 조회 실패: {e}")
            data = []
        self._daily_cache[symbol] = (now, data)
        return data

    def _is_daily_trend_bullish(self, symbol: str) -> bool:
        """일봉 EMA(5) > EMA(20) 여부 — 기관 지속 매수 중인 주도주 필터
        데이터 부족(상장 초기 등) 시 True 반환(필터 통과)
        """
        daily = self._get_daily_ohlcv(symbol)
        if len(daily) < 20:
            return True  # 데이터 부족 → 통과
        closes = [c["close"] for c in daily]
        ema5  = self._calc_ema(closes, 5)
        ema20 = self._calc_ema(closes, 20)
        return ema5 > ema20

    def _get_gap_pct(self, symbol: str, ohlcv: list[dict]) -> float | None:
        """오늘 갭업률 계산: (오늘 시초가 - 어제 종가) / 어제 종가
        계산 불가 시 None 반환 (필터 통과)
        """
        today_str = datetime.now().strftime("%Y%m%d")
        today_candles = [c for c in ohlcv if str(c.get("date", "")).startswith(today_str)]
        other_candles = [c for c in ohlcv if not str(c.get("date", "")).startswith(today_str)]

        if not today_candles or not other_candles:
            return None  # 데이터 부족 → 통과
        today_open     = today_candles[0]["open"]
        yesterday_close = other_candles[-1]["close"]
        if yesterday_close <= 0:
            return None
        return (today_open - yesterday_close) / yesterday_close

    @staticmethod
    def _calc_ema(values: list[float], period: int) -> float:
        """지수이동평균(EMA) 계산"""
        if len(values) < period:
            return values[-1] if values else 0.0
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period  # 초기값: SMA
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _calc_order_amount(self) -> int:
        return int(self.capital * self._cfg["capital_ratio"])

    def _deployed_capital(self) -> int:
        return sum(int(p.avg_price * p.quantity) for p in self._positions.values())

    @staticmethod
    def _read_input(prompt: str, timeout_secs: int) -> str:
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
        if not self._positions:
            return "[단타] 보유 포지션 없음"
        lines = ["[단타] 현재 포지션:"]
        for sym, pos in self._positions.items():
            try:
                price = self.broker.get_price(sym)
                pct   = (price - pos.avg_price) / pos.avg_price * 100
                held  = pos.held_minutes()
                lines.append(
                    f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f} | "
                    f"현재 {price:,.0f} ({pct:+.2f}%) | 보유 {held:.0f}분"
                )
            except Exception:
                lines.append(f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f}")
        lines.append(f"  {self._pnl_progress_str()}")
        lines.append(f"  총 거래횟수: {self._trade_count}회")
        return "\n".join(lines)
