"""매크로 시장 분석기 (base + claude + gemini + factory)"""
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime

from .models import MacroResult, MarketAnalysis, MarketData, neutral_macro_analysis

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "당신은 글로벌 매크로 경제 전문가이자 KOSDAQ 투자 전략가입니다. "
    "제공된 글로벌 시장 데이터와 역사적 통계를 바탕으로 "
    "오늘 KOSDAQ 시장의 방향성을 분석하고 JSON 형식으로 응답하세요."
)


class BaseMacroAnalyzer(ABC):
    @abstractmethod
    def analyze(self, market_data: MarketData, historical_ctx: str) -> MacroResult:
        ...

    def _build_prompt(self, market_data: MarketData, historical_ctx: str) -> str:
        lines = ["=== 현재 글로벌 시장 데이터 ==="]
        for idx in market_data.indices:
            sign = "+" if idx.change_pct >= 0 else ""
            lines.append(f"  {idx.name} ({idx.symbol}): {idx.price:,.2f} ({sign}{idx.change_pct:.2f}%)")
        if market_data.usd_krw:
            sign = "+" if market_data.usd_krw_change_pct >= 0 else ""
            lines.append(f"  USD/KRW: {market_data.usd_krw:,.2f} ({sign}{market_data.usd_krw_change_pct:.2f}%)")
        lines.append(f"  조회 시각: {market_data.timestamp}")
        lines.append("")
        lines.append(historical_ctx)
        lines.append("")
        lines.append(
            "위 데이터를 종합해 오늘 KOSDAQ 시장을 분석하세요.\n"
            "score: very_bullish | bullish | neutral | bearish | very_bearish\n"
            "confidence: 0.0 ~ 1.0 (분석 확신도)\n"
            "key_factors: 핵심 요인 2-3개 (리스트)\n"
            "us_overnight: 미국 증시 방향 (up | down | flat)\n"
            "reasoning: 분석 근거 2-3문장 (한국어)"
        )
        return "\n".join(lines)

    def _neutral_result(self, market_data: MarketData, provider: str) -> MacroResult:
        return MacroResult(
            analysis=neutral_macro_analysis(),
            market_data=market_data,
            provider=provider,
            updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )


class ClaudeMacroAnalyzer(BaseMacroAnalyzer):
    def __init__(self, model: str = "claude-opus-4-6"):
        self.model = model
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY 미설정 → 매크로 분석 비활성화")
            self._client = None
        else:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
            logger.info(f"Claude 매크로 분석기 초기화 완료 ({model})")

    def analyze(self, market_data: MarketData, historical_ctx: str) -> MacroResult:
        if not self._client:
            return self._neutral_result(market_data, "claude")

        prompt = self._build_prompt(market_data, historical_ctx)
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=MarketAnalysis,
            )
            analysis: MarketAnalysis = response.parsed_output
            logger.info(f"[매크로] 분석 완료: {analysis.score} (claude)")
        except Exception as e:
            logger.error(f"[매크로] Claude 분석 오류: {e}")
            return self._neutral_result(market_data, "claude")

        return MacroResult(
            analysis=analysis,
            market_data=market_data,
            provider="claude",
            updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )


class GeminiMacroAnalyzer(BaseMacroAnalyzer):
    def __init__(self, model: str = "gemini-2.0-flash"):
        self.model = model
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY 미설정 → 매크로 분석 비활성화")
            self._client = None
        else:
            try:
                from google import genai
                self._client = genai.Client(api_key=api_key)
                logger.info(f"Gemini 매크로 분석기 초기화 완료 ({model})")
            except ImportError:
                logger.error("google-genai 패키지 미설치: pip install google-genai")
                self._client = None

    def analyze(self, market_data: MarketData, historical_ctx: str) -> MacroResult:
        if not self._client:
            return self._neutral_result(market_data, "gemini")

        prompt = f"{_SYSTEM_PROMPT}\n\n{self._build_prompt(market_data, historical_ctx)}"
        response = None
        try:
            from google.genai import types
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=MarketAnalysis,
                ),
            )
            analysis = MarketAnalysis.model_validate_json(response.text)
            logger.info(f"[매크로] 분석 완료: {analysis.score} (gemini)")
        except Exception as e:
            logger.error(f"[매크로] Gemini 분석 오류: {e}")
            try:
                if response is not None:
                    raw = json.loads(response.text)
                    analysis = MarketAnalysis(**raw)
                else:
                    return self._neutral_result(market_data, "gemini")
            except Exception:
                return self._neutral_result(market_data, "gemini")

        return MacroResult(
            analysis=analysis,
            market_data=market_data,
            provider="gemini",
            updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )


def create_macro_analyzer(provider: str, model: str) -> BaseMacroAnalyzer:
    """
    provider: "gemini" | "claude" | "auto"
    auto: GEMINI_API_KEY 있으면 Gemini, 없으면 Claude, 둘 다 없으면 비활성화
    """
    if provider == "auto":
        if os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "claude"
        else:
            provider = "none"

    if provider == "gemini":
        return GeminiMacroAnalyzer(model=model or "gemini-2.0-flash")

    if provider == "claude":
        return ClaudeMacroAnalyzer(model=model or "claude-opus-4-6")

    logger.info("매크로 AI 분석 비활성화")

    class _DisabledAnalyzer(BaseMacroAnalyzer):
        def analyze(self, market_data: MarketData, historical_ctx: str) -> MacroResult:
            return self._neutral_result(market_data, "none")

    return _DisabledAnalyzer()
