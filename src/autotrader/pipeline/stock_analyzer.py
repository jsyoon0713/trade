"""
종목 분석 파이프라인
  1단계: 종목 선정  — 거래량 상위 스캔
  2단계: 차트 분석  — RSI, MACD, 볼린저밴드, 이동평균 정배열
  3단계: 수익률 분석 — 3개월 미니 백테스트 (yfinance)
  4단계: 투자 결정  — 종합 점수(0-100) → 강력매수/매수/관망/제외
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..broker.ls_broker import LSBroker
from ..scanner.stock_scanner import StockScanner
from ..strategy.indicators import (
    calc_rsi,
    calc_moving_average,
    calc_bollinger_bands,
    calc_macd,
)
from ..backtest.engine import BacktestEngine
from ..strategy.rsi_strategy import RSIStrategy

logger = logging.getLogger(__name__)

# 종목코드 → 회사명
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
    "055550": "신한지주",
    "086790": "하나금융지주",
    "003550": "LG",
    "012330": "현대모비스",
    "028260": "삼성물산",
}


@dataclass
class StockScore:
    symbol: str
    company_name: str
    current_price: float

    # 차트 지표 원값
    rsi: float = 0.0
    macd_histogram: float = 0.0
    bb_position: float = 0.5   # 0=하단, 1=상단
    ma5: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0

    # 차트 세부 점수 (각 0-25)
    rsi_score: int = 0
    macd_score: int = 0
    bb_score: int = 0
    ma_score: int = 0
    chart_score: int = 0       # 합계 0-100

    # 백테스트 결과
    backtest_return: float = 0.0
    backtest_win_rate: float = 0.0
    backtest_trades: int = 0
    backtest_score: int = 0    # 0-100

    # 종합
    total_score: int = 0       # 0-100
    decision: str = "관망"    # 강력매수 / 매수 / 관망 / 제외
    reason: str = ""

    # 내부용
    _reasons: list = field(default_factory=list, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "current_price": self.current_price,
            "rsi": round(self.rsi, 1),
            "macd_histogram": round(self.macd_histogram, 4),
            "bb_position": round(self.bb_position, 2),
            "ma5": self.ma5,
            "ma20": self.ma20,
            "ma60": self.ma60,
            "rsi_score": self.rsi_score,
            "macd_score": self.macd_score,
            "bb_score": self.bb_score,
            "ma_score": self.ma_score,
            "chart_score": self.chart_score,
            "backtest_return": round(self.backtest_return, 2),
            "backtest_win_rate": round(self.backtest_win_rate, 1),
            "backtest_trades": self.backtest_trades,
            "backtest_score": self.backtest_score,
            "total_score": self.total_score,
            "decision": self.decision,
            "reason": self.reason,
        }


class StockAnalyzer:
    """
    4단계 종목 분석 파이프라인.

    사용법:
        analyzer = StockAnalyzer(broker)
        results = analyzer.run()          # 자동 스캔 후 전체 분석
        top = analyzer.run(top_n=5)       # 상위 5개만 반환
        results = analyzer.run(["005930", "000660"])  # 특정 종목 분석
    """

    def __init__(
        self,
        broker: LSBroker,
        scan_top_n: int = 30,
        backtest_months: int = 3,
        min_score_to_include: int = 40,
    ):
        self.broker = broker
        self.scanner = StockScanner(broker)
        self.scan_top_n = scan_top_n
        self.backtest_months = backtest_months
        self.min_score_to_include = min_score_to_include

    def run(
        self,
        symbols: list[str] | None = None,
        top_n: int | None = None,
    ) -> list[StockScore]:
        """
        파이프라인 실행.
        symbols=None  → 자동 스캔
        top_n         → 상위 N개만 반환 (None이면 전체)
        """
        # 1단계: 종목 선정
        if symbols is None:
            logger.info(f"[파이프라인] 1단계: 거래량 상위 {self.scan_top_n}개 스캔")
            symbols = self.scanner.get_top_volume_stocks(self.scan_top_n)

        if not symbols:
            logger.warning("[파이프라인] 후보 종목 없음")
            return []

        logger.info(f"[파이프라인] 분석 대상 {len(symbols)}개: {symbols}")
        results: list[StockScore] = []

        for symbol in symbols:
            try:
                score = self._analyze(symbol)
                results.append(score)
                logger.info(
                    f"[파이프라인][{symbol}] 차트={score.chart_score} "
                    f"백테={score.backtest_score} 종합={score.total_score} "
                    f"→ {score.decision}"
                )
            except Exception as e:
                logger.warning(f"[파이프라인][{symbol}] 분석 실패: {e}")

        # 점수 내림차순 정렬
        results.sort(key=lambda x: x.total_score, reverse=True)

        if top_n is not None:
            results = results[:top_n]

        return results

    def get_buy_candidates(self, symbols: list[str] | None = None) -> list[str]:
        """매수 또는 강력매수 결정 종목 코드 리스트만 반환"""
        results = self.run(symbols)
        return [r.symbol for r in results if r.decision in ("매수", "강력매수")]

    # ── 단일 종목 분석 ────────────────────────────────────────────────────────

    def _analyze(self, symbol: str) -> StockScore:
        ohlcv = self.broker.get_ohlcv(symbol, period="D", count=60)
        if not ohlcv:
            raise ValueError("OHLCV 데이터 없음")

        closes = [c["close"] for c in ohlcv]
        score = StockScore(
            symbol=symbol,
            company_name=_COMPANY_NAMES.get(symbol, symbol),
            current_price=closes[-1],
        )

        self._chart_analysis(score, closes)
        self._backtest_analysis(score, symbol)
        self._finalize(score)
        return score

    # ── 2단계: 차트 분석 ──────────────────────────────────────────────────────

    def _chart_analysis(self, score: StockScore, closes: list[float]) -> None:
        reasons: list[str] = []

        # RSI (0-25점)
        try:
            rsi = calc_rsi(closes, period=14)
            score.rsi = rsi
            if rsi < 30:
                score.rsi_score = 25
                reasons.append(f"RSI 과매도({rsi:.0f})")
            elif rsi < 40:
                score.rsi_score = 20
                reasons.append(f"RSI 매수권({rsi:.0f})")
            elif rsi < 50:
                score.rsi_score = 12
            elif rsi < 65:
                score.rsi_score = 5
            else:
                score.rsi_score = 0
                reasons.append(f"RSI 과매수({rsi:.0f})")
        except Exception:
            pass

        # MACD (0-25점)
        try:
            macd = calc_macd(closes)
            score.macd_histogram = macd["histogram"]
            if macd["macd"] > macd["signal"] and macd["histogram"] > 0:
                score.macd_score = 25
                reasons.append("MACD 골든크로스↑")
            elif macd["macd"] > macd["signal"]:
                score.macd_score = 15
                reasons.append("MACD 매수우위")
            elif macd["histogram"] > 0:
                score.macd_score = 8
                reasons.append("MACD 반등조짐")
            else:
                score.macd_score = 0
        except Exception:
            pass

        # 볼린저밴드 (0-25점)
        try:
            bb = calc_bollinger_bands(closes, period=20)
            price = closes[-1]
            band_width = bb["upper"] - bb["lower"]
            if band_width > 0:
                pos = (price - bb["lower"]) / band_width
                score.bb_position = pos
                if pos < 0.15:
                    score.bb_score = 25
                    reasons.append("BB 하단 터치(강한반등기대)")
                elif pos < 0.35:
                    score.bb_score = 18
                    reasons.append("BB 하단권")
                elif pos > 0.85:
                    score.bb_score = 0
                    reasons.append("BB 상단 과열")
                else:
                    score.bb_score = 10
        except Exception:
            pass

        # 이동평균 정배열 (0-25점)
        try:
            ma5 = calc_moving_average(closes, 5)
            ma20 = calc_moving_average(closes, 20)
            ma60 = calc_moving_average(closes, min(60, len(closes)))
            price = closes[-1]
            score.ma5, score.ma20, score.ma60 = ma5, ma20, ma60

            if price > ma5 > ma20 > ma60:
                score.ma_score = 25
                reasons.append("정배열 강세")
            elif price > ma5 > ma20:
                score.ma_score = 20
                reasons.append("단기 정배열")
            elif price > ma20:
                score.ma_score = 12
            elif price < ma5 < ma20 < ma60:
                score.ma_score = 0
                reasons.append("역배열 약세")
            else:
                score.ma_score = 5
        except Exception:
            pass

        score.chart_score = score.rsi_score + score.macd_score + score.bb_score + score.ma_score
        score._reasons = reasons

    # ── 3단계: 백테스트 수익률 분석 ───────────────────────────────────────────

    def _backtest_analysis(self, score: StockScore, symbol: str) -> None:
        try:
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (
                datetime.now() - timedelta(days=self.backtest_months * 30)
            ).strftime("%Y-%m-%d")

            engine = BacktestEngine(
                strategy=RSIStrategy(period=14, oversold=30, overbought=70),
                initial_capital=1_000_000,
                order_amount=1_000_000,
                stop_loss_pct=-5.0,
                take_profit_pct=10.0,
            )
            result = engine.run(symbol, start_date, end_date)
            score.backtest_return = result.total_return_pct
            score.backtest_win_rate = result.win_rate
            score.backtest_trades = result.num_trades

            # 수익률 점수 (0-50)
            ret = result.total_return_pct
            if ret >= 20:
                ret_score = 50
            elif ret >= 10:
                ret_score = 35
            elif ret >= 5:
                ret_score = 20
            elif ret >= 0:
                ret_score = 10
            else:
                ret_score = max(0, 10 + int(ret))  # 손실 시 감점

            # 승률 점수 (0-50)
            win_score = min(50, int(result.win_rate / 2))

            score.backtest_score = ret_score + win_score

        except Exception as e:
            logger.debug(f"[파이프라인][{symbol}] 백테스트 실패: {e}")
            score.backtest_score = 0

    # ── 4단계: 종합 점수 및 투자 결정 ────────────────────────────────────────

    def _finalize(self, score: StockScore) -> None:
        # 차트 50% + 백테스트 50% 가중 합산
        chart_w = int(score.chart_score / 100 * 50)
        backtest_w = int(score.backtest_score / 100 * 50)
        score.total_score = chart_w + backtest_w

        if score.backtest_return != 0 or score.backtest_trades > 0:
            score._reasons.append(
                f"백테스트 {score.backtest_return:+.1f}% "
                f"(승률 {score.backtest_win_rate:.0f}%, {score.backtest_trades}회)"
            )

        if score.total_score >= 70:
            score.decision = "강력매수"
        elif score.total_score >= 50:
            score.decision = "매수"
        elif score.total_score >= 30:
            score.decision = "관망"
        else:
            score.decision = "제외"

        score.reason = " | ".join(score._reasons) if score._reasons else "지표 부족"
