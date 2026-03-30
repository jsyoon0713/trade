"""
단타 트레이더
- 08:30 종목 선택 프롬프트 (60초 대기 → 자동 스캐닝)
- 장중 1분 간격 5분봉 RSI(7) + 거래량 급증 체크
- 15:20 미청산 포지션 강제청산
- capital: 단타 전용 시드머니 한도 (내부 포지션 투입금이 초과하면 신규 매수 차단)
"""
import logging
import select
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..broker.ls_broker import LSBroker
from ..broker.models import Order, Position
from ..monitor.portfolio import PortfolioMonitor
from ..notification.telegram_notifier import TelegramNotifier
from ..scanner.stock_scanner import StockScanner
from ..strategy.daytrading_strategy import DayTradingStrategy

logger = logging.getLogger(__name__)

_PROMPT_TIMEOUT_SECS = 60  # 사용자 입력 대기 시간


@dataclass
class DayPosition:
    """단타 전용 포지션 (평균가 추적)"""
    symbol: str
    quantity: int
    avg_price: float
    entry_time: datetime = field(default_factory=datetime.now)


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
    ):
        self.broker = broker
        self.portfolio_monitor = portfolio_monitor
        self.notifier = notifier
        self.scanner = StockScanner(broker)
        self.strategy = DayTradingStrategy(
            period=rsi_period,
            oversold=rsi_oversold,
            overbought=rsi_overbought,
        )
        self.scan_top_n = scan_top_n
        self.capital = capital          # 단타 전용 시드머니 한도
        self.order_amount = order_amount
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_positions = max_positions
        self.candle_interval = candle_interval
        self.force_close_hour, self.force_close_min = map(int, force_close_time.split(":"))

        # 단타 대상 종목 (매일 초기화)
        self._watchlist: list[str] = []
        # 단타 포지션 내부 추적 (계좌 잔고와 분리)
        self._positions: dict[str, DayPosition] = {}

    # ── 08:30 준비 ────────────────────────────────────────────────────────────

    def morning_prep(self) -> None:
        """08:30 단타 종목 준비: 스캐닝 + 사용자 입력 대기"""
        logger.info("[단타] 아침 준비 시작 (08:30)")
        self._positions.clear()

        # 자동 스캐닝
        candidates = self.scanner.get_top_volume_stocks(self.scan_top_n)
        if candidates:
            print(f"\n[단타] 거래량 상위 후보 종목 ({len(candidates)}개):")
            for i, sym in enumerate(candidates, 1):
                print(f"  {i:2d}. {sym}")
        else:
            print("[단타] 스캔 결과 없음 - 종목을 직접 입력해 주세요")

        # 사용자 입력 대기
        print(
            f"\n[단타] 종목코드를 입력하세요 (쉼표 구분, {_PROMPT_TIMEOUT_SECS}초 내 미입력 시 자동 선택):"
        )
        print("예: 005930,000660,035420")

        user_symbols = self._wait_for_input(_PROMPT_TIMEOUT_SECS)

        if user_symbols:
            self._watchlist = [s.strip() for s in user_symbols.split(",") if s.strip()]
            logger.info(f"[단타] 사용자 선택 종목: {self._watchlist}")
            print(f"[단타] 선택 종목: {self._watchlist}")
        else:
            # 자동 선택: 상위 max_positions개
            self._watchlist = candidates[: self.max_positions]
            logger.info(f"[단타] 자동 선택 종목: {self._watchlist}")
            print(f"[단타] 자동 선택 종목: {self._watchlist}")

        msg = f"[단타] 오늘 종목: {', '.join(self._watchlist)}" if self._watchlist else "[단타] 오늘 단타 종목 없음"
        self.notifier.notify_portfolio(msg)

    def _wait_for_input(self, timeout_secs: int) -> str:
        """타임아웃 있는 stdin 읽기 (Unix only). Windows에서는 즉시 빈 문자열 반환."""
        if sys.platform == "win32":
            # Windows: select 미지원 → 입력 없이 자동 선택
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
        """매 1분 실행: 리스크 체크 → RSI 신호 → 주문"""
        if not self._watchlist:
            logger.debug("[단타] watchlist 없음 - 스킵")
            return

        deployed = self._deployed_capital()
        avail = self.capital - deployed
        logger.info(
            f"[단타] 시드머니: {self.capital:,}원 | 투입: {deployed:,}원 | 가용: {avail:,}원"
        )
        try:
            # 현재가 기준으로 내부 포지션 업데이트
            self._refresh_positions()

            # 1. 손절/익절 체크
            self._check_risk()

            # 2. 매수 신호 체크
            if len(self._positions) < self.max_positions:
                self._check_buy_signals()

        except Exception as e:
            logger.exception(f"[단타] 실행 오류: {e}")
            self.notifier.notify_error(f"[단타] {e}")

    def _refresh_positions(self) -> None:
        """보유 중인 단타 종목의 현재가 갱신"""
        for symbol in list(self._positions.keys()):
            try:
                current_price = self.broker.get_price(symbol)
                self._positions[symbol].avg_price  # avg_price는 그대로 유지
                # 현재가는 로깅용으로만 사용
                pct = (current_price - self._positions[symbol].avg_price) / self._positions[symbol].avg_price * 100
                logger.debug(f"[단타][{symbol}] 현재가 {current_price:,} 수익률 {pct:.2f}%")
            except Exception as e:
                logger.warning(f"[단타][{symbol}] 현재가 조회 실패: {e}")

    def _check_risk(self) -> None:
        """보유 종목 손절/익절 체크"""
        for symbol, pos in list(self._positions.items()):
            try:
                current_price = self.broker.get_price(symbol)
                pct = (current_price - pos.avg_price) / pos.avg_price * 100

                if pct <= self.stop_loss_pct:
                    reason = f"[단타] 손절: {pct:.2f}% ≤ {self.stop_loss_pct}%"
                    logger.warning(f"[{symbol}] {reason}")
                    order = self.broker.sell_market(symbol, pos.quantity, memo=reason)
                    self.notifier.notify_order(order)
                    del self._positions[symbol]

                elif pct >= self.take_profit_pct:
                    reason = f"[단타] 익절: {pct:.2f}% ≥ {self.take_profit_pct}%"
                    logger.info(f"[{symbol}] {reason}")
                    order = self.broker.sell_market(symbol, pos.quantity, memo=reason)
                    self.notifier.notify_order(order)
                    del self._positions[symbol]

            except Exception as e:
                logger.warning(f"[단타][{symbol}] 리스크 체크 실패: {e}")

    def _deployed_capital(self) -> int:
        """현재 단타 포지션에 투입된 금액 (매입가 × 수량)"""
        return sum(int(p.avg_price * p.quantity) for p in self._positions.values())

    def _check_buy_signals(self) -> None:
        """watchlist 대상 매수 신호 체크"""
        try:
            balance = self.portfolio_monitor.get_balance()
        except Exception as e:
            logger.warning(f"[단타] 잔고 조회 실패: {e}")
            return

        for symbol in self._watchlist:
            if len(self._positions) >= self.max_positions:
                break
            if symbol in self._positions:
                continue

            # 시드머니 한도 체크
            avail = self.capital - self._deployed_capital()
            if avail < self.order_amount:
                logger.info(
                    f"[단타] 매수 중단 - 시드머니 한도 초과 "
                    f"(가용 {avail:,}원 < 주문 {self.order_amount:,}원)"
                )
                break

            ohlcv = self.broker.get_ohlcv(symbol, period=self.candle_interval, count=50)
            if not ohlcv:
                continue

            result = self.strategy.generate_signal(symbol, ohlcv)
            logger.info(
                f"[단타][{symbol}] RSI={result.indicator_values.get('rsi','?')} "
                f"거래량급증={result.indicator_values.get('volume_spike','?')} "
                f"신호={result.signal.value}"
            )

            if result.signal.value == "buy":
                current_price = ohlcv[-1]["close"]
                qty = int(self.order_amount / current_price)
                if qty > 0 and balance.cash >= self.order_amount:
                    order = self.broker.buy_market(symbol, qty, memo=result.reason)
                    self.notifier.notify_order(order)
                    self._positions[symbol] = DayPosition(
                        symbol=symbol,
                        quantity=qty,
                        avg_price=current_price,
                    )
                    logger.info(f"[단타][{symbol}] 매수 {qty}주 @ {current_price:,}")

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
        msg = f"[단타] 강제청산 완료: {', '.join(closed)}" if closed else "[단타] 강제청산 완료 (없음)"
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
                lines.append(f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f} | 현재 {price:,.0f} ({pct:+.2f}%)")
            except Exception:
                lines.append(f"  {sym}: {pos.quantity}주 | 매입 {pos.avg_price:,.0f}")
        return "\n".join(lines)
