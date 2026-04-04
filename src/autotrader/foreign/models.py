"""외국인 매매동향 데이터 모델"""
from dataclasses import dataclass
from enum import Enum


class ForeignSignal(str, Enum):
    STRONG_BUY  = "strong_buy"   # 연속 N일+ 순매수
    BUY         = "buy"          # 순매수 우위
    NEUTRAL     = "neutral"      # 보합
    SELL        = "sell"         # 순매도 우위
    STRONG_SELL = "strong_sell"  # 연속 N일+ 순매도


@dataclass
class ForeignTradingData:
    symbol: str
    signal: ForeignSignal
    net_volume_5d: int      # 5일 누적 순매수 수량 (음수=순매도)
    consecutive_days: int   # 양수=연속매수일, 음수=연속매도일
    last_net_volume: int    # 최근일 순매수 수량
    ownership_pct: float    # 외국인 보유비중 (%)
    source: str = "none"    # "pykrx" | "ls_api" | "none"


def neutral_foreign_data(symbol: str) -> ForeignTradingData:
    return ForeignTradingData(
        symbol=symbol,
        signal=ForeignSignal.NEUTRAL,
        net_volume_5d=0,
        consecutive_days=0,
        last_net_volume=0,
        ownership_pct=0.0,
        source="none",
    )
