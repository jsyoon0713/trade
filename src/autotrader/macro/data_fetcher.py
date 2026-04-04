"""글로벌 시장 데이터 수집 (yfinance)"""
import logging
from datetime import datetime

from .models import IndexSnapshot, MarketData

logger = logging.getLogger(__name__)

_INDICES = [
    ("^GSPC",     "S&P500"),
    ("^IXIC",     "NASDAQ"),
    ("^DJI",      "DOW"),
    ("^N225",     "Nikkei"),
    ("000001.SS", "Shanghai"),
    ("^HSI",      "HangSeng"),
    ("^KS11",     "KOSPI"),
    ("^KQ11",     "KOSDAQ"),
]

_FX_SYMBOL = "KRW=X"


def fetch_market_data() -> MarketData:
    """글로벌 지수 + 환율 수집. 개별 심볼 실패 시 건너뜀."""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance 미설치: pip install yfinance")
        return MarketData()

    indices: list[IndexSnapshot] = []

    for symbol, name in _INDICES:
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if len(hist) < 2:
                continue
            prev_close = float(hist["Close"].iloc[-2])
            last_close = float(hist["Close"].iloc[-1])
            change_pct = (last_close - prev_close) / prev_close * 100
            indices.append(IndexSnapshot(
                symbol=symbol,
                name=name,
                price=round(last_close, 2),
                change_pct=round(change_pct, 2),
            ))
        except Exception as e:
            logger.debug(f"[매크로] {symbol} 조회 실패: {e}")

    usd_krw = 0.0
    usd_krw_chg = 0.0
    try:
        hist = yf.Ticker(_FX_SYMBOL).history(period="5d")
        if len(hist) >= 2:
            prev = float(hist["Close"].iloc[-2])
            last = float(hist["Close"].iloc[-1])
            usd_krw = round(last, 2)
            usd_krw_chg = round((last - prev) / prev * 100, 2)
    except Exception as e:
        logger.debug(f"[매크로] USD/KRW 조회 실패: {e}")

    return MarketData(
        indices=indices,
        usd_krw=usd_krw,
        usd_krw_change_pct=usd_krw_chg,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
