"""
네이버 금융 뉴스 크롤러
종목별 최신 뉴스 헤드라인과 내용을 가져옴
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
}


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    published_at: str
    url: str


class NaverNewsFetcher:
    def __init__(self, max_items: int = 10):
        self.max_items = max_items
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    def fetch(self, symbol: str, lookback_days: int = 3) -> list[NewsItem]:
        """종목 코드에 대한 최신 뉴스 수집"""
        items: list[NewsItem] = []
        cutoff = datetime.now() - timedelta(days=lookback_days)

        try:
            url = (
                f"https://finance.naver.com/item/news_news.naver"
                f"?code={symbol}&page=1&sm=title_entity_id.basic&clusterId="
            )
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            rows = soup.select("table.type5 tr")
            for row in rows:
                title_tag = row.select_one("td.title a")
                info_tag = row.select_one("td.info")
                date_tag = row.select_one("td.date")
                if not title_tag or not date_tag:
                    continue

                title = title_tag.get_text(strip=True)
                source = info_tag.get_text(strip=True) if info_tag else ""
                date_str = date_tag.get_text(strip=True)
                href = title_tag.get("href", "")
                full_url = f"https://finance.naver.com{href}" if href.startswith("/") else href

                # 날짜 파싱 (형식: "YYYY.MM.DD HH:MM" 또는 "MM.DD HH:MM")
                pub_dt = self._parse_date(date_str)
                if pub_dt and pub_dt < cutoff:
                    break

                summary = self._fetch_summary(full_url)
                items.append(NewsItem(
                    title=title,
                    summary=summary,
                    source=source,
                    published_at=date_str,
                    url=full_url,
                ))

                if len(items) >= self.max_items:
                    break

                time.sleep(0.3)  # 서버 부하 방지

        except Exception as e:
            logger.warning(f"[{symbol}] 네이버 뉴스 수집 실패: {e}")

        logger.debug(f"[{symbol}] 뉴스 {len(items)}건 수집")
        return items

    def _fetch_summary(self, url: str) -> str:
        """뉴스 본문 앞 200자 크롤링"""
        try:
            resp = self._session.get(url, timeout=8)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            body = soup.select_one("div#newsct_article") or soup.select_one("div.articleCont")
            if body:
                return body.get_text(strip=True)[:300]
        except Exception:
            pass
        return ""

    def _parse_date(self, date_str: str) -> datetime | None:
        now = datetime.now()
        for fmt in ("%Y.%m.%d %H:%M", "%m.%d %H:%M"):
            try:
                dt = datetime.strptime(date_str, fmt)
                if fmt == "%m.%d %H:%M":
                    dt = dt.replace(year=now.year)
                return dt
            except ValueError:
                continue
        return None
