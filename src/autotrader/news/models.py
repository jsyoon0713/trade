"""뉴스 분석 공통 데이터 모델"""
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel


class NewsSentiment(str, Enum):
    VERY_POSITIVE = "very_positive"
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    VERY_NEGATIVE = "very_negative"


class NewsAnalysis(BaseModel):
    sentiment: str          # NewsSentiment 값
    risk_level: str         # "low" | "medium" | "high"
    key_events: list[str]   # 핵심 이벤트 요약 (최대 3개)
    override_signal: str    # "none" | "block_buy" | "force_sell"
    reasoning: str          # 분석 근거 (한국어, 2-3문장)


@dataclass
class AnalysisResult:
    symbol: str
    analysis: NewsAnalysis
    news_count: int
    disclosure_count: int
    provider: str = "none"  # "claude" | "gemini" | "none"

    @property
    def should_block_buy(self) -> bool:
        return (
            self.analysis.override_signal == "block_buy"
            or self.analysis.sentiment == "very_negative"
        )

    @property
    def should_force_sell(self) -> bool:
        return self.analysis.override_signal == "force_sell"

    def summary(self) -> str:
        sentiment_emoji = {
            "very_positive": "🟢🟢",
            "positive": "🟢",
            "neutral": "⚪",
            "negative": "🔴",
            "very_negative": "🔴🔴",
        }
        emoji = sentiment_emoji.get(self.analysis.sentiment, "⚪")
        events = " | ".join(self.analysis.key_events) if self.analysis.key_events else "특이사항 없음"
        return (
            f"{emoji} [{self.symbol}] 뉴스 감성: {self.analysis.sentiment} ({self.provider})\n"
            f"  핵심: {events}\n"
            f"  근거: {self.analysis.reasoning}"
        )


def neutral_analysis() -> NewsAnalysis:
    return NewsAnalysis(
        sentiment="neutral",
        risk_level="low",
        key_events=[],
        override_signal="none",
        reasoning="뉴스 분석 불가 또는 특이사항 없음",
    )
