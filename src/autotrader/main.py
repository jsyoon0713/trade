"""
KRX 자동매매 메인 엔트리포인트
APScheduler로 장 시간에 맞춰 중장기(스윙) + 단타 이중 전략 실행

스케줄:
  08:30  단타 종목 준비 (스캐닝 + 60초 입력 대기)
  09:01  장 시작 → 단타 + 중장기 동시 실행 시작
  09:01~15:20  매 1분: 단타 체크
  09:01~15:20  매 30분: 중장기 체크
  15:20  단타 강제청산
  15:35  일일 리포트
"""
import getpass
import logging
import logging.config
import os
import sys
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

# 프로젝트 루트 기준 경로 설정
ROOT = Path(__file__).parents[2]
load_dotenv(ROOT / ".env")


def _prompt_credentials() -> None:
    """계좌번호/비밀번호가 미설정된 경우 실행 시 직접 입력받음"""
    if not os.environ.get("LS_ACCOUNT_NO"):
        account_no = input("LS증권 계좌번호 (숫자만): ").strip()
        os.environ["LS_ACCOUNT_NO"] = account_no

    if not os.environ.get("LS_ACCOUNT_PWD"):
        account_pwd = getpass.getpass("계좌 비밀번호 (입력 시 화면에 표시 안 됨): ")
        os.environ["LS_ACCOUNT_PWD"] = account_pwd


_prompt_credentials()

# 로깅 설정
with open(ROOT / "config" / "logging.yaml") as f:
    logging.config.dictConfig(yaml.safe_load(f))
logger = logging.getLogger(__name__)

# 설정 로드
with open(ROOT / "config" / "settings.yaml") as f:
    CFG = yaml.safe_load(f)

from .broker.ls_broker import LSBroker
from .monitor.portfolio import PortfolioMonitor
from .monitor.risk_manager import RiskManager
from .notification.telegram_notifier import TelegramNotifier
from .strategy.rsi_strategy import RSIStrategy
from .strategy.combined_strategy import CombinedStrategy
from .trader.swing_trader import SwingTrader
from .trader.daytrader import DayTrader


def build_components():
    broker = LSBroker()
    portfolio_monitor = PortfolioMonitor(broker)
    notifier = TelegramNotifier()

    # ── 중장기(스윙) 전략 ──────────────────────────────────────────────────
    swing_cfg = CFG.get("swing", {})
    news_cfg = CFG.get("news_analysis", {})
    rsi_cfg = swing_cfg.get("rsi", {})

    if news_cfg.get("enabled", False):
        swing_strategy = CombinedStrategy(
            rsi_period=rsi_cfg.get("period", 14),
            rsi_oversold=rsi_cfg.get("oversold", 30),
            rsi_overbought=rsi_cfg.get("overbought", 70),
            lookback_days=news_cfg.get("lookback_days", 3),
            block_buy_on_negative=news_cfg.get("block_buy_on_negative", True),
            include_dart=news_cfg.get("include_dart", True),
            ai_provider=news_cfg.get("provider", "auto"),
            ai_model=news_cfg.get("model", ""),
        )
        logger.info(f"[스윙] 전략: RSI + AI 뉴스 분석 (provider={news_cfg.get('provider','auto')})")
    else:
        swing_strategy = RSIStrategy(
            period=rsi_cfg.get("period", 14),
            oversold=rsi_cfg.get("oversold", 30),
            overbought=rsi_cfg.get("overbought", 70),
        )
        logger.info("[스윙] 전략: RSI 단독")

    swing_risk_manager = RiskManager(
        broker,
        stop_loss_pct=swing_cfg.get("stop_loss_pct", -5.0),
        take_profit_pct=swing_cfg.get("take_profit_pct", 10.0),
    )
    swing_trader = SwingTrader(
        broker=broker,
        strategy=swing_strategy,
        portfolio_monitor=portfolio_monitor,
        risk_manager=swing_risk_manager,
        notifier=notifier,
        watchlist=swing_cfg.get("watchlist", []),
        capital=swing_cfg.get("capital", 5_000_000),
        order_amount=swing_cfg.get("order_amount", 500_000),
        max_positions=swing_cfg.get("max_positions", 5),
        candle_interval=rsi_cfg.get("candle_interval", "D"),
    )

    # ── 단타 트레이더 ──────────────────────────────────────────────────────
    dt_cfg = CFG.get("daytrading", {})
    day_trader = DayTrader(
        broker=broker,
        portfolio_monitor=portfolio_monitor,
        notifier=notifier,
        scan_top_n=dt_cfg.get("scan_top_n", 20),
        capital=dt_cfg.get("capital", 2_000_000),
        order_amount=dt_cfg.get("order_amount", 200_000),
        take_profit_pct=dt_cfg.get("take_profit_pct", 2.0),
        stop_loss_pct=dt_cfg.get("stop_loss_pct", -1.5),
        max_positions=dt_cfg.get("max_positions", 3),
        rsi_period=dt_cfg.get("rsi_period", 7),
        rsi_oversold=dt_cfg.get("rsi_oversold", 35.0),
        rsi_overbought=dt_cfg.get("rsi_overbought", 65.0),
        candle_interval=str(dt_cfg.get("candle_interval", "5")),
        force_close_time=dt_cfg.get("force_close_time", "15:20"),
    )

    logger.info(
        f"시드머니 - 스윙: {swing_cfg.get('capital', 5_000_000):,}원 | "
        f"단타: {dt_cfg.get('capital', 2_000_000):,}원"
    )
    return broker, portfolio_monitor, notifier, swing_trader, day_trader


