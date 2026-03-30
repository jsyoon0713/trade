from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    status: OrderStatus = OrderStatus.PENDING
    order_id: str = ""
    filled_at: datetime | None = None
    memo: str = ""  # 매매 이유 (예: "RSI 28 → 매수")


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_price: float          # 평균 매입가
    current_price: float = 0.0

    @property
    def profit_pct(self) -> float:
        if self.avg_price == 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price * 100

    @property
    def profit_amount(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def market_value(self) -> float:
        return self.current_price * self.quantity


@dataclass
class AccountBalance:
    total_eval: float           # 총 평가금액
    cash: float                 # 예수금
    total_profit_loss: float    # 총 평가손익
    total_profit_pct: float     # 총 수익률
    positions: list[Position] = field(default_factory=list)
