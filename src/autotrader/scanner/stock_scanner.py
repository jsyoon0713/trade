"""
거래량 상위 종목 스캐너
KRX 데이터포털에 직접 요청하여 당일 거래량 상위 종목 조회
"""
import logging
from datetime import datetime

import requests

from ..broker.ls_broker import LSBroker

logger = logging.getLogger(__name__)

_KRX_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_HEADERS = {
    "Referer": "https://data.krx.co.kr/",
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# 가격 필터 기준
_MIN_PRICE = 1_000
_MAX_PRICE = 500_000


class StockScanner:
    def __init__(self, broker: LSBroker):
        self.broker = broker

    def get_top_volume_stocks(self, top_n: int = 20) -> list[str]:
        """
        당일 거래량 상위 종목 반환 (KRX 직접 조회)
        반환: 종목코드 리스트 (필터 적용 후 최대 top_n개)
        """
        logger.info(f"거래량 상위 {top_n}개 종목 스캐닝 시작")
        today = datetime.now().strftime("%Y%m%d")

        rows: list[dict] = []
        for market_id in ("STK", "KSQ"):  # STK=코스피, KSQ=코스닥
            try:
                resp = requests.post(
                    _KRX_URL,
                    headers=_HEADERS,
                    data={
                        "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
                        "mktId": market_id,
                        "trdDd": today,
                        "share": "1",
                        "money": "1",
                        "csvxls_isNo": "false",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                rows.extend(resp.json().get("OutBlock_1", []))
            except Exception as e:
                logger.warning(f"KRX {market_id} 조회 실패: {e}")

        if not rows:
            logger.error("KRX 스캔 실패 — 데이터 없음")
            return []

        # 거래량 기준 정렬
        try:
            rows.sort(key=lambda r: int(str(r.get("ACC_TRDVOL", "0")).replace(",", "")), reverse=True)
        except Exception as e:
            logger.warning(f"거래량 정렬 실패: {e}")

        candidates: list[str] = []
        for row in rows:
            symbol = str(row.get("ISU_SRT_CD", "")).strip()
            if not symbol or len(symbol) != 6:
                continue

            try:
                price = float(str(row.get("TDD_CLSPRC", "0")).replace(",", ""))
            except (ValueError, TypeError):
                continue

            if price < _MIN_PRICE or price > _MAX_PRICE:
                logger.debug(f"[{symbol}] 가격 필터 제외: {price:,.0f}원")
                continue

            candidates.append(symbol)
            if len(candidates) >= top_n:
                break

        logger.info(f"스캔 결과: {len(candidates)}개 종목 선택 → {candidates}")
        return candidates
