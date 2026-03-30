"""
백테스팅 엔진
yfinance로 과거 데이터를 가져와 전략을 시뮬레이션
한국 종목코드 자동 변환: 005930 → 005930.KS (KOSPI)
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import yfinance as yf

from ..strategy.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    symbol: str
    date: str
    side: str       # "buy" | "sell"
    price: float
    quantity: int
    reason: str


@dataclass
class BacktestResult:
    symbol: str
    initial_capital: float
    final_capital: float
    trades: list[Trade] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        return (self.final_capital - self.initial_capital) / self.initial_capital * 100

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        sell_trades = [t for t in self.trades if t.side == "sell"]
        if not sell_trades:
            return 0.0
        # 매수-매도 쌍 기준 승률 계산
        wins = sum(
            1 for i, t in enumerate(sell_trades)
            if i < len([x for x in self.trades if x.side == "buy"])
            and t.price > [x for x in self.trades if x.side == "buy"][i].price
        )
        return wins / len(sell_trades) * 100


class BacktestEngine:
    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 10_000_000,
        order_amount: float = 1_000_000,
        stop_loss_pct: float = -5.0,
        take_profit_pct: float = 10.0,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.order_amount = order_amount
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    def run(self, symbol: str, start_date: str, end_date: str) -> BacktestResult:
        """
        symbol: 종목코드 (예: "005930")
        start_date / end_date: "YYYY-MM-DD"
        """
        logger.info(f"백테스팅 시작: {symbol} ({start_date} ~ {end_date})")

        df = self._fetch_ohlcv(symbol, start_date, end_date)
        if df.empty:
            logger.error(f"{symbol}: 과거 데이터 없음")
            return BacktestResult(symbol, self.initial_capital, self.initial_capital)

        capital = self.initial_capital
        position_qty = 0
        position_price = 0.0
        trades: list[Trade] = []

        for i in range(30, len(df)):  # 최소 30개 데이터 워밍업
            window = df.iloc[:i].to_dict("records")
            current = df.iloc[i]
            date = str(current.name)[:10]
            price = float(current["Close"])

            # 손절/익절 체크 (보유 중)
            if position_qty > 0:
                pct = (price - position_price) / position_price * 100
                if pct <= self.stop_loss_pct or pct >= self.take_profit_pct:
                    reason = f"손절 {pct:.1f}%" if pct <= self.stop_loss_pct else f"익절 {pct:.1f}%"
                    capital += price * position_qty
                    trades.append(Trade(symbol, date, "sell", price, position_qty, reason))
                    position_qty = 0
                    position_price = 0.0
                    continue

            # 전략 신호
            ohlcv = [
                {
                    "date": str(r.get("date", "")),
                    "open": float(r.get("Open", 0)),
                    "high": float(r.get("High", 0)),
                    "low": float(r.get("Low", 0)),
                    "close": float(r.get("Close", 0)),
                    "volume": int(r.get("Volume", 0)),
                }
                for r in window
            ]
            result = self.strategy.generate_signal(symbol, ohlcv)

            if result.signal == Signal.BUY and position_qty == 0 and capital >= self.order_amount:
                qty = int(self.order_amount / price)
                if qty > 0:
                    cost = qty * price
                    capital -= cost
                    position_qty = qty
                    position_price = price
                    trades.append(Trade(symbol, date, "buy", price, qty, result.reason))

            elif result.signal == Signal.SELL and position_qty > 0:
                capital += price * position_qty
                trades.append(Trade(symbol, date, "sell", price, position_qty, result.reason))
                position_qty = 0
                position_price = 0.0

        # 미청산 포지션 종가로 청산
        if position_qty > 0:
            last_price = float(df.iloc[-1]["Close"])
            capital += last_price * position_qty

        result = BacktestResult(symbol, self.initial_capital, capital, trades)
        self._print_summary(result)
        return result

    def _fetch_ohlcv(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        # 한국 종목: KOSPI는 .KS, KOSDAQ은 .KQ 접미사
        ticker = f"{symbol}.KS"
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            # KOSDAQ 시도
            ticker = f"{symbol}.KQ"
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        # MultiIndex 컬럼 평탄화
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df

    def _print_summary(self, result: BacktestResult) -> None:
        logger.info("=" * 50)
        logger.info(f"백테스팅 결과: {result.symbol}")
        logger.info(f"  초기 자본  : {result.initial_capital:,.0f}원")
        logger.info(f"  최종 자본  : {result.final_capital:,.0f}원")
        logger.info(f"  수익률     : {result.total_return_pct:+.2f}%")
        logger.info(f"  총 거래 수 : {result.num_trades}회")
        logger.info(f"  승률       : {result.win_rate:.1f}%")
        logger.info("=" * 50)