def morning_daytrading_prep(day_trader: DayTrader) -> None:
    """08:30 단타 종목 준비"""
    day_trader.morning_prep()


def run_daytrading(day_trader: DayTrader, dt_enabled: bool) -> None:
    """매 1분 단타 체크"""
    if not dt_enabled:
        return
    day_trader.run()


def run_swing(swing_trader: SwingTrader, swing_enabled: bool) -> None:
    """매 30분 중장기 체크"""
    if not swing_enabled:
        return
    swing_trader.run()


def force_close_daytrading(day_trader: DayTrader, dt_enabled: bool) -> None:
    """15:20 단타 강제청산"""
    if not dt_enabled:
        return
    day_trader.force_close_all()


def daily_report(portfolio_monitor: PortfolioMonitor, notifier: TelegramNotifier) -> None:
    """15:35 일일 마감 리포트"""
    logger.info("일일 리포트 전송")
    report = portfolio_monitor.format_report()
    notifier.notify_portfolio(f"*일일 마감 리포트*\n\n{report}")


def main():
    logger.info("KRX 자동매매 봇 시작 (중장기 + 단타 이중 전략)")
    broker, portfolio_monitor, notifier, swing_trader, day_trader = build_components()
    notifier.notify_start()

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    sched_cfg = CFG.get("scheduler", {})

    swing_cfg = CFG.get("swing", {})
    dt_cfg = CFG.get("daytrading", {})
    swing_enabled = swing_cfg.get("enabled", True)
    dt_enabled = dt_cfg.get("enabled", True)

    swing_interval = sched_cfg.get("swing_interval_minutes", 30)
    prep_time = sched_cfg.get("daytrading_prep", "08:30").split(":")
    force_close_time = sched_cfg.get("daytrading_force_close", "15:20").split(":")
    report_time = sched_cfg.get("daily_report", "15:35").split(":")

    logger.info(
        f"설정 - 스윙: {'ON' if swing_enabled else 'OFF'} | "
        f"단타: {'ON' if dt_enabled else 'OFF'} | "
        f"스윙 인터벌: {swing_interval}분"
    )

    # 08:30 단타 종목 준비
    if dt_enabled:
        scheduler.add_job(
            morning_daytrading_prep,
            "cron",
            day_of_week="mon-fri",
            hour=prep_time[0],
            minute=prep_time[1],
            args=[day_trader],
            id="daytrading_prep",
        )

    # 09:01~15:19 매 1분: 단타 체크
    if dt_enabled:
        scheduler.add_job(
            run_daytrading,
            "cron",
            day_of_week="mon-fri",
            hour="9-15",
            minute="1-59",
            args=[day_trader, dt_enabled],
            id="daytrading_run",
        )

    # 09:01~15:20 매 N분: 중장기 체크
    if swing_enabled:
        scheduler.add_job(
            run_swing,
            "cron",
            day_of_week="mon-fri",
            hour="9-15",
            minute=f"1-59/{swing_interval}",
            args=[swing_trader, swing_enabled],
            id="swing_run",
        )

    # 15:20 단타 강제청산
    if dt_enabled:
        scheduler.add_job(
            force_close_daytrading,
            "cron",
            day_of_week="mon-fri",
            hour=force_close_time[0],
            minute=force_close_time[1],
            args=[day_trader, dt_enabled],
            id="daytrading_force_close",
        )

    # 15:35 일일 리포트
    scheduler.add_job(
        daily_report,
        "cron",
        day_of_week="mon-fri",
        hour=report_time[0],
        minute=report_time[1],
        args=[portfolio_monitor, notifier],
        id="daily_report",
    )

    try:
        logger.info("스케줄러 시작")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("봇 종료")
        notifier.notify_stop()


if __name__ == "__main__":
    main()
