"""
VWAP 눌림 반등 단타 전략

[ 전략 근거 ]
VWAP(Volume Weighted Average Price)는 기관·외국인이 일중 주문 기준으로 사용하는 가격.
→ VWAP 부근 = 기관 매수 대기 구간 = 실제 지지선
→ VWAP 눌림 후 반등 = 기관 매수 재개 신호 = 저점 매수

[ 수익 구조 ]
  진입:  VWAP 눌림 반등 확인 후 매수
  손절:  진입가 - 1.0%   (DayTrader 하드스탑)
  익절:  진입가 + 2.0%   (DayTrader 익절)
  추가:  현재가 < VWAP - 0.3% 이탈 시 모멘텀 청산
  손익비: 1:2  →  필요 승률 40% (거래비용 포함 시 ≈ 45%)

[ 매수 조건 (모두 충족) ]
  1. 오늘 봉 5개 이상 (VWAP 계산 가능)
  2. 현재가 > VWAP (당일 상승 추세 유지)
  3. 직전 봉이 VWAP ± vwap_touch_pct 이내 (눌림 확인)
  4. 현재 봉이 VWAP 위로 복귀 (반등 확인)
  5. 현재가가 VWAP 대비 max_above_pct 이내 (과추격 방지)
  6. 반등 봉 거래량 >= 평균 × vol_confirm_ratio (기관 매수 확인)

[ 매도 조건 ]
  A. 현재가 < VWAP × (1 - vwap_break_pct) — VWAP 이탈, 추세 전환
  ※ 하드스탑·익절은 DayTrader._check_exits()에서 처리
"""
import logging
from datetime import datetime

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)

_MIN_TODAY_CANDLES = 5   # VWAP 계산에 필요한 최소 당일 봉 수


