"""
단타 트레이더
- 08:30 파이프라인 종목 분석 (차트 + 백테스트) 후 자동 선정
- 일일 목표수익률 달성까지 지속 매매
- 파이프라인 점수 기반 분산투자 (강력매수 40%, 매수 25%)
- 30분마다 워치리스트 재스캔으로 더 좋은 종목 교체
- 15:20 미청산 포지션 강제청산
"""
import logging
import select
import sys
from dataclasses import dataclass, field
from datetime import datetime

from ..broker.ls_broker import LSBroker
from ..broker.models import Order, Position
from ..monitor.portfolio import PortfolioMonitor
from ..notification.telegram_notifier import TelegramNotifier
from ..pipeline.stock_analyzer import StockAnalyzer, StockScore
from ..pipeline.strategy_recommender import recommend, apply_allocation, Recommendation, MODES
from ..scanner.stock_scanner import StockScanner
from ..strategy.daytrading_strategy import DayTradingStrategy

logger = logging.getLogger(__name__)

_PROMPT_TIMEOUT_SECS = 60  # 사용자 입력 대기 시간
_RESCAN_INTERVAL_MINS = 30  # 워치리스트 재스캔 주기


@dataclass
class DayPosition:
    """단타 전용 포지션 (평균가 추적)"""
    symbol: str
    quantity: int
    avg_price: float
    entry_time: datetime = field(default_factory=datetime.now)
    pipeline_score: int = 0   # 진입 시 파이프라인 점수


