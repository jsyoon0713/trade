from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class StrategyResult:
    symbol: str
    signal: Signal
    reason: str        # 예: "RSI 28.4 → 매수"
    indicator_values: dict  # 디버깅/알림용 지표 값


class BaseStrategy(ABC):
    @abstractmethod
    def generate_signal(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        """
        ohlcv: [{date, open, high, low, close, volume}, ...] 오래된 순
        """
        ...
