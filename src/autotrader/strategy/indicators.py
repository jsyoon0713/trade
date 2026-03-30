"""기술적 지표 계산 유틸리티 (ta 라이브러리 래핑)"""
import pandas as pd
import ta


def calc_rsi(closes: list[float], period: int = 14) -> float:
    """
    RSI 계산 후 최신값 반환
    closes: 종가 리스트 (오래된 순 → 최신 순)
    """
    if len(closes) < period + 1:
        raise ValueError(f"RSI 계산에 최소 {period + 1}개 데이터 필요 (현재: {len(closes)})")

    series = pd.Series(closes)
    rsi_series = ta.momentum.RSIIndicator(close=series, window=period).rsi()
    return round(float(rsi_series.iloc[-1]), 2)


def calc_moving_average(closes: list[float], period: int) -> float:
    """단순 이동평균 (최신값)"""
    series = pd.Series(closes)
    return round(float(series.rolling(window=period).mean().iloc[-1]), 2)


def calc_bollinger_bands(
    closes: list[float], period: int = 20, std_dev: float = 2.0
) -> dict[str, float]:
    """볼린저 밴드 (최신값)"""
    series = pd.Series(closes)
    bb = ta.volatility.BollingerBands(close=series, window=period, window_dev=std_dev)
    return {
        "upper": round(float(bb.bollinger_hband().iloc[-1]), 2),
        "middle": round(float(bb.bollinger_mavg().iloc[-1]), 2),
        "lower": round(float(bb.bollinger_lband().iloc[-1]), 2),
    }
