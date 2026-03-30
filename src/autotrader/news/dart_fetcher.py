"""
DART 전자공시시스템 공시 데이터 수집
API 키 발급: https://opendart.fss.or.kr
"""
import logging
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://opendart.fss.or.kr/api"
_CACHE_DIR = Path(__file__).parents[4] / "data" / "dart_cache"


@dataclass
class Disclosure:
    title: str
    corp_name: str
    report_type: str   # 공시 유형 (예: 분기보고서, 임시주주총회 등)
    filed_at: str
    url: str


class DARTFetcher:
    def __init__(self):
        self._api_key = os.environ.get("DART_API_KEY", "")
        self._session = requests.Session()
        # 종목코드 → DART corp_code 매핑 캐시
        self._corp_map: dict[str, str] = {}
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if not self._api_key:
            logger.warning("DART_API_KEY 미설정 → 공시 데이터 비활성화")

    def fetch(self, symbol: str, lookback_days: int = 3) -> list[Disclosure]:
        """종목코드로 최근 공시 목록 조회"""
        if not self._api_key:
            return []

        corp_code = self._get_corp_code(symbol)
        if not corp_code:
            logger.warning(f"[{symbol}] DART corp_code 조회 실패")
            return []

        start_dt = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        end_dt = datetime.now().strftime("%Y%m%d")

        try:
            resp = self._session.get(
                f"{_BASE_URL}/list.json",
                params={
                    "crtfc_key": self._api_key,
                    "corp_code": corp_code,
                    "bgn_de": start_dt,
                    "end_de": end_dt,
                    "page_count": 10,
                    "sort": "date",
                    "sort_mth": "desc",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "000":
                logger.debug(f"[{symbol}] DART 공시 없음: {data.get('message')}")
                return []

            items = []
            for item in data.get("list", []):
                items.append(Disclosure(
                    title=item["report_nm"],
                    corp_name=item["corp_name"],
                    report_type=item.get("form_type", ""),
                    filed_at=item["rcept_dt"],
                    url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item['rcept_no']}",
                ))
            logger.debug(f"[{symbol}] DART 공시 {len(items)}건")
            return items

        except Exception as e:
            logger.warning(f"[{symbol}] DART 조회 실패: {e}")
            return []

    def _get_corp_code(self, symbol: str) -> str:
        """종목코드 → DART corp_code 변환 (캐시 활용)"""
        if symbol in self._corp_map:
            return self._corp_map[symbol]

        self._load_corp_codes()
        return self._corp_map.get(symbol, "")

    def _load_corp_codes(self) -> None:
        """DART corp_code 전체 목록 다운로드 및 파싱 (하루 1회 캐시)"""
        cache_file = _CACHE_DIR / "corp_codes.txt"
        if cache_file.exists():
            age_hours = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600
            if age_hours < 24:
                self._parse_corp_codes(cache_file.read_text(encoding="utf-8"))
                return

        try:
            resp = self._session.get(
                f"{_BASE_URL}/corpCode.xml",
                params={"crtfc_key": self._api_key},
                timeout=30,
            )
            resp.raise_for_status()

            # ZIP 파일 압축 해제
            with zipfile.ZipFile(BytesIO(resp.content)) as zf:
                xml_content = zf.read(zf.namelist()[0]).decode("utf-8")

            cache_file.write_text(xml_content, encoding="utf-8")
            self._parse_corp_codes(xml_content)
            logger.info("DART corp_code 목록 갱신 완료")

        except Exception as e:
            logger.warning(f"DART corp_code 다운로드 실패: {e}")

    def _parse_corp_codes(self, xml_text: str) -> None:
        """XML에서 stock_code → corp_code 매핑 파싱"""
        import re
        entries = re.findall(
            r"<stock_code>(\d+)</stock_code>.*?<corp_code>(\d+)</corp_code>",
            xml_text,
            re.DOTALL,
        )
        for stock_code, corp_code in entries:
            if stock_code:
                self._corp_map[stock_code.zfill(6)] = corp_code
