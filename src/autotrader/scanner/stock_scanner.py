"""
거래량 상위 종목 스캐너
LS증권 t1452 (거래량 상위 조회) 사용
"""
import logging

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
        전일 거래량 상위 종목 반환
        t1452: 거래량 상위 조회
        반환: 종목코드 리스트 (필터 적용 후 최대 top_n개)
        """
        logger.info(f"거래량 상위 {top_n}개 종목 스캐닝 시작")
        try:
            data = self.broker._post(
                "/stock/market-data",
                "t1452",
                {
                    "t1452InBlock": {
                        "gubun": "0",       # 0=전체, 1=코스피, 2=코스닥
                        "qrycnt": top_n * 3,  # 필터링 여유분 확보
                        "tradno": "0",
                    }
                },
            )
        except Exception as e:
            logger.error(f"t1452 조회 실패: {e}")
            return []

        rows = data.get("t1452OutBlock1", [])
        candidates: list[str] = []

        for row in rows:
            symbol = str(row.get("shcode", "")).strip()
            if not symbol:
                continue

            try:
                price = float(row.get("price", 0))
            except (ValueError, TypeError):
                continue

            # 가격 필터
            if price < _MIN_PRICE or price > _MAX_PRICE:
                logger.debug(f"[{symbol}] 가격 필터 제외: {price:,.0f}원")
                continue

            candidates.append(symbol)
            if len(candidates) >= top_n:
                break

        logger.info(f"스캔 결과: {len(candidates)}개 종목 선택 → {candidates}")
        return candidates
