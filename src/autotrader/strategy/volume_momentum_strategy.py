"""
거래량 폭발 + 가격 모멘텀 기반 단타 전략

매수 조건:
  1. 현재 거래량 > 최근 평균 거래량 × volume_spike_ratio (거래량 폭발)
  2. 최근 N봉 누적 가격 상승률 > momentum_threshold (모멘텀 상승)
  3. 현재가 > 직전 N봉 고가 (신고가 돌파 or 상단 압박)

매도 조건 (모멘텀 약화 기반):
  1. 거래량 < 평균 × exit_volume_ratio  (거래량 급감 → 모멘텀 소멸)
  OR
  2. consecutive_down 봉 연속 하락      (추세 반전)
  OR
  3. RSI ≥ rsi_overbought               (과매수)

  entry_price 제공 시 수익률도 함께 로깅
"""
import logging

from .base import BaseStrategy, Signal, StrategyResult
from .indicators import calc_rsi

logger = logging.getLogger(__name__)

_VOL_AVG_PERIODS = 10   # 평균 거래량 계산 기간 (봉 수)


class VolumeMomentumStrategy(BaseStrategy):
    def __init__(
        self,
        volume_spike_ratio: float = 3.0,
        exit_volume_ratio: float = 1.5,
        momentum_candles: int = 3,
        momentum_threshold: float = 0.005,
        consecutive_down: int = 3,
        rsi_overbought: float = 75.0,
    ):
        self.volume_spike_ratio = volume_spike_ratio
        self.exit_volume_ratio = exit_volume_ratio
        self.momentum_candles = momentum_candles
        self.momentum_threshold = momentum_threshold
        self.consecutive_down = consecutive_down
        self.rsi_overbought = rsi_overbought

    # ── 공통 진입점 (BaseStrategy 호환) ────────────────────────────────────

    def generate_signal(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        """미보유 종목에 대한 매수 신호 검사"""
        return self._check_entry(symbol, ohlcv)

    # ── 매수 신호 ──────────────────────────────────────────────────────────

    def _check_entry(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        if len(ohlcv) < max(_VOL_AVG_PERIODS + 1, self.momentum_candles + 1):
            return StrategyResult(
                symbol=symbol, signal=Signal.HOLD,
                reason="데이터 부족", indicator_values={},
            )

        closes  = [c["close"]  for c in ohlcv]
        volumes = [c["volume"] for c in ohlcv]

        current_price  = closes[-1]
        current_vol    = volumes[-1]
        avg_vol        = self._avg_volume(volumes)
        vol_ratio      = current_vol / avg_vol if avg_vol > 0 else 0.0

        # 가격 모멘텀: 최근 N봉 누적 상승률
        base_price    = closes[-self.momentum_candles - 1]
        momentum_pct  = (current_price - base_price) / base_price if base_price > 0 else 0.0

        # RSI (계산 실패해도 진입 차단하지 않음)
        try:
            rsi = calc_rsi(closes, period=7)
        except ValueError:
            rsi = 50.0

        indicators = {
            "current_volume": current_vol,
            "avg_volume":     avg_vol,
            "vol_ratio":      round(vol_ratio, 2),
            "momentum_pct":   round(momentum_pct * 100, 3),
            "rsi":            rsi,
        }

        # 매수 조건
        vol_ok      = vol_ratio >= self.volume_spike_ratio
        momentum_ok = momentum_pct >= self.momentum_threshold
        not_overbought = rsi < self.rsi_overbought

        if vol_ok and momentum_ok and not_overbought:
            reason = (
                f"거래량 폭발({vol_ratio:.1f}x ≥ {self.volume_spike_ratio}x) "
                f"+ 모멘텀 {momentum_pct*100:+.2f}% ≥ {self.momentum_threshold*100:.1f}%"
            )
            logger.info(f"[{symbol}] 매수신호: {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.BUY, reason=reason, indicator_values=indicators)

        reasons = []
        if not vol_ok:
            reasons.append(f"거래량 미충족({vol_ratio:.1f}x < {self.volume_spike_ratio}x)")
        if not momentum_ok:
            reasons.append(f"모멘텀 부족({momentum_pct*100:+.2f}%)")
        if not not_overbought:
            reasons.append(f"RSI 과매수({rsi:.0f})")

        return StrategyResult(
            symbol=symbol, signal=Signal.HOLD,
            reason=" | ".join(reasons), indicator_values=indicators,
        )

    # ── 매도 신호 (보유 종목용) ────────────────────────────────────────────

    def check_exit(
        self,
        symbol: str,
        ohlcv: list[dict],
        entry_price: float = 0.0,
    ) -> StrategyResult:
        """
        보유 종목에 대한 매도 신호 검사.
        entry_price > 0 이면 수익률 로깅에 활용.
        """
        if len(ohlcv) < max(_VOL_AVG_PERIODS + 1, self.consecutive_down + 1):
            return StrategyResult(
                symbol=symbol, signal=Signal.HOLD,
                reason="데이터 부족 — 보유 유지", indicator_values={},
            )

        closes  = [c["close"]  for c in ohlcv]
        volumes = [c["volume"] for c in ohlcv]

        current_price = closes[-1]
        current_vol   = volumes[-1]
        avg_vol       = self._avg_volume(volumes)
        vol_ratio     = current_vol / avg_vol if avg_vol > 0 else 0.0

        # RSI
        try:
            rsi = calc_rsi(closes, period=7)
        except ValueError:
            rsi = 50.0

        # 수익률 (참고용)
        pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0

        indicators = {
            "current_volume": current_vol,
            "avg_volume":     avg_vol,
            "vol_ratio":      round(vol_ratio, 2),
            "rsi":            rsi,
            "pnl_pct":        round(pnl_pct, 2),
        }

        # 조건 1: 거래량 급감 (모멘텀 소멸)
        if avg_vol > 0 and vol_ratio < self.exit_volume_ratio:
            reason = (
                f"거래량 급감({vol_ratio:.1f}x < {self.exit_volume_ratio}x) — 모멘텀 소멸"
                + (f" | 수익 {pnl_pct:+.2f}%" if entry_price > 0 else "")
            )
            logger.info(f"[{symbol}] 매도신호(거래량): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.SELL, reason=reason, indicator_values=indicators)

        # 조건 2: 연속 하락 (추세 반전)
        recent_closes = closes[-self.consecutive_down:]
        all_down = all(
            recent_closes[i] < recent_closes[i - 1]
            for i in range(1, len(recent_closes))
        )
        if len(recent_closes) >= self.consecutive_down and all_down:
            reason = (
                f"{self.consecutive_down}봉 연속 하락 — 추세 반전"
                + (f" | 수익 {pnl_pct:+.2f}%" if entry_price > 0 else "")
            )
            logger.info(f"[{symbol}] 매도신호(연속하락): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.SELL, reason=reason, indicator_values=indicators)

        # 조건 3: RSI 과매수
        if rsi >= self.rsi_overbought:
            reason = (
                f"RSI 과매수({rsi:.0f} ≥ {self.rsi_overbought})"
                + (f" | 수익 {pnl_pct:+.2f}%" if entry_price > 0 else "")
            )
            logger.info(f"[{symbol}] 매도신호(RSI): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.SELL, reason=reason, indicator_values=indicators)

        return StrategyResult(
            symbol=symbol, signal=Signal.HOLD,
            reason=f"모멘텀 유지(vol {vol_ratio:.1f}x, RSI {rsi:.0f})"
            + (f", 수익 {pnl_pct:+.2f}%" if entry_price > 0 else ""),
            indicator_values=indicators,
        )

    # ── 내부 유틸 ────────────────────────────────────────────────────────

    def _avg_volume(self, volumes: list[int]) -> float:
        """직전 N봉 평균 거래량 (현재 봉 제외)"""
        window = volumes[-_VOL_AVG_PERIODS - 1 : -1]
        return sum(window) / len(window) if window else 0.0
