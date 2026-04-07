"""
거래량 상위 종목 스캐너
pykrx로 당일 거래량 상위 종목 조회
"""
import logging
from datetime import datetime

from ..broker.ls_broker import LSBroker

logger = logging.getLogger(__name__)

# 가격 필터 기준
_MIN_PRICE = 1_000     # 1천원 미만 제외 (저가주)
_MAX_PRICE = 500_000   # 50만원 초과 제외 (고가주 - 소액 매매 어려움)


class StockScanner:
    def __init__(self, broker: LSBroker):
        self.broker = broker

    def get_top_volume_stocks(self, top_n: int = 20) -> list[str]:
        """
        당일 거래량 상위 종목 반환 (pykrx 사용)
        반환: 종목코드 리스트 (필터 적용 후 최대 top_n개)
        """
        logger.info(f"거래량 상위 {top_n}개 종목 스캐닝 시작")
        try:
            from pykrx import stock as krx
            today = datetime.now().strftime("%Y%m%d")

            # KOSPI + KOSDAQ 합쳐서 거래량 상위 조회 (OHLCV에 거래량 포함)
            import pandas as pd
            df_kospi  = krx.get_market_ohlcv_by_ticker(today, market="KOSPI")
            df_kosdaq = krx.get_market_ohlcv_by_ticker(today, market="KOSDAQ")
            df = pd.concat([df_kospi, df_kosdaq])

            # 거래량 컬럼 찾기
            vol_col = next(
                (c for c in df.columns if "거래량" in c or "volume" in c.lower()),
                None
            )
            if vol_col is None:
                raise ValueError(f"거래량 컬럼 없음: {list(df.columns)}")
            if vol_col != "거래량":
                df = df.rename(columns={vol_col: "거래량"})

            df = df.sort_values("거래량", ascending=False)

        except Exception as e:
            logger.error(f"pykrx 스캔 실패: {e} — LS 브로커 fallback 시도")
            return self._scan_via_broker(top_n)

        candidates: list[str] = []
        for symbol in df.index:
            symbol = str(symbol).strip().zfill(6)
            if not symbol:
                continue

            # 현재가 조회로 가격 필터
            try:
                price = self.broker.get_price(symbol)
            except Exception:
                continue

            if price < _MIN_PRICE or price > _MAX_PRICE:
                logger.debug(f"[{symbol}] 가격 필터 제외: {price:,.0f}원")
                continue

            candidates.append(symbol)
            if len(candidates) >= top_n:
                break

        logger.info(f"스캔 결과: {len(candidates)}개 종목 선택 → {candidates}")
        return candidates

    def _scan_via_broker(self, top_n: int) -> list[str]:
        """pykrx 실패 시 LS 브로커 watchlist 기반 fallback (빈 리스트)"""
        logger.warning("pykrx 불가 — 워치리스트 비어있음. 종목을 수동으로 추가해주세요.")
        return []
