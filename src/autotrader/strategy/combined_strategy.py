"""
RSI + Claude 뉴스 분석 통합 전략

신호 결합 로직:
  RSI=BUY  + 뉴스=positive/neutral  → BUY
  RSI=BUY  + 뉴스=negative          → HOLD  (뉴스 오버라이드)
  RSI=BUY  + 뉴스=very_negative     → SELL  (강한 오버라이드)
  RSI=SELL + 뉴스=positive          → HOLD  (반등 가능성)
  RSI=SELL + 뉴스=neutral/negative  → SELL
  RSI=HOLD + 뉴스=very_positive     → BUY   (뉴스 단독 매수)
  기타                               → HOLD
"""
import logging

from .base import BaseStrategy, Signal, StrategyResult
from .rsi_strategy import RSIStrategy
from ..news.factory import create_analyzer
from ..news.models import AnalysisResult
from ..news.dart_fetcher import DARTFetcher
from ..news.naver_news import NaverNewsFetcher

logger = logging.getLogger(__name__)

# 종목코드 → 회사명 (텔레그램 알림용 간단 매핑)
_COMPANY_NAMES: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "035720": "카카오",
    "051910": "LG화학",
    "006400": "삼성SDI",
    "207940": "삼성바이오로직스",
    "005380": "현대차",
    "000270": "기아",
    "105560": "KB금융",
}


class CombinedStrategy(BaseStrategy):
    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        lookback_days: int = 3,
        block_buy_on_negative: bool = True,
        include_dart: bool = True,
        ai_provider: str = "auto",   # "gemini" | "claude" | "auto" | "none"
        ai_model: str = "",          # 비워두면 provider 기본 모델 사용
    ):
        self._rsi = RSIStrategy(rsi_period, rsi_oversold, rsi_overbought)
        self._news_fetcher = NaverNewsFetcher(max_items=10)
        self._dart_fetcher = DARTFetcher() if include_dart else None
        self._analyzer = create_analyzer(provider=ai_provider, model=ai_model)
        self.lookback_days = lookback_days
        self.block_buy_on_negative = block_buy_on_negative

    def generate_signal(self, symbol: str, ohlcv: list[dict]) -> StrategyResult:
        # 1. RSI 신호
        rsi_result = self._rsi.generate_signal(symbol, ohlcv)

        # 2. 뉴스/공시 수집 및 분석
        news_items = self._news_fetcher.fetch(symbol, self.lookback_days)
        disclosures = (
            self._dart_fetcher.fetch(symbol, self.lookback_days)
            if self._dart_fetcher else []
        )
        company_name = _COMPANY_NAMES.get(symbol, symbol)
        news_result = self._analyzer.analyze(symbol, company_name, news_items, disclosures)

        # 3. 신호 결합
        final_signal, reason = self._combine(rsi_result, news_result)

        return StrategyResult(
            symbol=symbol,
            signal=final_signal,
            reason=reason,
            indicator_values={
                **rsi_result.indicator_values,
                "news_sentiment": news_result.analysis.sentiment,
                "news_risk": news_result.analysis.risk_level,
                "news_override": news_result.analysis.override_signal,
                "news_count": news_result.news_count,
            },
        )

    def _combine(
        self, rsi_result: StrategyResult, news_result: AnalysisResult
    ) -> tuple[Signal, str]:
        rsi_signal = rsi_result.signal
        sentiment = news_result.analysis.sentiment
        override = news_result.analysis.override_signal

        # 공시/뉴스 강제 매도 (횡령, 상폐 등 악재)
        if override == "force_sell":
            reason = f"⚠️ 뉴스 강제 매도 | {news_result.analysis.reasoning}"
            logger.warning(f"[{news_result.symbol}] {reason}")
            return Signal.SELL, reason

        # RSI 매수 신호
        if rsi_signal == Signal.BUY:
            if self.block_buy_on_negative and sentiment in ("negative", "very_negative"):
                reason = (
                    f"RSI 매수 신호 차단 (뉴스 {sentiment}) | "
                    f"{rsi_result.reason} | {news_result.analysis.reasoning}"
                )
                logger.info(f"[{news_result.symbol}] {reason}")
                return Signal.HOLD, reason
            reason = f"{rsi_result.reason} | 뉴스 {sentiment}"
            return Signal.BUY, reason

        # RSI 매도 신호
        if rsi_signal == Signal.SELL:
            if sentiment == "very_positive":
                reason = (
                    f"RSI 매도 신호 완화 (뉴스 very_positive) | "
                    f"{rsi_result.reason} | {news_result.analysis.reasoning}"
                )
                logger.info(f"[{news_result.symbol}] {reason}")
                return Signal.HOLD, reason
            reason = f"{rsi_result.reason} | 뉴스 {sentiment}"
            return Signal.SELL, reason

        # RSI HOLD이지만 뉴스 매우 긍정적
        if rsi_signal == Signal.HOLD and sentiment == "very_positive":
            reason = f"뉴스 단독 매수 신호 (very_positive) | {news_result.analysis.reasoning}"
            logger.info(f"[{news_result.symbol}] {reason}")
            return Signal.BUY, reason

        # 기본: RSI HOLD
        reason = f"{rsi_result.reason} | 뉴스 {sentiment}"
        return Signal.HOLD, reason
