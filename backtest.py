"""
백테스팅 실행 스크립트
사용법: python backtest.py [종목코드] [시작일] [종료일] [--daytrading]
예시:
  python backtest.py 005930                        # 스윙 전략 (기본)
  python backtest.py 005930 2023-01-01 2024-12-31  # 스윙 전략 기간 지정
  python backtest.py 005930 --daytrading           # 단타 전략
"""
import logging
import logging.config
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

with open(ROOT / "config" / "logging.yaml") as f:
    logging.config.dictConfig(yaml.safe_load(f))

with open(ROOT / "config" / "settings.yaml") as f:
    CFG = yaml.safe_load(f)

from src.autotrader.backtest.engine import BacktestEngine
from src.autotrader.strategy.rsi_strategy import RSIStrategy
from src.autotrader.strategy.daytrading_strategy import DayTradingStrategy


def main():
    args = sys.argv[1:]
    daytrading_mode = "--daytrading" in args
    args = [a for a in args if a != "--daytrading"]

    bt_cfg = CFG.get("backtest", {})
    swing_cfg = CFG.get("swing", {})
    swing_rsi = swing_cfg.get("rsi", {})
    dt_bt_cfg = bt_cfg.get("daytrading", {})
    dt_cfg = CFG.get("daytrading", {})

    default_symbol = (swing_cfg.get("watchlist") or ["005930"])[0]
    symbol = args[0] if len(args) > 0 else default_symbol
    start = args[1] if len(args) > 1 else bt_cfg.get("start_date", "2023-01-01")
    end = args[2] if len(args) > 2 else bt_cfg.get("end_date", "2024-12-31")

    if daytrading_mode:
        strategy = DayTradingStrategy(
            period=dt_bt_cfg.get("rsi_period", dt_cfg.get("rsi_period", 7)),
            oversold=dt_bt_cfg.get("rsi_oversold", dt_cfg.get("rsi_oversold", 35.0)),
            overbought=dt_bt_cfg.get("rsi_overbought", dt_cfg.get("rsi_overbought", 65.0)),
        )
        engine = BacktestEngine(
            strategy=strategy,
            initial_capital=bt_cfg.get("initial_capital", 10_000_000),
            order_amount=dt_cfg.get("order_amount", 200_000),
            stop_loss_pct=dt_bt_cfg.get("stop_loss_pct", dt_cfg.get("stop_loss_pct", -1.5)),
            take_profit_pct=dt_bt_cfg.get("take_profit_pct", dt_cfg.get("take_profit_pct", 2.0)),
        )
        print(f"[단타 백테스트] {symbol} | {start} ~ {end}")
    else:
        strategy = RSIStrategy(
            period=swing_rsi.get("period", 14),
            oversold=swing_rsi.get("oversold", 30),
            overbought=swing_rsi.get("overbought", 70),
        )
        engine = BacktestEngine(
            strategy=strategy,
            initial_capital=bt_cfg.get("initial_capital", 10_000_000),
            order_amount=swing_cfg.get("order_amount", 500_000),
            stop_loss_pct=swing_cfg.get("stop_loss_pct", -5.0),
            take_profit_pct=swing_cfg.get("take_profit_pct", 10.0),
        )
        print(f"[스윙 백테스트] {symbol} | {start} ~ {end}")

    engine.run(symbol, start, end)


if __name__ == "__main__":
    main()
