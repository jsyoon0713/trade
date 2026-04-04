"""
중장기(스윙) 트레이더
- CombinedStrategy(RSI 일봉 + 뉴스) 사용
- watchlist 기반 종목 관리
- 30분 간격 실행
- capital: 스윙 전용 시드머니 한도 (watchlist 종목 투자금 합산이 초과하면 신규 매수 차단)
"""
import logging

from ..broker.ls_broker import LSBroker
from ..broker.models import Order
from ..monitor.portfolio import PortfolioMonitor
from ..monitor.risk_manager import RiskManager
from ..notification.telegram_notifier import TelegramNotifier
from ..pipeline.stock_analyzer import StockAnalyzer
from ..strategy.combined_strategy import CombinedStrategy
from ..strategy.rsi_strategy import RSIStrategy

logger = logging.getLogger(__name__)


class SwingTrader:
    def __init__(
        self,
        broker: LSBroker,
        strategy: CombinedStrategy | RSIStrategy,
        portfolio_monitor: PortfolioMonitor,
        risk_manager: RiskManager,
        notifier: TelegramNotifier,
        watchlist: list[str],
        capital: int,
        order_amount: int,
        max_positions: int,
        candle_interval: str = "D",
        use_pipeline: bool = True,
    ):
        self.broker = broker
        self.strategy = strategy
        self.portfolio_monitor = portfolio_monitor
        self.risk_manager = risk_manager
        self.notifier = notifier
        self.watchlist = list(watchlist)
        self.capital = capital          # 스윙 전용 시드머니 한도
        self.order_amount = order_amount
        self.max_positions = max_positions
        self.candle_interval = candle_interval
        self.use_pipeline = use_pipeline
        self._analyzer = StockAnalyzer(broker, scan_top_n=30, backtest_months=3) if use_pipeline else None
        self._pipeline_scores: list = []  # 최근 파이프라인 결과 캐시

    def deployed_capital(self, positions) -> int:
        """현재 스윙 포지션에 투입된 금액 (평균매입가 × 수량)"""
        return sum(
            int(pos.avg_price * pos.quantity)
            for pos in positions
            if pos.symbol in self.watchlist
        )

    def available_capital(self, positions) -> int:
        """추가 매수 가능 금액 = 시드머니 한도 - 현재 투입금"""
        return self.capital - self.deployed_capital(positions)

    def refresh_watchlist(self) -> None:
        """파이프라인으로 워치리스트 갱신 (매수/강력매수 종목 추가)"""
        if not self._analyzer:
            return
        try:
            logger.info("[스윙] 파이프라인으로 워치리스트 갱신 중...")
            scores = self._analyzer.run()
            self._pipeline_scores = scores
            pipeline_buys = [s.symbol for s in scores if s.decision in ("매수", "강력매수")]
            # 기존 watchlist + 파이프라인 추천 종목 합산 (중복 제거, 순서 유지)
            merged = list(dict.fromkeys(self.watchlist + pipeline_buys))
            added = [s for s in pipeline_buys if s not in self.watchlist]
            self.watchlist = merged
            if added:
                logger.info(f"[스윙] 파이프라인 추가 종목: {added}")
                self.notifier.notify_portfolio(
                    f"[스윙] 파이프라인 추천 종목 추가: {', '.join(added)}"
                )
        except Exception as e:
            logger.warning(f"[스윙] 파이프라인 갱신 오류: {e}")

    def run(self) -> None:
        """중장기 전략 1회 실행: 리스크 체크 → RSI+뉴스 신호 → 주문"""
        logger.info(
            f"[스윙] 전략 실행 시작 | order_amount={self.order_amount:,} "
            f"max_pos={self.max_positions} sl={self.risk_manager.stop_loss_pct}% "
            f"tp={self.risk_manager.take_profit_pct}%"
        )
        try:
            balance = self.portfolio_monitor.get_balance()
            deployed = self.deployed_capital(balance.positions)
            avail = self.capital - deployed
            logger.info(
                f"[스윙] 시드머니: {self.capital:,}원 | 투입: {deployed:,}원 | 가용: {avail:,}원"
            )

            # 1. 손절/익절 체크
            orders = self.risk_manager.check_and_execute(balance.positions)
            for order in orders:
                self.notifier.notify_order(order)

            # 2. 매수 신호 체크
            held_symbols = {pos.symbol for pos in balance.positions}

            for symbol in self.watchlist:
                if len(held_symbols) >= self.max_positions:
                    break
                if symbol in held_symbols:
                    continue

                ohlcv = self.broker.get_ohlcv(symbol, period=self.candle_interval, count=60)
                if not ohlcv:
                    continue

                result = self.strategy.generate_signal(symbol, ohlcv)

                if result.indicator_values.get("news_sentiment"):
                    logger.info(
                        f"[스윙][{symbol}] 뉴스: {result.indicator_values['news_sentiment']} "
                        f"(오버라이드: {result.indicator_values.get('news_override', 'none')})"
                    )

                if result.signal.value == "buy":
                    current_price = ohlcv[-1]["close"]
                    qty = int(self.order_amount / current_price)

                    # 시드머니 한도 체크
                    if avail < self.order_amount:
                        logger.info(
                            f"[스윙][{symbol}] 매수 스킵 - 시드머니 한도 초과 "
                            f"(가용 {avail:,}원 < 주문 {self.order_amount:,}원)"
                        )
                        continue

                    if qty > 0 and balance.cash >= self.order_amount:
                        order = self.broker.buy_market(symbol, qty, memo=f"[스윙] {result.reason}")
                        self.notifier.notify_order(order)
                        held_symbols.add(symbol)
                        avail -= self.order_amount  # 투입금 갱신
                        logger.info(f"[스윙][{symbol}] 매수 {qty}주: {result.reason}")

            # 3. 보유 종목 매도 신호
            balance = self.portfolio_monitor.get_balance()
            for pos in balance.positions:
                if pos.symbol not in self.watchlist:
                    continue
                ohlcv = self.broker.get_ohlcv(pos.symbol, period=self.candle_interval, count=60)
                if not ohlcv:
                    continue
                result = self.strategy.generate_signal(pos.symbol, ohlcv)
                if result.signal.value == "sell":
                    order = self.broker.sell_market(pos.symbol, pos.quantity, memo=f"[스윙] {result.reason}")
                    self.notifier.notify_order(order)
                    logger.info(f"[스윙][{pos.symbol}] 매도 {pos.quantity}주: {result.reason}")

        except Exception as e:
            logger.exception(f"[스윙] 전략 실행 오류: {e}")
            self.notifier.notify_error(f"[스윙] {e}")
