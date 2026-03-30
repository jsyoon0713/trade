"""RSI 기반 매수/매도 전략"""
import logging

from .base import BaseStrategy, Signal, StrategyResult
from .indicators import calc_rsi

logger = logging.getLogger(__name__)


class RSIStrategy(BaseStrategy):
    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate_signal(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        closes = [candle["close"] for candle in ohlcv if candle["close"] > 0]

        try:
            rsi = calc_rsi(closes, self.period)
        except ValueError as e:
            logger.warning(f"[{symbol}] RSI 계산 불가: {e}")
            return StrategyResult(
                symbol=symbol,
                signal=Signal.HOLD,
                reason=str(e),
                indicator_values={},
            )

        if rsi <= self.oversold:
            signal = Signal.BUY
            reason = f"RSI {rsi} ≤ {self.oversold} → 매수"
        elif rsi >= self.overbought:
            signal = Signal.SELL
            reason = f"RSI {rsi} ≥ {self.overbought} → 매도"
        else:
            signal = Signal.HOLD
            reason = f"RSI {rsi} (중립 구간)"

        logger.debug(f"[{symbol}] {reason}")
        return StrategyResult(
            symbol=symbol,
            signal=signal,
            reason=reason,
            indicator_values={"rsi": rsi},
        )
