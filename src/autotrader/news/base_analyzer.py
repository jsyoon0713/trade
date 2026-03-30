"""뉴스 분석기 추상 기본 클래스"""
from abc import ABC, abstractmethod

from .dart_fetcher import Disclosure
from .models import AnalysisResult, NewsAnalysis, neutral_analysis
from .naver_news import NewsItem


class BaseNewsAnalyzer(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        company_name: str,
        news_items: list[NewsItem],
        disclosures: list[Disclosure],
    ) -> AnalysisResult:
        ...

    def _build_prompt(
        self,
        symbol: str,
        company_name: str,
        news_items: list[NewsItem],
        disclosures: list[Disclosure],
    ) -> str:
        parts = [
            f"종목: {company_name} ({symbol})\n"
            f"분석일: 최근 {len(news_items)}건 뉴스, {len(disclosures)}건 공시\n"
        ]
        if news_items:
            parts.append("=== 최근 뉴스 ===")
            for i, item in enumerate(news_items[:8], 1):
                parts.append(f"{i}. [{item.published_at}] {item.title} ({item.source})")
                if item.summary:
                    parts.append(f"   요약: {item.summary[:150]}")

        if disclosures:
            parts.append("\n=== DART 공시 ===")
            for i, d in enumerate(disclosures[:5], 1):
                parts.append(f"{i}. [{d.filed_at}] {d.title} ({d.report_type})")

        parts.append(
            "\n위 뉴스와 공시를 분석해주세요. 특히 다음을 중점 확인하세요:\n"
            "- 실적 서프라이즈/쇼크\n"
            "- 횡령, 배임, 소송, 제재\n"
            "- M&A, 사업 확장, 신사업\n"
            "- 대규모 계약 체결/해지\n"
            "- 경영진 변경\n"
            "- 업황 변화 (반도체, 2차전지 등 섹터 이슈)"
        )
        return "\n".join(parts)

    def _neutral_result(
        self,
        symbol: str,
        news_items: list[NewsItem],
        disclosures: list[Disclosure],
        provider: str = "none",
    ) -> AnalysisResult:
        return AnalysisResult(
            symbol=symbol,
            analysis=neutral_analysis(),
            news_count=len(news_items),
            disclosure_count=len(disclosures),
            provider=provider,
        )