class DayTrader:
    def __init__(
        self,
        broker: LSBroker,
        portfolio_monitor: PortfolioMonitor,
        notifier: TelegramNotifier,
        scan_top_n: int = 20,
        capital: int = 2_000_000,
        order_amount: int = 200_000,
        take_profit_pct: float = 2.0,
        stop_loss_pct: float = -1.5,
        max_positions: int = 3,
        rsi_period: int = 7,
        rsi_oversold: float = 35.0,
        rsi_overbought: float = 65.0,
        candle_interval: str = "5",
        force_close_time: str = "15:20",
        daily_target_pct: float = 0.0,   # 일일 목표수익률 (0이면 비활성)
    ):
        self.broker = broker
        self.portfolio_monitor = portfolio_monitor
        self.notifier = notifier
        self.scanner = StockScanner(broker)
        self.analyzer = StockAnalyzer(broker, scan_top_n=scan_top_n, backtest_months=3)
        self.strategy = DayTradingStrategy(
            period=rsi_period,
            oversold=rsi_oversold,
            overbought=rsi_overbought,
        )
        self.scan_top_n = scan_top_n
        self.capital = capital
        self.order_amount = order_amount
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_positions = max_positions
        self.candle_interval = candle_interval
        self.force_close_hour, self.force_close_min = map(int, force_close_time.split(":"))
        self.daily_target_pct = daily_target_pct

        # 상태
        self._watchlist: list[str] = []
        self._positions: dict[str, DayPosition] = {}
        self._pipeline_scores: dict[str, StockScore] = {}  # symbol → 최신 파이프라인 점수
        self._daily_realized_pnl: float = 0.0              # 당일 실현 손익 (원)
        self._last_rescan_time: datetime | None = None      # 마지막 재스캔 시각
        self._recommendation: Recommendation | None = None  # 최근 투자 방식 추천
        self._mode_allocations: dict[str, int] = {}        # 모드 적용 시 symbol→투입금액

    # ── 목표수익률 추적 ───────────────────────────────────────────────────────

    @property
    def daily_target_amount(self) -> float:
        """목표 수익금액 (원)"""
        return self.capital * self.daily_target_pct / 100

    @property
    def target_achieved(self) -> bool:
        """목표수익률 달성 여부"""
        if self.daily_target_pct <= 0:
            return False
        return self._daily_realized_pnl >= self.daily_target_amount

    def _record_realized_pnl(self, symbol: str, quantity: int, sell_price: float) -> float:
        """청산 시 실현 손익 기록, pnl 반환"""
        pos = self._positions.get(symbol)
        if not pos:
            return 0.0
        pnl = (sell_price - pos.avg_price) * quantity
        self._daily_realized_pnl += pnl
        pct = pnl / (pos.avg_price * quantity) * 100
        logger.info(
            f"[단타][{symbol}] 실현손익: {pnl:+,.0f}원 ({pct:+.2f}%) | "
            f"당일 누적: {self._daily_realized_pnl:+,.0f}원 "
            f"(목표: {self.daily_target_amount:,.0f}원)"
        )
        return pnl

    # ── 분산투자 금액 계산 ────────────────────────────────────────────────────

    def _position_size(self, symbol: str) -> int:
        """
        포지션 크기 결정 (우선순위):
        1) 모드 배분 테이블 (_mode_allocations) — 추천 모드 적용 시
        2) 파이프라인 점수 기반 — 직접 입력 시
        3) order_amount 고정 — 기본값
        """
        avail = self.capital - self._deployed_capital()

        # 모드 배분 우선
        if symbol in self._mode_allocations:
            return min(self._mode_allocations[symbol], avail)

        # 파이프라인 점수 기반
        score = self._pipeline_scores.get(symbol)
        if score is None:
            return min(self.order_amount, avail)

        if score.total_score >= 70:
            amount = int(self.capital * 0.40)
        elif score.total_score >= 50:
            amount = int(self.capital * 0.25)
        else:
            amount = self.order_amount

        return max(min(amount, avail), self.order_amount)

    # ── 08:30 준비 ────────────────────────────────────────────────────────────

    def morning_prep(self, macro_score: str = "neutral") -> None:
        """
        08:30 단타 종목 준비
        1) 거래량 상위 스캔 + 파이프라인 분석
        2) 시장 상황 기반 투자 방식 추천
        3) 사용자 선택 대기 (60초)
        4) 선택된 모드로 종목/자금 배분 결정
        """
        logger.info("[단타] 아침 준비 시작 (08:30)")
        self._positions.clear()
        self._daily_realized_pnl = 0.0
        self._last_rescan_time = None
        self._recommendation = None
        self._mode_allocations = {}

        if self.daily_target_pct > 0:
            logger.info(
                f"[단타] 일일 목표수익률: {self.daily_target_pct}% "
                f"({self.daily_target_amount:,.0f}원)"
            )

        # 1단계: 스캔 + 파이프라인 분석
        candidates = self.scanner.get_top_volume_stocks(self.scan_top_n)
        logger.info(f"[단타] 거래량 상위 후보: {len(candidates)}개")
        logger.info("[단타] 파이프라인 분석 중 (차트 + 백테스트)...")
        scored = self.analyzer.run(candidates if candidates else None)
        self._update_pipeline_scores(scored)

        # 종목 분석 결과 출력
        if scored:
            print(f"\n[단타] ── 종목 분석 결과 ({len(scored)}개) ──")
            print(f"  {'코드':>8}  {'회사명':<12}  {'현재가':>8}  {'점수':>5}  {'결정':<6}  요약")
            print("  " + "─" * 78)
            for s in scored:
                flag = "★" if s.decision == "강력매수" else "·"
                print(
                    f"  {flag} {s.symbol:>7}  {s.company_name:<12}  {s.current_price:>8,.0f}  "
                    f"{s.total_score:>5}  {s.decision:<6}  {s.reason[:38]}"
                )

        # 2단계: 투자 방식 추천
        rec = recommend(scored, self.capital, self.order_amount, macro_score)
        self._recommendation = rec

        print(f"\n[단타] ── 투자 방식 추천 ──")
        print(f"  매크로: {macro_score}  |  매수 종목: {rec.buy_candidates}개  |  최고점: {rec.top_stock_score}점")
        print(rec.summary_text())

        # 3단계: 사용자 선택
        mode_options = "  " + " / ".join(
            f"[{k[0]}]{v}" for k, v in {
                "1": "추천대로", "2": "집중투자", "3": "비중분산",
                "4": "균형분산", "5": "소극적", "6": "관망", "7": "직접입력"
            }.items()
        )
        print(f"\n[단타] 투자 방식을 선택하세요 ({_PROMPT_TIMEOUT_SECS}초 내 미입력 시 추천대로 진행):")
        print(mode_options)

        user_input = self._wait_for_input(_PROMPT_TIMEOUT_SECS).strip()
        chosen_mode, custom_symbols = self._parse_mode_input(user_input, rec, scored, candidates)

        # 4단계: 선택 모드 적용
        if custom_symbols:
            # 직접 종목 입력한 경우
            self._watchlist = custom_symbols
            self._mode_allocations = {}
            logger.info(f"[단타] 직접 입력 종목: {self._watchlist}")
        elif chosen_mode == "HOLD":
            self._watchlist = []
            self._mode_allocations = {}
            logger.info("[단타] 관망 선택 — 오늘 단타 없음")
        else:
            # 모드에 맞는 추천 재계산 후 적용
            if chosen_mode != rec.mode:
                from ..pipeline.strategy_recommender import recommend as _rec
                rec = _rec(scored, self.capital, self.order_amount, macro_score)
                rec.mode = chosen_mode
                from ..pipeline.strategy_recommender import MODES, MODE_DESC, _calc_allocations
                rec.mode_label = MODES[chosen_mode]
                rec.description = MODE_DESC[chosen_mode]
                rec.allocations = _calc_allocations(chosen_mode, [s for s in scored if s.decision in ("매수","강력매수")], self.capital, self.order_amount)
                self._recommendation = rec
            apply_allocation(self, rec)

        print(f"\n[단타] 최종 선택: [{MODES.get(chosen_mode, chosen_mode)}]")
        print(f"  대상 종목: {self._watchlist}")
        if self._mode_allocations:
            for sym, amt in self._mode_allocations.items():
                s = self._pipeline_scores.get(sym)
                name = s.company_name if s else sym
                print(f"    {sym}({name}): {amt:,}원")

        msg_lines = [f"[단타] 오늘 전략: {MODES.get(chosen_mode, '?')}"]
        if self._watchlist:
            msg_lines.append(f"종목: {', '.join(self._watchlist)}")
        if self.daily_target_pct > 0:
            msg_lines.append(f"목표: {self.daily_target_pct}% ({self.daily_target_amount:,.0f}원)")
        msg_lines.append(f"매크로: {macro_score} | 추천근거: {rec.reasoning}")
        self.notifier.notify_portfolio("\n".join(msg_lines))
        self._last_rescan_time = datetime.now()

    def _parse_mode_input(
        self,
        user_input: str,
        rec: "Recommendation",
        scored: list,
        candidates: list[str],
    ) -> tuple[str, list[str]]:
        """
        사용자 입력 파싱
        반환: (mode_key, custom_symbols)
        custom_symbols가 비어있으면 mode_key 로 처리
        """
        mapping = {
            "1": rec.mode, "추천": rec.mode,
            "2": "FOCUSED",  "집중": "FOCUSED",
            "3": "WEIGHTED",  "비중": "WEIGHTED",
            "4": "BALANCED",  "균형": "BALANCED",
            "5": "DEFENSIVE", "소극": "DEFENSIVE",
            "6": "HOLD",      "관망": "HOLD",
        }

        if not user_input:
            logger.info(f"[단타] 시간 초과 → 추천 모드 적용: {rec.mode}")
            return rec.mode, []

        # 숫자/키워드 선택
        for key, mode in mapping.items():
            if user_input == key or user_input.startswith(key):
                return mode, []

        # 7 또는 종목코드 직접 입력
        parts = [p.strip() for p in user_input.replace("/", ",").split(",") if p.strip()]
        if parts:
            return "CUSTOM", parts

        return rec.mode, []

    def _wait_for_input(self, timeout_secs: int) -> str:
        if sys.platform == "win32":
            return ""
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout_secs)
            if ready:
                return sys.stdin.readline().strip()
        except Exception:
            pass
        print("\n[단타] 시간 초과 → 자동 선택")
        return ""

    # ── 장중 실행 ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        매 1분 실행:
        1) 목표수익률 달성 체크
        2) 30분마다 워치리스트 재스캔 (더 좋은 종목으로 교체)
        3) 손절/익절 체크
        4) 매수 신호 체크 (분산투자 적용)
        """
        if not self._watchlist:
            logger.debug("[단타] watchlist 없음 - 스킵")
            return

        # 목표수익률 달성 시 신규 매수 중단
        if self.target_achieved:
            logger.info(
                f"[단타] 목표수익률 달성! 실현손익 {self._daily_realized_pnl:+,.0f}원 "
                f"(목표 {self.daily_target_amount:,.0f}원) → 신규 매수 중단"
            )
            self._check_risk()  # 기존 포지션 리스크 체크만 계속
            return

        deployed = self._deployed_capital()
        avail = self.capital - deployed
        logger.info(
            f"[단타] 시드머니: {self.capital:,}원 | 투입: {deployed:,}원 | 가용: {avail:,}원 | "
            f"당일손익: {self._daily_realized_pnl:+,.0f}원"
        )

        try:
            self._refresh_positions()

            # 30분마다 워치리스트 재스캔
            self._maybe_rescan()

            # 손절/익절 체크
            self._check_risk()

            # 매수 신호 체크
            if len(self._positions) < self.max_positions:
                self._check_buy_signals()

        except Exception as e:
            logger.exception(f"[단타] 실행 오류: {e}")
            self.notifier.notify_error(f"[단타] {e}")

    def _maybe_rescan(self) -> None:
        """30분마다 파이프라인 재스캔 → 더 좋은 종목으로 워치리스트 교체"""
        now = datetime.now()
        if self._last_rescan_time is None:
            self._last_rescan_time = now
            return

        elapsed_mins = (now - self._last_rescan_time).total_seconds() / 60
        if elapsed_mins < _RESCAN_INTERVAL_MINS:
            return

        logger.info("[단타] 워치리스트 재스캔 시작 (30분 주기)")
        try:
            candidates = self.scanner.get_top_volume_stocks(self.scan_top_n)
            scored = self.analyzer.run(candidates if candidates else None)
            self._update_pipeline_scores(scored)

            new_buys = [s.symbol for s in scored if s.decision in ("매수", "강력매수")]
            # 포지션 없는 종목만 교체 (보유 중인 종목은 유지)
            held = set(self._positions.keys())
            refreshed = list(held) + [s for s in new_buys if s not in held]
            refreshed = refreshed[: self.max_positions * 2]  # 최대 후보 수 제한

            added = [s for s in refreshed if s not in self._watchlist]
            removed = [s for s in self._watchlist if s not in refreshed and s not in held]

            self._watchlist = refreshed
            self._last_rescan_time = now

            if added or removed:
                logger.info(f"[단타] 워치리스트 갱신 | 추가: {added} | 제거: {removed}")
                self.notifier.notify_portfolio(
                    f"[단타] 종목 교체\n추가: {', '.join(added)}\n제거: {', '.join(removed)}"
                )
        except Exception as e:
            logger.warning(f"[단타] 재스캔 실패: {e}")

    def _update_pipeline_scores(self, scored: list[StockScore]) -> None:
        for s in scored:
            self._pipeline_scores[s.symbol] = s

    def _refresh_positions(self) -> None:
        for symbol in list(self._positions.keys()):
            try:
                current_price = self.broker.get_price(symbol)
                pct = (current_price - self._positions[symbol].avg_price) / self._positions[symbol].avg_price * 100
                logger.debug(f"[단타][{symbol}] 현재가 {current_price:,} 수익률 {pct:.2f}%")
            except Exception as e:
                logger.warning(f"[단타][{symbol}] 현재가 조회 실패: {e}")

    def _check_risk(self) -> None:
        """손절/익절 체크 → 실현손익 누적"""
        for symbol, pos in list(self._positions.items()):
            try:
                current_price = self.broker.get_price(symbol)
                pct = (current_price - pos.avg_price) / pos.avg_price * 100

                if pct <= self.stop_loss_pct:
                    reason = f"[단타] 손절: {pct:.2f}% ≤ {self.stop_loss_pct}%"
                    logger.warning(f"[{symbol}] {reason}")
                    self._record_realized_pnl(symbol, pos.quantity, current_price)
                    order = self.broker.sell_market(symbol, pos.quantity, memo=reason)
                    self.notifier.notify_order(order)
                    del self._positions[symbol]

                elif pct >= self.take_profit_pct:
                    reason = f"[단타] 익절: {pct:.2f}% ≥ {self.take_profit_pct}%"
                    logger.info(f"[{symbol}] {reason}")
                    self._record_realized_pnl(symbol, pos.quantity, current_price)
                    order = self.broker.sell_market(symbol, pos.quantity, memo=reason)
                    self.notifier.notify_order(order)
                    del self._positions[symbol]

                    # 익절 후 목표 미달성이면 바로 재진입 후보 탐색
                    if not self.target_achieved:
                        logger.info(f"[단타] {symbol} 익절 후 재진입 후보 탐색")

            except Exception as e:
                logger.warning(f"[단타][{symbol}] 리스크 체크 실패: {e}")

    def _deployed_capital(self) -> int:
        return sum(int(p.avg_price * p.quantity) for p in self._positions.values())

    def _check_buy_signals(self) -> None:
        """파이프라인 점수 기반 분산투자 매수"""
        logger.info(
            f"[단타] 매수 신호 체크 | max_pos={self.max_positions} "
            f"sl={self.stop_loss_pct}% tp={self.take_profit_pct}%"
        )
        try:
            balance = self.portfolio_monitor.get_balance()
        except Exception as e:
            logger.warning(f"[단타] 잔고 조회 실패: {e}")
            return

        # 파이프라인 점수 높은 순으로 정렬
        watchlist_sorted = sorted(
            self._watchlist,
            key=lambda s: self._pipeline_scores[s].total_score if s in self._pipeline_scores else 0,
            reverse=True,
        )

        for symbol in watchlist_sorted:
            if len(self._positions) >= self.max_positions:
                break
            if symbol in self._positions:
                continue

            # 분산투자 금액 결정
            order_amount = self._position_size(symbol)
            avail = self.capital - self._deployed_capital()
            if avail < order_amount:
                logger.info(
                    f"[단타] 매수 중단 - 가용 {avail:,}원 < 주문 {order_amount:,}원"
                )
                break

            ohlcv = self.broker.get_ohlcv(symbol, period=self.candle_interval, count=50)
            if not ohlcv:
                continue

            result = self.strategy.generate_signal(symbol, ohlcv)
            pipeline_score = self._pipeline_scores.get(symbol)
            logger.info(
                f"[단타][{symbol}] RSI={result.indicator_values.get('rsi','?')} "
                f"파이프라인={pipeline_score.total_score if pipeline_score else '?'}점 "
                f"신호={result.signal.value}"
            )

            if result.signal.value == "buy":
                current_price = ohlcv[-1]["close"]
                qty = int(order_amount / current_price)
                if qty > 0 and balance.cash >= order_amount:
                    order = self.broker.buy_market(symbol, qty, memo=result.reason)
                    self.notifier.notify_order(order)
                    self._positions[symbol] = DayPosition(
                        symbol=symbol,
                        quantity=qty,
                        avg_price=current_price,
                        pipeline_score=pipeline_score.total_score if pipeline_score else 0,
                    )
                    logger.info(
                        f"[단타][{symbol}] 매수 {qty}주 @ {current_price:,} "
                        f"(투입 {order_amount:,}원, 파이프라인 {pipeline_score.total_score if pipeline_score else 0}점)"
                    )

    # ── 15:20 강제청산 ────────────────────────────────────────────────────────

    def force_close_all(self) -> None:
        """15:20 단타 포지션 전량 강제청산"""
        logger.info("[단타] 15:20 강제청산 시작")
        if not self._positions:
            logger.info("[단타] 강제청산할 포지션 없음")
            return

        closed: list[str] = []
        for symbol, pos in list(self._positions.items()):
            try:
                current_price = self.broker.get_price(symbol)
                self._record_realized_pnl(symbol, pos.quantity, current_price)
                reason = "[단타] 장 마감 강제청산"
                order = self.broker.sell_market(symbol, pos.quantity, memo=reason)
                self.notifier.notify_order(order)
                closed.append(symbol)
                logger.info(f"[단타][{symbol}] 강제청산 {pos.quantity}주")
            except Exception as e:
                logger.error(f"[단타][{symbol}] 강제청산 실패: {e}")
                self.notifier.notify_error(f"[단타] {symbol} 강제청산 실패: {e}")

        for symbol in closed:
            self._positions.pop(symbol, None)

        self._watchlist.clear()
        target_str = (
            f"\n목표: {self.daily_target_amount:,.0f}원 / 실현: {self._daily_realized_pnl:+,.0f}원"
            if self.daily_target_pct > 0 else ""
        )
        msg = (
            f"[단타] 강제청산 완료: {', '.join(closed)}{target_str}"
            if closed else "[단타] 강제청산 완료 (없음)"
        )
        logger.info(msg)
        self.notifier.notify_portfolio(msg)

    def get_positions_summary(self) -> str:
        """단타 포지션 현황 문자열 반환"""
        if not self._positions:
            return "[단타] 보유 포지션 없음"
        lines = ["[단타] 현재 포지션:"]
        for sym, pos in self._positions.items():
            try:
                price = self.broker.get_price(sym)
                pct = (price - pos.avg_price) / pos.avg_price * 100
                lines.append(
                    f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f} | "
                    f"현재 {price:,.0f} ({pct:+.2f}%) | 점수 {pos.pipeline_score}"
                )
            except Exception:
                lines.append(f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f}")
        if self.daily_target_pct > 0:
            lines.append(
                f"  당일실현: {self._daily_realized_pnl:+,.0f}원 / "
                f"목표: {self.daily_target_amount:,.0f}원 "
                f"({'달성' if self.target_achieved else '미달성'})"
            )
        return "\n".join(lines)
