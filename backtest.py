"""
백테스팅 실행 스크립트
사용법:
  python backtest.py 005930                        # 스윙 전략 (기본)
  python backtest.py 005930 2023-01-01 2024-12-31  # 스윙 전략 기간 지정
  python backtest.py --vwap 005930,000660,035420   # 단타 VWAP 전략 (NEW)
  python backtest.py --vwap 005930 --days 30       # 기간 지정
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


def main():
    args = sys.argv[1:]

    # ── 단타 VWAP 백테스트 ────────────────────────────────────────────────
    if "--vwap" in args:
        from src.autotrader.backtest.daytrading_backtest import DayTradingBacktest

        # --days 파싱
        days = 45
        if "--days" in args:
            idx = args.index("--days")
            try:
                days = int(args[idx + 1])
                args = args[:idx] + args[idx + 2:]
            except (IndexError, ValueError):
                pass

        args.remove("--vwap")

        # 종목 파싱 (쉼표 구분 또는 스페이스 구분)
        symbols = []
        for a in args:
            symbols.extend([s.strip() for s in a.split(",") if s.strip()])

        if not symbols:
            # 기본값: settings.yaml 스윙 워치리스트 첫 번째
            default = (CFG.get("swing", {}).get("watchlist") or ["005930"])[0]
            symbols = [default]

        dt_cfg = CFG.get("daytrading", {})
        capital = float(dt_cfg.get("capital", 2_000_000))

        print(f"\n단타 VWAP 백테스트: {symbols} | 최근 {days}일")
        print("데이터 다운로드 중... (yfinance, 잠시 기다려주세요)\n")

        bt = DayTradingBacktest(
            hard_stop_pct   = 1.0,
            take_profit_pct = 2.0,
            gap_up_min_pct  = 0.5,
            order_amount    = capital * 0.3,
            initial_capital = capital,
        )
        report = bt.run(symbols, days=days)
        bt.print_report(report)

        # 그래프 출력 여부
        if report.all_trades:
            try:
                bt.plot(report)
            except Exception as e:
                print(f"(차트 출력 실패: {e})")
        return

    # ── 기존 스윙/단타RSI 백테스트 ───────────────────────────────────────
    from src.autotrader.backtest.engine import BacktestEngine
    from src.autotrader.strategy.rsi_strategy import RSIStrategy
    from src.autotrader.strategy.daytrading_strategy import DayTradingStrategy

    daytrading_mode = "--daytrading" in args
    args = [a for a in args if a != "--daytrading"]

    bt_cfg    = CFG.get("backtest", {})
    swing_cfg = CFG.get("swing", {})
    swing_rsi = swing_cfg.get("rsi", {})
    dt_bt_cfg = bt_cfg.get("daytrading", {})
    dt_cfg    = CFG.get("daytrading", {})

    default_symbol = (swing_cfg.get("watchlist") or ["005930"])[0]
    symbol = args[0] if len(args) > 0 else default_symbol
    start  = args[1] if len(args) > 1 else bt_cfg.get("start_date", "2023-01-01")
    end    = args[2] if len(args) > 2 else bt_cfg.get("end_date",   "2024-12-31")

    if daytrading_mode:
        strategy = DayTradingStrategy(
            period     = dt_bt_cfg.get("rsi_period",    dt_cfg.get("rsi_period", 7)),
            oversold   = dt_bt_cfg.get("rsi_oversold",  dt_cfg.get("rsi_oversold", 35.0)),
            overbought = dt_bt_cfg.get("rsi_overbought", dt_cfg.get("rsi_overbought", 65.0)),
        )
        engine = BacktestEngine(
            strategy        = strategy,
            initial_capital = bt_cfg.get("initial_capital", 10_000_000),
            order_amount    = dt_cfg.get("order_amount", 200_000),
            stop_loss_pct   = dt_bt_cfg.get("stop_loss_pct",   dt_cfg.get("stop_loss_pct", -1.5)),
            take_profit_pct = dt_bt_cfg.get("take_profit_pct", dt_cfg.get("take_profit_pct", 2.0)),
        )
        print(f"[단타RSI 백테스트] {symbol} | {start} ~ {end}")
    else:
        strategy = RSIStrategy(
            period     = swing_rsi.get("period", 14),
            oversold   = swing_rsi.get("oversold", 30),
            overbought = swing_rsi.get("overbought", 70),
        )
        engine = BacktestEngine(
            strategy        = strategy,
            initial_capital = bt_cfg.get("initial_capital", 10_000_000),
            order_amount    = swing_cfg.get("order_amount", 500_000),
            stop_loss_pct   = swing_cfg.get("stop_loss_pct", -5.0),
            take_profit_pct = swing_cfg.get("take_profit_pct", 10.0),
        )
        print(f"[스윙 백테스트] {symbol} | {start} ~ {end}")

    engine.run(symbol, start, end)


if __name__ == "__main__":
    main()
