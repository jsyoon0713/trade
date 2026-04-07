"""
거래량 폭발 + 가격 모멘텀 기반 단타 전략 v2

매수 조건 (모두 충족):
  1. 현재 거래량 > 평균 × volume_spike_ratio           (거래량 폭발)
  2. 최근 N봉 가격 상승률 > momentum_threshold          (상승 모멘텀)
  3. RSI < rsi_overbought                               (과매수 아님)
  4. 시초가 대비 상승폭 < max_entry_rise_pct            (추격 매수 방지)

매도 조건 (아래 중 하나):
  A. 거래량 급감(< exit_volume_ratio) AND 가격 하락     (모멘텀 소멸 확인)
  B. consecutive_down 봉 연속 하락                      (추세 반전)
  C. RSI ≥ rsi_overbought                              (과매수)
  ※ 하드 스탑(-hard_stop_pct%)은 DayTrader에서 처리
"""
import logging

from .base import BaseStrategy, Signal, StrategyResult
from .indicators import calc_rsi

logger = logging.getLogger(__name__)

_VOL_AVG_PERIODS = 10


class VolumeMomentumStrategy(BaseStrategy):
    def __init__(
        self,
        volume_spike_ratio: float = 3.0,
        exit_volume_ratio: float = 1.5,
        momentum_candles: int = 3,
        momentum_threshold: float = 0.005,
        consecutive_down: int = 3,
        rsi_overbought: float = 75.0,
        max_entry_rise_pct: float = 0.05,   # 시초가 대비 5% 이상 오른 종목 진입 차단
    ):
        self.volume_spike_ratio  = volume_spike_ratio
        self.exit_volume_ratio   = exit_volume_ratio
        self.momentum_candles    = momentum_candles
        self.momentum_threshold  = momentum_threshold
        self.consecutive_down    = consecutive_down
        self.rsi_overbought      = rsi_overbought
        self.max_entry_rise_pct  = max_entry_rise_pct

    # ── BaseStrategy 호환 ──────────────────────────────────────────────────

    def generate_signal(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        return self._check_entry(symbol, ohlcv)

    # ── 매수 신호 ─────────────────────────────────────────────────────────

    def _check_entry(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        min_len = max(_VOL_AVG_PERIODS + 1, self.momentum_candles + 2)
        if len(ohlcv) < min_len:
            return StrategyResult(symbol=symbol, signal=Signal.HOLD,
                                  reason="데이터 부족", indicator_values={})

        closes  = [c["close"]  for c in ohlcv]
        volumes = [c["volume"] for c in ohlcv]
        opens   = [c["open"]   for c in ohlcv]

        current_price = closes[-1]
        current_vol   = volumes[-1]
        avg_vol       = self._avg_volume(volumes)
        vol_ratio     = current_vol / avg_vol if avg_vol > 0 else 0.0

        # ── 추격 매수 방지: 당일 시초가 대비 상승폭 ───────────────────────
        # ohlcv[-1]["date"]는 "YYYYMMDDHHmm" 형식 (분봉: date+time 합산)
        # 오늘 날짜의 첫 번째 봉 시가를 당일 시초가로 사용
        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y%m%d")
        today_candles = [c for c in ohlcv if str(c.get("date", "")).startswith(today_str)]
        if today_candles:
            day_open = today_candles[0]["open"]   # 오늘 9:00~9:05 첫 봉 시가
        else:
            day_open = opens[0]                   # fallback: 조회 범위 첫 봉
        open_rise  = (current_price - day_open) / day_open if day_open > 0 else 0.0

        # ── 가격 모멘텀 ──────────────────────────────────────────────────
        base_price   = closes[-self.momentum_candles - 1]
        momentum_pct = (current_price - base_price) / base_price if base_price > 0 else 0.0

        # ── RSI ──────────────────────────────────────────────────────────
        try:
            rsi = calc_rsi(closes, period=7)
        except ValueError:
            rsi = 50.0

        indicators = {
            "vol_ratio":    round(vol_ratio, 2),
            "momentum_pct": round(momentum_pct * 100, 3),
            "open_rise_pct":round(open_rise * 100, 2),
            "rsi":          rsi,
        }

        # ── 매수 조건 판단 ────────────────────────────────────────────────
        vol_ok      = vol_ratio >= self.volume_spike_ratio
        momentum_ok = momentum_pct >= self.momentum_threshold
        rsi_ok      = rsi < self.rsi_overbought
        chase_ok    = open_rise < self.max_entry_rise_pct   # 추격 매수 아님

        if vol_ok and momentum_ok and rsi_ok and chase_ok:
            reason = (
                f"거래량 폭발 {vol_ratio:.1f}x "
                f"+ 모멘텀 {momentum_pct*100:+.2f}% "
                f"(시초가 대비 {open_rise*100:+.1f}%)"
            )
            logger.info(f"[{symbol}] 매수신호: {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.BUY,
                                  reason=reason, indicator_values=indicators)

        # 미충족 사유 정리
        reasons = []
        if not vol_ok:
            reasons.append(f"거래량 미충족({vol_ratio:.1f}x)")
        if not momentum_ok:
            reasons.append(f"모멘텀 부족({momentum_pct*100:+.2f}%)")
        if not rsi_ok:
            reasons.append(f"RSI 과매수({rsi:.0f})")
        if not chase_ok:
            reasons.append(f"추격매수 차단(시초가+{open_rise*100:.1f}% ≥ {self.max_entry_rise_pct*100:.0f}%)")

        return StrategyResult(symbol=symbol, signal=Signal.HOLD,
                              reason=" | ".join(reasons), indicator_values=indicators)

    # ── 매도 신호 (보유 종목용) ───────────────────────────────────────────

    def check_exit(
        self,
        symbol: str,
        ohlcv: list[dict],
        entry_price: float = 0.0,
    ) -> StrategyResult:
        """
        모멘텀 약화 기반 매도 신호.
        하드 스탑(-X%)은 DayTrader._check_exits()에서 별도 처리.
        """
        min_len = max(_VOL_AVG_PERIODS + 1, self.consecutive_down + 1)
        if len(ohlcv) < min_len:
            return StrategyResult(symbol=symbol, signal=Signal.HOLD,
                                  reason="데이터 부족 — 보유 유지", indicator_values={})

        closes  = [c["close"]  for c in ohlcv]
        volumes = [c["volume"] for c in ohlcv]

        current_price = closes[-1]
        current_vol   = volumes[-1]
        avg_vol       = self._avg_volume(volumes)
        vol_ratio     = current_vol / avg_vol if avg_vol > 0 else 0.0

        try:
            rsi = calc_rsi(closes, period=7)
        except ValueError:
            rsi = 50.0

        pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0

        indicators = {
            "vol_ratio": round(vol_ratio, 2),
            "rsi":       rsi,
            "pnl_pct":   round(pnl_pct, 2),
        }

        pnl_str = f" | 수익 {pnl_pct:+.2f}%" if entry_price > 0 else ""

        # 조건 A: 거래량 급감 AND 가격 하락 (둘 다 충족해야 청산)
        vol_drop   = avg_vol > 0 and vol_ratio < self.exit_volume_ratio
        price_down = closes[-1] < closes[-2]   # 직전 봉보다 낮음

        if vol_drop and price_down:
            reason = (
                f"거래량 급감({vol_ratio:.1f}x < {self.exit_volume_ratio}x) "
                f"+ 가격 하락 — 모멘텀 소멸{pnl_str}"
            )
            logger.info(f"[{symbol}] 매도(모멘텀소멸): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.SELL,
                                  reason=reason, indicator_values=indicators)

        # 조건 B: N봉 연속 하락
        recent = closes[-self.consecutive_down:]
        all_down = (
            len(recent) >= self.consecutive_down and
            all(recent[i] < recent[i - 1] for i in range(1, len(recent)))
        )
        if all_down:
            reason = f"{self.consecutive_down}봉 연속 하락 — 추세 반전{pnl_str}"
            logger.info(f"[{symbol}] 매도(연속하락): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.SELL,
                                  reason=reason, indicator_values=indicators)

        # 조건 C: RSI 과매수
        if rsi >= self.rsi_overbought:
            reason = f"RSI 과매수({rsi:.0f} ≥ {self.rsi_overbought}){pnl_str}"
            logger.info(f"[{symbol}] 매도(RSI): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.SELL,
                                  reason=reason, indicator_values=indicators)

        return StrategyResult(
            symbol=symbol, signal=Signal.HOLD,
            reason=f"모멘텀 유지(vol {vol_ratio:.1f}x, RSI {rsi:.0f}){pnl_str}",
            indicator_values=indicators,
        )

    # ── 내부 유틸 ─────────────────────────────────────────────────────────

    def _avg_volume(self, volumes: list[int]) -> float:
        window = volumes[-_VOL_AVG_PERIODS - 1 : -1]
        return sum(window) / len(window) if window else 0.0
