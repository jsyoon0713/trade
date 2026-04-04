"""매크로 시장 분석 데이터 모델"""
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel


class MarketScore(str, Enum):
    VERY_BULLISH = "very_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    VERY_BEARISH = "very_bearish"


class MarketAnalysis(BaseModel):
    score: str          # MarketScore 값
    confidence: float   # 0.0 ~ 1.0
    key_factors: list[str]  # 핵심 요인 (최대 3개)
    us_overnight: str   # "up" | "down" | "flat"
    reasoning: str      # 분석 근거 (한국어, 2-3문장)


@dataclass
class IndexSnapshot:
    symbol: str
    name: str
    price: float
    change_pct: float   # 전일 대비 %


@dataclass
class MarketData:
    indices: list = field(default_factory=list)  # list[IndexSnapshot]
    usd_krw: float = 0.0
    usd_krw_change_pct: float = 0.0
    timestamp: str = ""


@dataclass
class MacroResult:
    analysis: MarketAnalysis
    market_data: MarketData
    provider: str = "none"
    updated_at: str = ""


def neutral_macro_analysis() -> MarketAnalysis:
    return MarketAnalysis(
        score="neutral",
        confidence=0.5,
        key_factors=[],
        us_overnight="flat",
        reasoning="매크로 분석 불가 또는 데이터 없음",
    )
