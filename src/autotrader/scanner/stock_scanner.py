"""
거래량 상위 종목 스캐너
네이버 금융 거래량 순위에서 당일 상위 종목 조회
"""
import logging

import requests
from bs4 import BeautifulSoup

from ..broker.ls_broker import LSBroker

logger = logging.getLogger(__name__)

_NAVER_URL = "https://finance.naver.com/sise/sise_quant.naver"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}

_MIN_PRICE = 1_000
_MAX_PRICE = 500_000


class StockScanner:
    def __init__(self, broker: LSBroker):
        self.broker = broker

    def get_top_volume_stocks(self, top_n: int = 20) -> list[str]:
        """네이버 금융 거래량 순위에서 상위 종목 코드 반환"""
        logger.info(f"거래량 상위 {top_n}개 종목 스캐닝 시작 (네이버 금융)")
        candidates: list[str] = []

        for market in ("0", "1"):  # 0=코스피, 1=코스닥
            if len(candidates) >= top_n:
                break
            try:
                rows = self._fetch_naver(market)
                for symbol, price in rows:
                    if len(candidates) >= top_n:
                        break
                    if price < _MIN_PRICE or price > _MAX_PRICE:
                        logger.debug(f"[{symbol}] 가격 필터 제외: {price:,.0f}원")
                        continue
                    if symbol not in candidates:
                        candidates.append(symbol)
            except Exception as e:
                logger.warning(f"네이버 금융 market={market} 조회 실패: {e}")

        logger.info(f"스캔 결과: {len(candidates)}개 종목 → {candidates}")
        return candidates

    def _fetch_naver(self, market: str) -> list[tuple[str, float]]:
        """네이버 금융 거래량 페이지 파싱 → [(종목코드, 현재가), ...]"""
        resp = requests.get(
            _NAVER_URL,
            params={"sosok": market},
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        resp.encoding = "euc-kr"

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[tuple[str, float]] = []

        for row in soup.select("table.type_2 tr"):
            link = row.select_one("td.name a")
            price_td = row.select_one("td.number:nth-of-type(3)")
            if not link or not price_td:
                continue

            href = link.get("href", "")
            # href 예: /item/main.naver?code=005930
            if "code=" not in href:
                continue
            symbol = href.split("code=")[-1].strip()
            if len(symbol) != 6:
                continue

            try:
                price = float(price_td.text.strip().replace(",", ""))
            except (ValueError, TypeError):
                continue

            results.append((symbol, price))

        return results
