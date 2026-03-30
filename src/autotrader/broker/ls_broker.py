"""
LS증권 OpenAPI 브로커 모듈
REST API 직접 호출 방식 (requests)
API 문서: https://openapi.ls-sec.co.kr/apiservice
"""
import logging
import os
import time
from datetime import datetime

import requests

from .models import AccountBalance, Order, OrderSide, OrderStatus, Position

logger = logging.getLogger(__name__)

_REAL_HOST = "https://openapi.ls-sec.co.kr:8080"
_PAPER_HOST = "https://openapi.ls-sec.co.kr:9090"


class LSBroker:
    def __init__(self):
        self._app_key = os.environ["LS_APP_KEY"]
        self._app_secret = os.environ["LS_APP_SECRET"]
        self._account_no = os.environ["LS_ACCOUNT_NO"]    # 8자리
        self._account_code = os.environ.get("LS_ACCOUNT_CODE", "01")
        self._account_pwd = os.environ.get("LS_ACCOUNT_PWD", "")
        self._is_paper = os.environ.get("LS_IS_PAPER", "true").lower() == "true"
        self._host = _PAPER_HOST if self._is_paper else _REAL_HOST

        self._token: str = ""
        self._token_expires: float = 0.0
        self._session = requests.Session()

        self._ensure_token()
        mode = "모의투자" if self._is_paper else "실거래"
        logger.info(f"LS증권 브로커 초기화 완료 ({mode})")

    # ── 인증 ───────────────────────────────────────────────────────────────────

    def _ensure_token(self) -> None:
        """토큰 만료 시 자동 재발급"""
        if self._token and time.time() < self._token_expires - 60:
            return
        resp = self._session.post(
            f"{self._host}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecretkey": self._app_secret,
                "scope": "oob",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 86400))
        logger.debug("LS증권 토큰 발급 완료")

    def _headers(self, tr_cd: str, tr_cont: str = "N") -> dict:
        self._ensure_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self._token}",
            "tr_cd": tr_cd,
            "tr_cont": tr_cont,
            "tr_cont_key": "",
            "mac_address": "",
        }

    def _post(self, path: str, tr_cd: str, body: dict) -> dict:
        url = f"{self._host}{path}"
        resp = self._session.post(url, headers=self._headers(tr_cd), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── 시세 조회 ──────────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """현재가 조회 (t1102)"""
        data = self._post(
            "/stock/market-data",
            "t1102",
            {"t1102InBlock": {"shcode": symbol}},
        )
        return float(data["t1102OutBlock"]["price"])

    def get_quote(self, symbol: str) -> dict:
        """
        종목 상세 시세 조회 (t1102)
        반환: 종목명, 현재가, 전일대비, 등락률, 거래량, 시가/고가/저가, 52주 고저가
        """
        data = self._post(
            "/stock/market-data",
            "t1102",
            {"t1102InBlock": {"shcode": symbol}},
        )
        b = data.get("t1102OutBlock", {})
        sign = str(b.get("sign", "3"))
        diff_str = str(b.get("diff", "0")).replace("+", "").replace("%", "")
        try:
            diff = float(diff_str)
        except ValueError:
            diff = 0.0
        # sign: 1=상한가 2=상승 3=보합 4=하한가 5=하락
        if sign in ("1", "2"):
            direction = "up"
        elif sign in ("4", "5"):
            direction = "down"
        else:
            direction = "flat"

        return {
            "symbol": symbol,
            "name": str(b.get("hname", symbol)).strip(),
            "price": int(b.get("price", 0)),
            "change": int(b.get("change", 0)),
            "diff": diff,
            "direction": direction,   # "up" | "down" | "flat"
            "volume": int(b.get("volume", 0)),
            "prev_close": int(b.get("recprice", 0)),
            "open": int(b.get("open", 0)),
            "high": int(b.get("high", 0)),
            "low": int(b.get("low", 0)),
            "high52w": int(b.get("high52w", 0)),
            "low52w": int(b.get("low52w", 0)),
        }

    def get_ohlcv(self, symbol: str, period: str = "D", count: int = 100) -> list[dict]:
        """
        OHLCV 캔들 데이터 조회
        period: D=일봉, W=주봉, M=월봉, 숫자=분봉(1/3/5/10/15/30/60)
        """
        if period == "D":
            return self._fetch_daily_ohlcv(symbol, count)
        elif period in ("W", "M"):
            return self._fetch_period_ohlcv(symbol, period, count)
        else:
            return self._fetch_minute_ohlcv(symbol, int(period), count)

    def _fetch_daily_ohlcv(self, symbol: str, count: int) -> list[dict]:
        """t8410 일봉"""
        data = self._post(
            "/stock/chart",
            "t8410",
            {
                "t8410InBlock": {
                    "shcode": symbol,
                    "gubun": "2",      # 2=수정주가
                    "qrycnt": count,
                    "sdate": "",
                    "edate": "",
                    "cts_date": "",
                    "req_cnt": 0,
                }
            },
        )
        rows = data.get("t8410OutBlock1", [])
        return [
            {
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["jdiff_vol"]),
            }
            for r in reversed(rows)  # 오래된 순 정렬
        ]

    def _fetch_period_ohlcv(self, symbol: str, period: str, count: int) -> list[dict]:
        """t8412 주봉/월봉"""
        gubun = "W" if period == "W" else "M"
        data = self._post(
            "/stock/chart",
            "t8412",
            {
                "t8412InBlock": {
                    "shcode": symbol,
                    "gubun": gubun,
                    "qrycnt": count,
                    "sdate": "",
                    "edate": "",
                    "cts_date": "",
                    "req_cnt": 0,
                }
            },
        )
        rows = data.get("t8412OutBlock1", [])
        return [
            {
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["jdiff_vol"]),
            }
            for r in reversed(rows)
        ]

    def _fetch_minute_ohlcv(self, symbol: str, minute: int, count: int) -> list[dict]:
        """t8411 분봉"""
        data = self._post(
            "/stock/chart",
            "t8411",
            {
                "t8411InBlock": {
                    "shcode": symbol,
                    "gubun": "2",
                    "qrycnt": count,
                    "sdate": "",
                    "edate": "",
                    "cts_date": "",
                    "cts_time": "",
                    "req_cnt": 0,
                    "timegubun": str(minute),  # 1/3/5/10/15/30/60
                }
            },
        )
        rows = data.get("t8411OutBlock1", [])
        return [
            {
                "date": r["date"] + r["time"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["jdiff_vol"]),
            }
            for r in reversed(rows)
        ]

    # ── 주문 ───────────────────────────────────────────────────────────────────

    def buy_market(self, symbol: str, quantity: int, memo: str = "") -> Order:
        """시장가 매수 (CSPAT00601)"""
        logger.info(f"매수 주문: {symbol} {quantity}주 ({memo})")
        data = self._post(
            "/stock/order",
            "CSPAT00601",
            {
                "CSPAT00601InBlock1": {
                    "IsuNo": f"A{symbol}",
                    "OrdQty": quantity,
                    "OrdPrc": 0,            # 시장가 = 0
                    "BnsTpCode": "2",       # 2=매수
                    "OrdprcPtnCode": "03",  # 03=시장가
                    "MgntrnCode": "000",
                    "LoanDt": "",
                    "OrdCndiTpCode": "0",
                }
            },
        )
        return self._parse_order(data, "CSPAT00601OutBlock2", symbol, OrderSide.BUY, quantity, memo)

    def sell_market(self, symbol: str, quantity: int, memo: str = "") -> Order:
        """시장가 매도 (CSPAT00701)"""
        logger.info(f"매도 주문: {symbol} {quantity}주 ({memo})")
        data = self._post(
            "/stock/order",
            "CSPAT00701",
            {
                "CSPAT00701InBlock1": {
                    "OrgOrdNo": 0,
                    "IsuNo": f"A{symbol}",
                    "OrdQty": quantity,
                    "OrdPrc": 0,
                    "BnsTpCode": "1",       # 1=매도
                    "OrdprcPtnCode": "03",
                    "MgntrnCode": "000",
                    "LoanDt": "",
                    "OrdCndiTpCode": "0",
                }
            },
        )
        return self._parse_order(data, "CSPAT00701OutBlock2", symbol, OrderSide.SELL, quantity, memo)

    def _parse_order(
        self,
        data: dict,
        out_block_key: str,
        symbol: str,
        side: OrderSide,
        qty: int,
        memo: str,
    ) -> Order:
        out = data.get(out_block_key, {})
        rsp_cd = data.get("rsp_cd", "99999999")
        order = Order(
            symbol=symbol,
            side=side,
            quantity=qty,
            price=self.get_price(symbol),
            memo=memo,
        )
        if rsp_cd == "00000000":
            order.status = OrderStatus.FILLED
            order.order_id = str(out.get("OrdNo", ""))
            order.filled_at = datetime.now()
            logger.info(f"주문 체결: {symbol} {side.value} {qty}주 (주문번호: {order.order_id})")
        else:
            order.status = OrderStatus.FAILED
            logger.error(f"주문 실패 [{rsp_cd}]: {data.get('rsp_msg', '')}")
        return order

    # ── 계좌 조회 ──────────────────────────────────────────────────────────────

    def get_balance(self) -> AccountBalance:
        """잔고 및 보유 종목 조회 (t0424 - 계좌번호 불필요, 토큰 기반)"""
        data = self._post(
            "/stock/accno",
            "t0424",
            {
                "t0424InBlock": {
                    "prcgb": "1",    # 1=현재가
                    "chegb": "2",    # 2=체결잔고
                    "acgb": "1",     # 1=계좌
                    "charge": "1",
                    "cts_expcode": "",
                }
            },
        )
        summary = data.get("t0424OutBlock", {})
        holdings = data.get("t0424OutBlock1", []) or []

        positions = []
        for item in holdings:
            qty = int(item.get("janqty", 0))
            if qty <= 0:
                continue
            symbol = str(item.get("expcode", "")).strip().lstrip("A")
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=qty,
                    avg_price=float(item.get("pchsprc", 0)),
                    current_price=float(item.get("price", 0)),
                )
            )

        cash = float(summary.get("sunamt", 0))          # 예수금
        total_eval = float(summary.get("tappamt", 0))   # 총 평가금액
        if total_eval == 0 and positions:
            total_eval = sum(p.market_value for p in positions) + cash

        total_pl = float(summary.get("dtsunik", 0))     # 당일 실현손익
        invest_amt = float(summary.get("mamt", 0))      # 매입금액
        total_pct = (total_pl / invest_amt * 100) if invest_amt > 0 else 0.0

        return AccountBalance(
            total_eval=total_eval,
            cash=cash,
            total_profit_loss=total_pl,
            total_profit_pct=round(total_pct, 2),
            positions=positions,
        )
