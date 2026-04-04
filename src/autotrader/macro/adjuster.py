"""매크로 점수에 따른 트레이더 파라미터 자동 조정"""
import logging
from dataclasses import dataclass

from .models import MarketScore

logger = logging.getLogger(__name__)

# 점수별 배수 테이블
# max_pos_op: ("add", n) → base + n  |  ("div", n) → base // n
_MULTIPLIERS = {
    MarketScore.VERY_BULLISH: dict(order_amount=1.5,  max_pos_op=("add", +1), stop_loss=1.0, take_profit=1.3),
    MarketScore.BULLISH:      dict(order_amount=1.2,  max_pos_op=("add",  0), stop_loss=1.0, take_profit=1.15),
    MarketScore.NEUTRAL:      dict(order_amount=1.0,  max_pos_op=("add",  0), stop_loss=1.0, take_profit=1.0),
    MarketScore.BEARISH:      dict(order_amount=0.7,  max_pos_op=("add", -1), stop_loss=0.7, take_profit=1.0),
    MarketScore.VERY_BEARISH: dict(order_amount=0.3,  max_pos_op=("div",  2), stop_loss=0.5, take_profit=1.0),
}


def _calc_max_pos(base: int, op: tuple) -> int:
    action, val = op
    if action == "div":
        return max(1, base // val)
    return max(1, base + val)


@dataclass
class _BaseParams:
    order_amount: int
    max_positions: int
    stop_loss_pct: float
    take_profit_pct: float


class ParameterAdjuster:
    """최초 호출 시 base 값 캡처 → 매크로 점수에 따라 트레이더 파라미터 조정"""

    def __init__(self) -> None:
        self._sw_base: _BaseParams | None = None
        self._dt_base: _BaseParams | None = None

    def apply(self, score_str: str, swing_trader, day_trader) -> None:
        try:
            score = MarketScore(score_str)
        except ValueError:
            score = MarketScore.NEUTRAL

        mult = _MULTIPLIERS[score]

        if swing_trader is not None:
            if self._sw_base is None:
                self._sw_base = _BaseParams(
                    order_amount=swing_trader.order_amount,
                    max_positions=swing_trader.max_positions,
                    stop_loss_pct=swing_trader.risk_manager.stop_loss_pct,
                    take_profit_pct=swing_trader.risk_manager.take_profit_pct,
                )

            new_amount = int(self._sw_base.order_amount * mult["order_amount"])
            new_max = _calc_max_pos(self._sw_base.max_positions, mult["max_pos_op"])
            new_sl = round(self._sw_base.stop_loss_pct * mult["stop_loss"], 2)
            new_tp = round(self._sw_base.take_profit_pct * mult["take_profit"], 2)

            swing_trader.order_amount = new_amount
            swing_trader.max_positions = new_max
            swing_trader.risk_manager.stop_loss_pct = new_sl
            swing_trader.risk_manager.take_profit_pct = new_tp
            logger.info(
                f"[매크로] 스윙 파라미터 조정: 점수={score.value} | "
                f"order_amount={new_amount:,} | max_pos={new_max} | "
                f"sl={new_sl}% | tp={new_tp}%"
            )

        if day_trader is not None:
            if self._dt_base is None:
                self._dt_base = _BaseParams(
                    order_amount=day_trader.order_amount,
                    max_positions=day_trader.max_positions,
                    stop_loss_pct=day_trader.stop_loss_pct,
                    take_profit_pct=day_trader.take_profit_pct,
                )

            new_amount = int(self._dt_base.order_amount * mult["order_amount"])
            new_max = _calc_max_pos(self._dt_base.max_positions, mult["max_pos_op"])
            new_sl = round(self._dt_base.stop_loss_pct * mult["stop_loss"], 2)
            new_tp = round(self._dt_base.take_profit_pct * mult["take_profit"], 2)

            day_trader.order_amount = new_amount
            day_trader.max_positions = new_max
            day_trader.stop_loss_pct = new_sl
            day_trader.take_profit_pct = new_tp
            logger.info(
                f"[매크로] 단타 파라미터 조정: 점수={score.value} | "
                f"order_amount={new_amount:,} | max_pos={new_max} | "
                f"sl={new_sl}% | tp={new_tp}%"
            )
