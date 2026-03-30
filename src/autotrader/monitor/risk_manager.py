"""손절/익절 자동화 리스크 관리"""
import logging

from ..broker.ls_broker import LSBroker
from ..broker.models import Order, Position

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        broker: LSBroker,
        stop_loss_pct: float = -5.0,
        take_profit_pct: float = 10.0,
    ):
        self.broker = broker
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    def check_and_execute(self, positions: list[Position]) -> list[Order]:
        """
        전체 보유 종목에 대해 손절/익절 조건 확인 후 매도 실행
        반환: 체결된 주문 리스트
        """
        executed_orders: list[Order] = []
        for pos in positions:
            order = self._check_position(pos)
            if order:
                executed_orders.append(order)
        return executed_orders

    def _check_position(self, pos: Position) -> Order | None:
        pct = pos.profit_pct

        if pct <= self.stop_loss_pct:
            reason = f"손절: {pct:.2f}% ≤ {self.stop_loss_pct}%"
            logger.warning(f"[{pos.symbol}] {reason} → 전량 매도")
            return self.broker.sell_market(pos.symbol, pos.quantity, memo=reason)

        if pct >= self.take_profit_pct:
            reason = f"익절: {pct:.2f}% ≥ {self.take_profit_pct}%"
            logger.info(f"[{pos.symbol}] {reason} → 전량 매도")
            return self.broker.sell_market(pos.symbol, pos.quantity, memo=reason)

        return None
