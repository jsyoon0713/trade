"""
단타 전략: 5분봉 RSI(7) + 거래량 급증 확인
- oversold=35, overbought=65 (단타용 좁은 기준)
- 현재 거래량 > 최근 평균 거래량의 1.5배 조건 추가
"""
import logging

from .base import BaseStrategy, Signal, StrategyResult
from .indicators import calc_rsi

logger = logging.getLogger(__name__)

_VOLUME_SPIKE_RATIO = 1.5  # 거래량 급증 기준 배수
_VOLUME_AVG_PERIODS = 10   # 평균 거래량 계산 기간 (캔들 수)


class DayTradingStrategy(BaseStrategy):
    def __init__(
        self,
        period: int = 7,
        oversold: float = 35.0,
        overbought: float = 65.0,
        volume_spike_ratio: float = _VOLUME_SPIKE_RATIO,
    ):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.volume_spike_ratio = volume_spike_ratio

    def generate_signal(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        closes = [candle["close"] for candle in ohlcv if candle["close"] > 0]
        volumes = [candle["volume"] for candle in ohlcv if candle["close"] > 0]

        # RSI 계산
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

        # 거래량 급증 확인
        volume_ok = self._check_volume_spike(volumes)
        current_vol = volumes[-1] if volumes else 0
        avg_vol = self._calc_avg_volume(volumes)

        indicator_values = {
            "rsi": rsi,
            "current_volume": current_vol,
            "avg_volume": avg_vol,
            "volume_spike": volume_ok,
        }

        if rsi <= self.oversold:
            if volume_ok:
                signal = Signal.BUY
                reason = f"RSI {rsi} ≤ {self.oversold} + 거래량 급증({current_vol:,} > avg {avg_vol:,.0f}×{self.volume_spike_ratio}) → 단타 매수"
            else:
                signal = Signal.HOLD
                reason = f"RSI {rsi} ≤ {self.oversold} 이나 거래량 미충족(현재 {current_vol:,}, 필요 {avg_vol * self.volume_spike_ratio:,.0f}) → 관망"
        elif rsi >= self.overbought:
            signal = Signal.SELL
            reason = f"RSI {rsi} ≥ {self.overbought} → 단타 매도"
        else:
            signal = Signal.HOLD
            reason = f"RSI {rsi} (중립 구간 {self.oversold}~{self.overbought})"

        logger.debug(f"[{symbol}] {reason}")
        return StrategyResult(
            symbol=symbol,
            signal=signal,
            reason=reason,
            indicator_values=indicator_values,
        )

    def _check_volume_spike(self, volumes: list[int]) -> bool:
        if len(volumes) < 2:
            return False
        avg = self._calc_avg_volume(volumes[:-1])  # 직전 캔들들의 평균
        if avg == 0:
            return False
        return volumes[-1] >= avg * self.volume_spike_ratio

    def _calc_avg_volume(self, volumes: list[int]) -> float:
        if not volumes:
            return 0.0
        # 최근 _VOLUME_AVG_PERIODS개 기준 (마지막 제외)
        window = volumes[-_VOLUME_AVG_PERIODS - 1 : -1] if len(volumes) > 1 else volumes
        if not window:
            return 0.0
        return sum(window) / len(window)
