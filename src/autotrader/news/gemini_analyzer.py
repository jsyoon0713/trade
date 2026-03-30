"""
Google Gemini 뉴스 분석기
무료 티어: Gemini 2.0 Flash 1,500회/일
API 키 발급: https://aistudio.google.com/apikey
"""
import json
import logging
import os

from .base_analyzer import BaseNewsAnalyzer
from .dart_fetcher import Disclosure
from .models import AnalysisResult, NewsAnalysis
from .naver_news import NewsItem

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "당신은 한국 주식 시장 전문 애널리스트입니다. "
    "주어진 뉴스와 공시를 분석하여 매매 신호에 영향을 줄 수 있는 "
    "핵심 정보를 추출하고 반드시 JSON 형식으로만 응답하세요. "
    "다른 텍스트 없이 JSON만 출력하세요."
)

_JSON_SCHEMA = """
{
  "sentiment": "very_positive | positive | neutral | negative | very_negative",
  "risk_level": "low | medium | high",
  "key_events": ["이벤트1", "이벤트2"],
  "override_signal": "none | block_buy | force_sell",
  "reasoning": "분석 근거 2-3문장"
}
"""


class GeminiNewsAnalyzer(BaseNewsAnalyzer):
    def __init__(self, model: str = "gemini-2.0-flash"):
        self.model = model
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY 미설정 → 뉴스 분석 비활성화")
            self._client = None
        else:
            try:
                from google import genai
                self._client = genai.Client(api_key=api_key)
                logger.info(f"Gemini 분석기 초기화 완료 ({model})")
            except ImportError:
                logger.error("google-genai 패키지 미설치: pip install google-genai")
                self._client = None

    def analyze(
        self,
        symbol: str,
        company_name: str,
        news_items: list[NewsItem],
        disclosures: list[Disclosure],
    ) -> AnalysisResult:
        if not self._client or (not news_items and not disclosures):
            return self._neutral_result(symbol, news_items, disclosures, "gemini")

        prompt = (
            f"{_SYSTEM_PROMPT}\n\n"
            f"{self._build_prompt(symbol, company_name, news_items, disclosures)}\n\n"
            f"응답 JSON 스키마:\n{_JSON_SCHEMA}"
        )

        try:
            from google.genai import types

            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=NewsAnalysis,
                ),
            )
            analysis = NewsAnalysis.model_validate_json(response.text)
            logger.info(f"[{symbol}] Gemini 분석 완료: {analysis.sentiment}")

        except Exception as e:
            logger.error(f"[{symbol}] Gemini 분석 오류: {e}")
            # JSON 파싱 fallback
            try:
                raw = json.loads(response.text)
                analysis = NewsAnalysis(**raw)
            except Exception:
                return self._neutral_result(symbol, news_items, disclosures, "gemini")

        return AnalysisResult(
            symbol=symbol,
            analysis=analysis,
            news_count=len(news_items),
            disclosure_count=len(disclosures),
            provider="gemini",
        )
