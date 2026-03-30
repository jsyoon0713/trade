"""
AI 뉴스 분석기 팩토리
settings.yaml의 provider 설정에 따라 자동 선택
"""
import logging

from .base_analyzer import BaseNewsAnalyzer

logger = logging.getLogger(__name__)


def create_analyzer(provider: str, model: str) -> BaseNewsAnalyzer:
    """
    provider: "gemini" | "claude" | "auto"
    auto: GEMINI_API_KEY 있으면 Gemini, 없으면 Claude, 둘 다 없으면 비활성화
    """
    if provider == "auto":
        import os
        if os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "claude"
        else:
            provider = "none"

    if provider == "gemini":
        from .gemini_analyzer import GeminiNewsAnalyzer
        return GeminiNewsAnalyzer(model=model or "gemini-2.0-flash")

    if provider == "claude":
        from .claude_analyzer import ClaudeNewsAnalyzer
        return ClaudeNewsAnalyzer(model=model or "claude-opus-4-6")

    # provider == "none" 또는 미지원
    logger.info("AI 뉴스 분석 비활성화")
    from .base_analyzer import BaseNewsAnalyzer
    from .models import AnalysisResult

    class _DisabledAnalyzer(BaseNewsAnalyzer):
        def analyze(self, symbol, company_name, news_items, disclosures) -> AnalysisResult:
            return self._neutral_result(symbol, news_items, disclosures, "none")

    return _DisabledAnalyzer()