class VWAPPullbackStrategy(BaseStrategy):
    def __init__(
        self,
        vwap_touch_pct: float = 0.005,      # VWAP ± 0.5% 이내를 "눌림"으로 인정
        max_above_vwap_pct: float = 0.025,  # VWAP 대비 2.5% 이상 이격 시 진입 차단
        vol_confirm_ratio: float = 1.3,     # 반등 봉 거래량 >= 평균 × 1.3
        vwap_break_pct: float = 0.003,      # VWAP 아래 0.3% 이탈 시 청산
    ):
        self.vwap_touch_pct    = vwap_touch_pct
        self.max_above_vwap_pct = max_above_vwap_pct
        self.vol_confirm_ratio = vol_confirm_ratio
        self.vwap_break_pct    = vwap_break_pct

    # ── BaseStrategy 인터페이스 ────────────────────────────────────────────

    def generate_signal(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        return self._check_entry(symbol, ohlcv)

    # ── 매수 신호 ─────────────────────────────────────────────────────────

    def _check_entry(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        # 마지막 봉의 날짜를 "오늘"로 사용 — 실거래·백테스트 모두 호환
        last_date = str(ohlcv[-1].get("date", ""))[:8] if ohlcv else ""
        today_str = last_date or datetime.now().strftime("%Y%m%d")
        today = [c for c in ohlcv if str(c.get("date", "")).startswith(today_str)]

        if len(today) < _MIN_TODAY_CANDLES:
            return StrategyResult(
                symbol=symbol, signal=Signal.HOLD,
                reason=f"당일 데이터 부족 ({len(today)}봉 < {_MIN_TODAY_CANDLES}봉)",
                indicator_values={},
            )

        vwap = self._calc_vwap(today)
        if vwap <= 0:
            return StrategyResult(symbol=symbol, signal=Signal.HOLD,
                                  reason="VWAP 계산 불가", indicator_values={})

        closes  = [c["close"]  for c in today]
        volumes = [c["volume"] for c in today]

        current_price = closes[-1]
        prev_price    = closes[-2] if len(closes) >= 2 else current_price

        # 평균 거래량: 당일 직전 봉들 (현재 봉 제외)
        past_vols = volumes[:-1]
        avg_vol   = sum(past_vols) / len(past_vols) if past_vols else 0
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        above_vwap_pct = (current_price - vwap) / vwap  # VWAP 대비 이격률

        indicators = {
            "vwap":         round(vwap, 0),
            "above_vwap":   round(above_vwap_pct * 100, 2),  # % 단위
            "prev_close":   prev_price,
            "vol_ratio":    round(vol_ratio, 2),
        }

        # ── 조건 판단 ──────────────────────────────────────────────────
        # 1. 현재가 > VWAP (상승 추세)
        cond_above = current_price > vwap

        # 2. 직전 봉이 VWAP ± touch_pct 이내 (눌림 확인)
        cond_touched = abs(prev_price - vwap) / vwap <= self.vwap_touch_pct

        # 3. 현재 봉이 VWAP 위 (반등 확인) — prev가 VWAP 아래였다가 현재 위
        cond_bounce = prev_price <= vwap * (1 + self.vwap_touch_pct) and current_price > vwap

        # 4. VWAP 대비 과도한 이격 아님
        cond_not_far = 0 < above_vwap_pct <= self.max_above_vwap_pct

        # 5. 거래량 확인
        cond_vol = vol_ratio >= self.vol_confirm_ratio

        if cond_above and cond_touched and cond_bounce and cond_not_far and cond_vol:
            reason = (
                f"VWAP({vwap:,.0f}) 눌림 반등 | "
                f"이격 +{above_vwap_pct*100:.2f}% | "
                f"vol {vol_ratio:.1f}x"
            )
            logger.info(f"[{symbol}] 매수신호(VWAP): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.BUY,
                                  reason=reason, indicator_values=indicators)

        # 미충족 사유 정리
        reasons = []
        if not cond_above:
            reasons.append(f"VWAP 하향({current_price:,} ≤ {vwap:,.0f})")
        elif not cond_touched:
            reasons.append(f"눌림 없음(직전 이격 {abs(prev_price-vwap)/vwap*100:.1f}%)")
        elif not cond_bounce:
            reasons.append(f"반등 미확인(prev {prev_price:,})")
        if not cond_not_far:
            if above_vwap_pct > self.max_above_vwap_pct:
                reasons.append(f"VWAP 과이탈(+{above_vwap_pct*100:.1f}% > {self.max_above_vwap_pct*100:.0f}%)")
            elif above_vwap_pct <= 0:
                reasons.append(f"VWAP 하방")
        if not cond_vol:
            reasons.append(f"거래량 미확인({vol_ratio:.1f}x < {self.vol_confirm_ratio}x)")

        return StrategyResult(symbol=symbol, signal=Signal.HOLD,
                              reason=" | ".join(reasons) or "조건 미충족",
                              indicator_values=indicators)

    # ── 매도 신호 (보유 종목용) ───────────────────────────────────────────

    def check_exit(
        self,
        symbol: str,
        ohlcv: list[dict],
        entry_price: float = 0.0,
    ) -> StrategyResult:
        """
        VWAP 이탈 시 청산.
        하드스탑·익절은 DayTrader._check_exits()에서 먼저 처리됨.
        """
        last_date = str(ohlcv[-1].get("date", ""))[:8] if ohlcv else ""
        today_str = last_date or datetime.now().strftime("%Y%m%d")
        today = [c for c in ohlcv if str(c.get("date", "")).startswith(today_str)]

        if len(today) < 2:
            return StrategyResult(symbol=symbol, signal=Signal.HOLD,
                                  reason="당일 데이터 부족 — 보유 유지", indicator_values={})

        vwap = self._calc_vwap(today)
        current_price = today[-1]["close"]
        pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

        indicators = {
            "vwap":     round(vwap, 0),
            "pnl_pct":  round(pnl_pct, 2),
        }

        # VWAP 아래 0.3% 이탈 → 추세 전환, 청산
        if vwap > 0 and current_price < vwap * (1 - self.vwap_break_pct):
            reason = (
                f"VWAP({vwap:,.0f}) 이탈 | "
                f"현재 {current_price:,} | 손익 {pnl_pct:+.2f}%"
            )
            logger.info(f"[{symbol}] 매도(VWAP이탈): {reason}")
            return StrategyResult(symbol=symbol, signal=Signal.SELL,
                                  reason=reason, indicator_values=indicators)

        return StrategyResult(
            symbol=symbol, signal=Signal.HOLD,
            reason=f"VWAP 위 유지({current_price:,} > {vwap:,.0f}) | {pnl_pct:+.2f}%",
            indicator_values=indicators,
        )

    # ── 내부 유틸 ─────────────────────────────────────────────────────────

    @staticmethod
    def _calc_vwap(today_ohlcv: list[dict]) -> float:
        """당일 VWAP = Σ(대표가 × 거래량) / Σ거래량  (대표가 = (H+L+C)/3)"""
        total_pv  = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"]
                        for c in today_ohlcv)
        total_vol = sum(c["volume"] for c in today_ohlcv)
        return total_pv / total_vol if total_vol > 0 else 0.0
