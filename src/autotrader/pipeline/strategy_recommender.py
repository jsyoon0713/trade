"""
시장 상황 기반 투자 방식 추천기

매크로 점수 + 파이프라인 종목 점수를 종합하여
단타 투자 방식(집중/분산/소극적/관망)을 추천한다.

투자 모드:
  FOCUSED    - 집중투자: 최상위 1개 종목에 시드의 80%
  WEIGHTED   - 비중분산: 상위 2~3개 종목에 점수 비례 배분
  BALANCED   - 균형분산: 상위 2~3개 종목에 균등 배분
  DEFENSIVE  - 소극적:  1~2개 종목에 소액(최소 order_amount)
  HOLD       - 관망:    신규 매수 없음
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stock_analyzer import StockScore

# 매크로 점수 → 숫자 (높을수록 강세)
_MACRO_SCORE_MAP = {
    "very_bullish": 5,
    "bullish":      4,
    "neutral":      3,
    "bearish":      2,
    "very_bearish": 1,
}

MODES = {
    "FOCUSED":   "집중투자",
    "WEIGHTED":  "비중분산",
    "BALANCED":  "균형분산",
    "DEFENSIVE": "소극적",
    "HOLD":      "관망",
}

MODE_DESC = {
    "FOCUSED":   "최고점 종목 1개에 시드 80% 집중 투입",
    "WEIGHTED":  "상위 2~3개 종목에 점수 비례 자금 배분",
    "BALANCED":  "상위 2~3개 종목에 균등하게 자금 배분",
    "DEFENSIVE": "상위 1~2개 종목에 최소 단위로 소액 투자",
    "HOLD":      "시장 불확실성 높음 — 신규 매수 없음",
}


@dataclass
class ModeAllocation:
    """투자 모드에 따른 종목별 투자 금액 계획"""
    symbol: str
    company_name: str
    pipeline_score: int
    amount: int          # 투입 금액 (원)
    ratio: float         # 시드 대비 비율 (0~1)


@dataclass
class Recommendation:
    mode: str                                  # FOCUSED / WEIGHTED / BALANCED / DEFENSIVE / HOLD
    mode_label: str                            # 한글 레이블
    description: str                           # 설명
    macro_score: str                           # very_bullish … very_bearish
    top_stock_score: int                       # 최고점 종목 파이프라인 점수
    buy_candidates: int                        # 매수 추천 종목 수
    reasoning: str                             # 추천 이유
    allocations: list[ModeAllocation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode":            self.mode,
            "mode_label":      self.mode_label,
            "description":     self.description,
            "macro_score":     self.macro_score,
            "top_stock_score": self.top_stock_score,
            "buy_candidates":  self.buy_candidates,
            "reasoning":       self.reasoning,
            "allocations": [
                {
                    "symbol":         a.symbol,
                    "company_name":   a.company_name,
                    "pipeline_score": a.pipeline_score,
                    "amount":         a.amount,
                    "ratio":          round(a.ratio * 100, 1),
                }
                for a in self.allocations
            ],
        }

    def summary_text(self) -> str:
        lines = [
            f"  추천 모드: [{self.mode_label}] — {self.description}",
            f"  근거: {self.reasoning}",
        ]
        if self.allocations:
            lines.append("  자금 배분 계획:")
            for a in self.allocations:
                lines.append(
                    f"    {a.symbol}({a.company_name}) "
                    f"점수 {a.pipeline_score}점 → {a.amount:,}원 ({a.ratio*100:.0f}%)"
                )
        return "\n".join(lines)


def recommend(
    scored: list["StockScore"],
    capital: int,
    order_amount: int,
    macro_score: str = "neutral",
) -> Recommendation:
    """
    시장 상황 + 파이프라인 결과 → 투자 방식 추천

    scored      : StockAnalyzer.run() 결과 (점수 내림차순)
    capital     : 단타 시드머니
    order_amount: 기본 1회 매수금액
    macro_score : 매크로 분석 점수 문자열
    """
    macro_num = _MACRO_SCORE_MAP.get(macro_score, 3)

    # 매수 추천 종목 필터
    buys = [s for s in scored if s.decision in ("매수", "강력매수")]
    strong_buys = [s for s in buys if s.decision == "강력매수"]
    top_score = buys[0].total_score if buys else 0

    # ── 모드 결정 로직 ──────────────────────────────────────────────────────
    if macro_num <= 1:
        # 매우 약세: 관망
        mode = "HOLD"
        reasoning = f"매크로 {macro_score} — 시장 전반 하락 위험, 신규 진입 자제"

    elif macro_num == 2:
        # 약세: 소극적 대응
        if top_score >= 65:
            mode = "DEFENSIVE"
            reasoning = f"매크로 약세이나 최고점 종목({top_score}점) 소액 진입 검토"
        else:
            mode = "HOLD"
            reasoning = f"매크로 약세({macro_score}) + 종목 점수 부족({top_score}점) — 관망"

    elif macro_num == 3:
        # 중립
        if len(strong_buys) >= 2:
            mode = "BALANCED"
            reasoning = f"중립 시장, 강력매수 {len(strong_buys)}개 — 균등 분산으로 리스크 관리"
        elif top_score >= 70:
            mode = "WEIGHTED"
            reasoning = f"중립 시장, 최고점 {top_score}점 — 점수 비중 분산"
        elif top_score >= 50:
            mode = "DEFENSIVE"
            reasoning = f"중립 시장, 종목 점수 보통({top_score}점) — 소액 진입"
        else:
            mode = "HOLD"
            reasoning = f"중립 시장, 유효 종목 없음({top_score}점) — 관망"

    elif macro_num == 4:
        # 강세
        if top_score >= 75 and len(strong_buys) == 1:
            mode = "FOCUSED"
            reasoning = f"강세 시장 + 강력매수 단독 {top_score}점 — 집중 투자 유리"
        elif len(buys) >= 2:
            mode = "WEIGHTED"
            reasoning = f"강세 시장, 매수 종목 {len(buys)}개 — 점수 비중 분산"
        else:
            mode = "BALANCED"
            reasoning = f"강세 시장, 매수 종목 {len(buys)}개 — 균등 분산"

    else:
        # 매우 강세 (5)
        if top_score >= 75:
            mode = "FOCUSED"
            reasoning = f"매우강세 시장 + 최고점 {top_score}점 — 집중 투자로 수익 극대화"
        elif len(buys) >= 2:
            mode = "WEIGHTED"
            reasoning = f"매우강세 시장, 매수 종목 {len(buys)}개 — 점수 비중 분산"
        else:
            mode = "BALANCED"
            reasoning = "매우강세 시장, 유효 종목 소수 — 균등 분산"

    # ── 자금 배분 계산 ──────────────────────────────────────────────────────
    allocations = _calc_allocations(mode, buys, capital, order_amount)

    return Recommendation(
        mode=mode,
        mode_label=MODES[mode],
        description=MODE_DESC[mode],
        macro_score=macro_score,
        top_stock_score=top_score,
        buy_candidates=len(buys),
        reasoning=reasoning,
        allocations=allocations,
    )


def _calc_allocations(
    mode: str,
    buys: list["StockScore"],
    capital: int,
    order_amount: int,
) -> list[ModeAllocation]:
    if not buys or mode == "HOLD":
        return []

    allocs: list[ModeAllocation] = []

    if mode == "FOCUSED":
        top = buys[0]
        amount = int(capital * 0.80)
        allocs.append(ModeAllocation(top.symbol, top.company_name, top.total_score, amount, 0.80))

    elif mode == "WEIGHTED":
        targets = buys[:3]
        total_score = sum(s.total_score for s in targets) or 1
        usable = int(capital * 0.85)
        for s in targets:
            ratio = s.total_score / total_score
            amount = max(order_amount, int(usable * ratio))
            allocs.append(ModeAllocation(s.symbol, s.company_name, s.total_score, amount, amount / capital))

    elif mode == "BALANCED":
        targets = buys[:3]
        n = len(targets)
        amount_each = min(int(capital * 0.80 / n), capital)
        for s in targets:
            allocs.append(ModeAllocation(s.symbol, s.company_name, s.total_score, amount_each, amount_each / capital))

    elif mode == "DEFENSIVE":
        targets = buys[:2]
        for s in targets:
            amount = order_amount  # 기본 주문 금액만
            allocs.append(ModeAllocation(s.symbol, s.company_name, s.total_score, amount, amount / capital))

    return allocs


def apply_allocation(
    daytrader,
    recommendation: Recommendation,
) -> None:
    """
    추천 배분을 DayTrader에 적용:
    - _watchlist 재설정
    - _pipeline_scores의 amount 정보를 이용해 position_size 오버라이드
    """
    if recommendation.mode == "HOLD":
        daytrader._watchlist = []
        daytrader._mode_allocations = {}
        return

    symbols = [a.symbol for a in recommendation.allocations]
    amounts = {a.symbol: a.amount for a in recommendation.allocations}

    daytrader._watchlist = symbols
    daytrader._mode_allocations = amounts  # _position_size()에서 참조
