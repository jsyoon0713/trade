"""
거래량 상위 종목 스캐너
KRX 데이터포털 OTP 방식으로 당일 거래량 상위 종목 조회
"""
import logging
from datetime import datetime

import requests

from ..broker.ls_broker import LSBroker

logger = logging.getLogger(__name__)

_OTP_URL  = "https://data.krx.co.kr/comm/bldAttendant/getOTP.cmd"
_DATA_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_HEADERS = {
    "Referer": "https://data.krx.co.kr/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

_MIN_PRICE = 1_000
_MAX_PRICE = 500_000


class StockScanner:
    def __init__(self, broker: LSBroker):
        self.broker = broker

    def get_top_volume_stocks(self, top_n: int = 20) -> list[str]:
        """당일 거래량 상위 종목 반환 (KRX OTP 방식)"""
        logger.info(f"거래량 상위 {top_n}개 종목 스캐닝 시작")
        today = datetime.now().strftime("%Y%m%d")

        rows: list[dict] = []
        for mkt_id in ("STK", "KSQ"):  # KOSPI, KOSDAQ
            try:
                rows.extend(self._fetch_market(mkt_id, today))
            except Exception as e:
                logger.warning(f"KRX {mkt_id} 조회 실패: {e}")

        if not rows:
            logger.error("KRX 스캔 실패 — 데이터 없음")
            return []

        # 거래량 기준 정렬
        rows.sort(
            key=lambda r: int(str(r.get("ACC_TRDVOL", "0")).replace(",", "")),
            reverse=True,
        )

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
                continue
            candidates.append(symbol)
            if len(candidates) >= top_n:
                break

        logger.info(f"스캔 결과: {len(candidates)}개 종목 → {candidates}")
        return candidates

    def _fetch_market(self, mkt_id: str, date: str) -> list[dict]:
        params = {
            "bld":  "dbms/MDC/STAT/standard/MDCSTAT01501",
            "mktId": mkt_id,
            "trdDd": date,
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        }
        # 1단계: OTP 발급
        otp_resp = requests.get(_OTP_URL, params=params, headers=_HEADERS, timeout=10)
        otp_resp.raise_for_status()
        otp = otp_resp.text.strip()

        # 2단계: 데이터 조회
        data_resp = requests.post(
            _DATA_URL,
            data={"code": otp},
            headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        data_resp.raise_for_status()
        return data_resp.json().get("OutBlock_1", [])
