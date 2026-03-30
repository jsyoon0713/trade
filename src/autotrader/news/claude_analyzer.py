"""
Claude AI 뉴스 분석기 (Anthropic)
API 키 발급: https://console.anthropic.com
"""
import logging
import os

import anthropic

from .base_analyzer import BaseNewsAnalyzer
from .dart_fetcher import Disclosure
from .models import AnalysisResult, NewsAnalysis
from .naver_news import NewsItem

logger = logging.getLogger(__name__)


class ClaudeNewsAnalyzer(BaseNewsAnalyzer):
    def __init__(self, model: str = "claude-opus-4-6"):
        self.model = model
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY 미설정 → 뉴스 분석 비활성화")
            self._client = None
        else:
            self._client = anthropic.Anthropic(api_key=api_key)
            logger.info(f"Claude 분석기 초기화 완료 ({model})")

    def analyze(
        self,
        symbol: str,
        company_name: str,
        news_items: list[NewsItem],
        disclosures: list[Disclosure],
    ) -> AnalysisResult:
        if not self._client or (not news_items and not disclosures):
            return self._neutral_result(symbol, news_items, disclosures, "claude")

        prompt = self._build_prompt(symbol, company_name, news_items, disclosures)

        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                system=(
                    "당신은 한국 주식 시장 전문 애널리스트입니다. "
                    "주어진 뉴스와 공시를 분석하여 매매 신호에 영향을 줄 수 있는 "
                    "핵심 정보를 추출하고 JSON 형식으로 정확하게 응답하세요."
                ),
                messages=[{"role": "user", "content": prompt}],
                output_format=NewsAnalysis,
            )
            analysis: NewsAnalysis = response.parsed_output
            logger.info(f"[{symbol}] Claude 분석 완료: {analysis.sentiment}")

        except Exception as e:
            logger.error(f"[{symbol}] Claude 분석 오류: {e}")
            return self._neutral_result(symbol, news_items, disclosures, "claude")

        return AnalysisResult(
            symbol=symbol,
            analysis=analysis,
            news_count=len(news_items),
            disclosure_count=len(disclosures),
            provider="claude",
        )
