"""
KRX 자동매매 웹 대시보드
Flask + SSE 기반 실시간 모니터링 & 제어
"""
import functools
import hashlib
import json
import logging
import os
import queue
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import (
    Flask, Response, jsonify, render_template,
    request, session, redirect, url_for, stream_with_context,
)

ROOT = Path(__file__).parents[3]
load_dotenv(ROOT / ".env")

with open(ROOT / "config" / "settings.yaml") as f:
    CFG = yaml.safe_load(f)

from ..broker.ls_broker import LSBroker
from ..macro.adjuster import ParameterAdjuster
from ..macro.analyzer import create_macro_analyzer
from ..macro.data_fetcher import fetch_market_data
from ..macro.historical import build_knowledge_base, get_as_text
from ..macro.models import MacroResult, neutral_macro_analysis, MarketData
from ..monitor.portfolio import PortfolioMonitor
from ..monitor.risk_manager import RiskManager
from ..notification.telegram_notifier import TelegramNotifier
from ..pipeline.stock_analyzer import StockAnalyzer
from ..strategy.combined_strategy import CombinedStrategy
from ..strategy.rsi_strategy import RSIStrategy
from ..trader.daytrader import DayTrader
from ..trader.swing_trader import SwingTrader

logger = logging.getLogger(__name__)

# ── 로그 캡처 ──────────────────────────────────────────────────────────────

_LOG_BUFFER: deque = deque(maxlen=300)
_LOG_SUBSCRIBERS: list[queue.Queue] = []
_LOG_LOCK = threading.Lock()


class _WebLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name.split(".")[-1],
            "msg": record.getMessage(),
        }
        _LOG_BUFFER.append(entry)
        with _LOG_LOCK:
            for q in list(_LOG_SUBSCRIBERS):
                try:
                    q.put_nowait(entry)
                except Exception:
                    pass


def _install_log_handler() -> None:
    handler = _WebLogHandler()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)


# ── 봇 관리자 ──────────────────────────────────────────────────────────────

class BotManager:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None
        self._broker: LSBroker | None = None
        self._pm: PortfolioMonitor | None = None
        self._swing: SwingTrader | None = None
        self._day: DayTrader | None = None
        self._notifier: TelegramNotifier | None = None
        self._running = False
        self._lock = threading.Lock()
        self._error: str = ""
        # 매크로 분석
        self._macro_result: MacroResult | None = None
        self._macro_adjuster: ParameterAdjuster | None = None
        self._macro_analyzer = None
        self._historical_kb: dict = {}
        self._macro_lock = threading.Lock()
        # 파이프라인 분석 결과 캐시
        self._pipeline_results: list = []
        self._pipeline_running = False
        self._pipeline_updated_at: str = ""
        self._pipeline_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._error

    def start(self) -> dict:
        with self._lock:
            if self._running:
                return {"ok": False, "msg": "이미 실행 중입니다"}
            try:
                self._error = ""
                self._build_components()
                self._build_scheduler()
                self._scheduler.start()
                self._running = True
                logger.info("웹 대시보드: 봇 시작")
                return {"ok": True, "msg": "봇이 시작되었습니다"}
            except Exception as e:
                self._error = str(e)
                logger.error(f"봇 시작 실패: {e}")
                return {"ok": False, "msg": str(e)}

    def stop(self) -> dict:
        with self._lock:
            if not self._running:
                return {"ok": False, "msg": "봇이 실행 중이 아닙니다"}
            try:
                self._scheduler.shutdown(wait=False)
                self._running = False
                logger.info("웹 대시보드: 봇 정지")
                return {"ok": True, "msg": "봇이 정지되었습니다"}
            except Exception as e:
                return {"ok": False, "msg": str(e)}

    def _ensure_broker(self) -> bool:
        """봇 실행 여부와 무관하게 잔고 조회용 브로커를 초기화"""
        if self._broker:
            return True
        try:
            self._broker = LSBroker()
            self._pm = PortfolioMonitor(self._broker)
            logger.info("잔고 조회용 브로커 초기화 완료")
            return True
        except Exception as e:
            logger.warning(f"브로커 초기화 실패: {e}")
            return False

    def get_portfolio(self) -> dict:
        if not self._ensure_broker():
            return {"error": "브로커 연결 실패"}
        try:
            balance = self._pm.get_balance()
            swing_syms = set(CFG.get("swing", {}).get("watchlist", []))
            day_syms = set(self._day._positions.keys()) if self._day else set()

            swing_pos, day_pos, other_pos = [], [], []
            for pos in balance.positions:
                p = {
                    "symbol": pos.symbol,
                    "qty": pos.quantity,
                    "avg": int(pos.avg_price),
                    "cur": int(pos.current_price),
                    "pct": round(pos.profit_pct, 2),
                    "pl": int(pos.profit_amount),
                }
                if pos.symbol in swing_syms:
                    swing_pos.append(p)
                elif pos.symbol in day_syms:
                    day_pos.append(p)
                else:
                    other_pos.append(p)

            sw_cfg = CFG.get("swing", {})
            dt_cfg = CFG.get("daytrading", {})
            sw_deployed = self._swing.deployed_capital(balance.positions) if self._swing else 0
            dt_deployed = self._day._deployed_capital() if self._day else 0

            return {
                "total_eval": int(balance.total_eval),
                "cash": int(balance.cash),
                "total_pl": int(balance.total_profit_loss),
                "total_pct": round(balance.total_profit_pct, 2),
                "sw_capital": sw_cfg.get("capital", 5_000_000),
                "sw_deployed": sw_deployed,
                "dt_capital": dt_cfg.get("capital", 2_000_000),
                "dt_deployed": dt_deployed,
                "swing_positions": swing_pos,
                "day_positions": day_pos,
                "other_positions": other_pos,
            }
        except Exception as e:
            logger.warning(f"포트폴리오 조회 실패: {e}")
            return {"error": str(e)}

    def get_schedule(self) -> list[dict]:
        if not self._scheduler:
            return []
        result = []
        label_map = {
            "daytrading_prep": "단타 종목 준비",
            "daytrading_run": "단타 체크",
            "swing_run": "스윙 체크",
            "daytrading_force_close": "단타 강제청산",
            "daily_report": "일일 리포트",
        }
        for job in self._scheduler.get_jobs():
            nxt = job.next_run_time
            result.append({
                "id": job.id,
                "label": label_map.get(job.id, job.id),
                "next": nxt.strftime("%H:%M:%S") if nxt else "-",
            })
        return result

    def get_config(self) -> dict:
        sw = CFG.get("swing", {})
        dt = CFG.get("daytrading", {})
        return {
            "swing": {
                "enabled": sw.get("enabled", True),
                "capital": sw.get("capital", 5_000_000),
                "order_amount": sw.get("order_amount", 500_000),
                "stop_loss": sw.get("stop_loss_pct", -5.0),
                "take_profit": sw.get("take_profit_pct", 10.0),
                "watchlist": sw.get("watchlist", []),
            },
            "daytrading": {
                "enabled": dt.get("enabled", True),
                "capital": dt.get("capital", 2_000_000),
                "order_amount": dt.get("order_amount", 200_000),
                "stop_loss": dt.get("stop_loss_pct", -1.5),
                "take_profit": dt.get("take_profit_pct", 2.0),
                "force_close": dt.get("force_close_time", "15:20"),
            },
        }

    def get_config_all(self) -> dict:
        """웹 설정 화면용 전체 설정 반환"""
        sw  = CFG.get("swing", {})
        dt  = CFG.get("daytrading", {})
        news = CFG.get("news_analysis", {})
        mac  = CFG.get("macro_analysis", {})
        sc   = CFG.get("scheduler", {})
        rsi_sw = sw.get("rsi", {})
        return {
            "swing": {
                "enabled":        sw.get("enabled", True),
                "capital":        sw.get("capital", 5_000_000),
                "order_amount":   sw.get("order_amount", 500_000),
                "max_positions":  sw.get("max_positions", 5),
                "stop_loss_pct":  sw.get("stop_loss_pct", -5.0),
                "take_profit_pct":sw.get("take_profit_pct", 10.0),
                "watchlist":      ",".join(str(s) for s in sw.get("watchlist", [])),
                "rsi_period":     rsi_sw.get("period", 14),
                "rsi_oversold":   rsi_sw.get("oversold", 30),
                "rsi_overbought": rsi_sw.get("overbought", 70),
                "candle_interval":rsi_sw.get("candle_interval", "D"),
            },
            "daytrading": {
                "enabled":          dt.get("enabled", True),
                "capital":          dt.get("capital", 2_000_000),
                "order_amount":     dt.get("order_amount", 200_000),
                "max_positions":    dt.get("max_positions", 3),
                "stop_loss_pct":    dt.get("stop_loss_pct", -1.5),
                "take_profit_pct":  dt.get("take_profit_pct", 2.0),
                "force_close_time": dt.get("force_close_time", "15:20"),
                "scan_top_n":       dt.get("scan_top_n", 20),
                "daily_target_pct": dt.get("daily_target_pct", 0.0),
                "rsi_period":       dt.get("rsi_period", 7),
                "rsi_oversold":     dt.get("rsi_oversold", 35),
                "rsi_overbought":   dt.get("rsi_overbought", 65),
                "candle_interval":  str(dt.get("candle_interval", "5")),
            },
            "news_analysis": {
                "enabled":              news.get("enabled", True),
                "provider":             news.get("provider", "auto"),
                "model":                news.get("model", ""),
                "lookback_days":        news.get("lookback_days", 3),
                "block_buy_on_negative":news.get("block_buy_on_negative", True),
                "include_dart":         news.get("include_dart", True),
            },
            "macro_analysis": {
                "enabled":                  mac.get("enabled", True),
                "provider":                 mac.get("provider", "auto"),
                "model":                    mac.get("model", ""),
                "update_interval_minutes":  mac.get("update_interval_minutes", 60),
                "pre_market_time":          mac.get("pre_market_time", "08:20"),
                "apply_to_swing":           mac.get("apply_to_swing", True),
                "apply_to_day":             mac.get("apply_to_day", True),
            },
            "scheduler": {
                "daytrading_prep":          sc.get("daytrading_prep", "08:30"),
                "swing_interval_minutes":   sc.get("swing_interval_minutes", 30),
                "daytrading_force_close":   sc.get("daytrading_force_close", "15:20"),
                "daily_report":             sc.get("daily_report", "15:35"),
            },
        }

    def save_config_all(self, data: dict) -> dict:
        """전체 설정 저장 및 가능한 항목 즉시 적용"""
        try:
            sw  = data.get("swing", {})
            dt  = data.get("daytrading", {})
            news = data.get("news_analysis", {})
            mac  = data.get("macro_analysis", {})
            sc   = data.get("scheduler", {})

            # ── CFG 업데이트 ──
            CFG["swing"].update({
                "enabled":        bool(sw.get("enabled", True)),
                "capital":        int(sw.get("capital", 5_000_000)),
                "order_amount":   int(sw.get("order_amount", 500_000)),
                "max_positions":  int(sw.get("max_positions", 5)),
                "stop_loss_pct":  float(sw.get("stop_loss_pct", -5.0)),
                "take_profit_pct":float(sw.get("take_profit_pct", 10.0)),
                "watchlist":      [s.strip() for s in str(sw.get("watchlist","")).split(",") if s.strip()],
            })
            if "rsi" not in CFG["swing"]:
                CFG["swing"]["rsi"] = {}
            CFG["swing"]["rsi"].update({
                "period":          int(sw.get("rsi_period", 14)),
                "oversold":        float(sw.get("rsi_oversold", 30)),
                "overbought":      float(sw.get("rsi_overbought", 70)),
                "candle_interval": str(sw.get("candle_interval", "D")),
            })

            CFG["daytrading"].update({
                "enabled":          bool(dt.get("enabled", True)),
                "capital":          int(dt.get("capital", 2_000_000)),
                "order_amount":     int(dt.get("order_amount", 200_000)),
                "max_positions":    int(dt.get("max_positions", 3)),
                "stop_loss_pct":    float(dt.get("stop_loss_pct", -1.5)),
                "take_profit_pct":  float(dt.get("take_profit_pct", 2.0)),
                "force_close_time": str(dt.get("force_close_time", "15:20")),
                "scan_top_n":       int(dt.get("scan_top_n", 20)),
                "daily_target_pct": float(dt.get("daily_target_pct", 0.0)),
                "rsi_period":       int(dt.get("rsi_period", 7)),
                "rsi_oversold":     float(dt.get("rsi_oversold", 35)),
                "rsi_overbought":   float(dt.get("rsi_overbought", 65)),
                "candle_interval":  str(dt.get("candle_interval", "5")),
            })

            CFG["news_analysis"].update({
                "enabled":               bool(news.get("enabled", True)),
                "provider":              str(news.get("provider", "auto")),
                "model":                 str(news.get("model", "")),
                "lookback_days":         int(news.get("lookback_days", 3)),
                "block_buy_on_negative": bool(news.get("block_buy_on_negative", True)),
                "include_dart":          bool(news.get("include_dart", True)),
            })

            CFG["macro_analysis"].update({
                "enabled":                 bool(mac.get("enabled", True)),
                "provider":                str(mac.get("provider", "auto")),
                "model":                   str(mac.get("model", "")),
                "update_interval_minutes": int(mac.get("update_interval_minutes", 60)),
                "pre_market_time":         str(mac.get("pre_market_time", "08:20")),
                "apply_to_swing":          bool(mac.get("apply_to_swing", True)),
                "apply_to_day":            bool(mac.get("apply_to_day", True)),
            })

            CFG["scheduler"].update({
                "daytrading_prep":        str(sc.get("daytrading_prep", "08:30")),
                "swing_interval_minutes": int(sc.get("swing_interval_minutes", 30)),
                "daytrading_force_close": str(sc.get("daytrading_force_close", "15:20")),
                "daily_report":           str(sc.get("daily_report", "15:35")),
            })

            # ── 즉시 적용 가능한 항목 ──
            if self._swing:
                self._swing.capital       = CFG["swing"]["capital"]
                self._swing.order_amount  = CFG["swing"]["order_amount"]
                self._swing.max_positions = CFG["swing"]["max_positions"]
                self._swing.watchlist     = list(CFG["swing"]["watchlist"])
                if hasattr(self._swing, 'risk_manager'):
                    self._swing.risk_manager.stop_loss_pct  = CFG["swing"]["stop_loss_pct"]
                    self._swing.risk_manager.take_profit_pct= CFG["swing"]["take_profit_pct"]

            if self._day:
                self._day.capital          = CFG["daytrading"]["capital"]
                self._day.order_amount     = CFG["daytrading"]["order_amount"]
                self._day.max_positions    = CFG["daytrading"]["max_positions"]
                self._day.stop_loss_pct    = CFG["daytrading"]["stop_loss_pct"]
                self._day.take_profit_pct  = CFG["daytrading"]["take_profit_pct"]
                self._day.daily_target_pct = CFG["daytrading"]["daily_target_pct"]
                self._day.scan_top_n       = CFG["daytrading"]["scan_top_n"]

            # ── YAML 저장 ──
            settings_path = ROOT / "config" / "settings.yaml"
            with open(settings_path, "w", encoding="utf-8") as f:
                yaml.dump(CFG, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

            logger.info("[설정] 전체 설정 저장 완료")
            needs_restart = self._running  # 실행 중이면 일부 변경사항은 재시작 필요
            return {
                "ok": True,
                "msg": "설정이 저장되었습니다" + (" (스케줄러/전략 변경사항은 봇 재시작 후 적용)" if needs_restart else ""),
                "needs_restart": needs_restart,
            }
        except Exception as e:
            logger.error(f"[설정] 저장 실패: {e}")
            return {"ok": False, "msg": str(e)}

    def _build_components(self) -> None:
        self._broker = LSBroker()
        self._pm = PortfolioMonitor(self._broker)
        self._notifier = TelegramNotifier()

        sw = CFG.get("swing", {})
        dt = CFG.get("daytrading", {})
        news = CFG.get("news_analysis", {})
        rsi = sw.get("rsi", {})

        if news.get("enabled", False):
            strategy = CombinedStrategy(
                rsi_period=rsi.get("period", 14),
                rsi_oversold=rsi.get("oversold", 30),
                rsi_overbought=rsi.get("overbought", 70),
                lookback_days=news.get("lookback_days", 3),
                block_buy_on_negative=news.get("block_buy_on_negative", True),
                include_dart=news.get("include_dart", True),
                ai_provider=news.get("provider", "auto"),
                ai_model=news.get("model", ""),
            )
        else:
            strategy = RSIStrategy(
                period=rsi.get("period", 14),
                oversold=rsi.get("oversold", 30),
                overbought=rsi.get("overbought", 70),
            )

        self._swing = SwingTrader(
            broker=self._broker,
            strategy=strategy,
            portfolio_monitor=self._pm,
            risk_manager=RiskManager(
                self._broker,
                stop_loss_pct=sw.get("stop_loss_pct", -5.0),
                take_profit_pct=sw.get("take_profit_pct", 10.0),
            ),
            notifier=self._notifier,
            watchlist=sw.get("watchlist", []),
            capital=sw.get("capital", 5_000_000),
            order_amount=sw.get("order_amount", 500_000),
            max_positions=sw.get("max_positions", 5),
            candle_interval=rsi.get("candle_interval", "D"),
        )
        self._day = DayTrader(
            broker=self._broker,
            portfolio_monitor=self._pm,
            notifier=self._notifier,
            scan_top_n=dt.get("scan_top_n", 20),
            capital=dt.get("capital", 2_000_000),
            order_amount=dt.get("order_amount", 200_000),
            take_profit_pct=dt.get("take_profit_pct", 2.0),
            stop_loss_pct=dt.get("stop_loss_pct", -1.5),
            max_positions=dt.get("max_positions", 3),
            rsi_period=dt.get("rsi_period", 7),
            rsi_oversold=dt.get("rsi_oversold", 35.0),
            rsi_overbought=dt.get("rsi_overbought", 65.0),
            candle_interval=str(dt.get("candle_interval", "5")),
            force_close_time=dt.get("force_close_time", "15:20"),
            daily_target_pct=dt.get("daily_target_pct", 0.0),
        )

        # 매크로 분석 초기화
        macro = CFG.get("macro_analysis", {})
        if macro.get("enabled", True):
            self._macro_analyzer = create_macro_analyzer(
                provider=macro.get("provider", "auto"),
                model=macro.get("model", ""),
            )
            self._macro_adjuster = ParameterAdjuster()
            # KB 빌드를 데몬 스레드로 실행
            t = threading.Thread(target=self._refresh_historical_kb, daemon=True)
            t.start()

    def _build_scheduler(self) -> None:
        sc = CFG.get("scheduler", {})
        sw = CFG.get("swing", {})
        dt = CFG.get("daytrading", {})
        prep = sc.get("daytrading_prep", "08:30").split(":")
        force = sc.get("daytrading_force_close", "15:20").split(":")
        rep = sc.get("daily_report", "15:35").split(":")
        interval = sc.get("swing_interval_minutes", 30)

        self._scheduler = BackgroundScheduler(timezone="Asia/Seoul")

        if dt.get("enabled", True):
            self._scheduler.add_job(self._morning_prep_with_macro, "cron",
                day_of_week="mon-fri", hour=prep[0], minute=prep[1], id="daytrading_prep")
            self._scheduler.add_job(self._day.run, "cron",
                day_of_week="mon-fri", hour="9-15", minute="1-59", id="daytrading_run")
            self._scheduler.add_job(self._day.force_close_all, "cron",
                day_of_week="mon-fri", hour=force[0], minute=force[1], id="daytrading_force_close")

        if sw.get("enabled", True):
            self._scheduler.add_job(self._swing.run, "cron",
                day_of_week="mon-fri", hour="9-15", minute=f"1-59/{interval}", id="swing_run")

        self._scheduler.add_job(
            lambda: self._notifier.notify_portfolio(
                f"일일 마감\n{self._pm.format_report()}"
            ),
            "cron", day_of_week="mon-fri", hour=rep[0], minute=rep[1], id="daily_report",
        )

        # 매크로 분석 스케줄
        macro = CFG.get("macro_analysis", {})
        if macro.get("enabled", True):
            pre = macro.get("pre_market_time", "08:20").split(":")
            interval = macro.get("update_interval_minutes", 60)
            self._scheduler.add_job(
                self._run_macro_update, "cron",
                day_of_week="mon-fri", hour=pre[0], minute=pre[1],
                id="macro_pre_market",
            )
            self._scheduler.add_job(
                self._run_macro_update, "interval",
                minutes=interval, id="macro_intraday",
            )
            self._scheduler.add_job(
                self._refresh_historical_kb, "cron",
                day_of_week="sun", hour="2", minute="0",
                id="macro_kb_refresh",
            )


    def _morning_prep_with_macro(self) -> None:
        """매크로 점수를 포함해 단타 아침 준비 실행"""
        macro_score = "neutral"
        with self._macro_lock:
            if self._macro_result:
                macro_score = self._macro_result.analysis.score
        if self._day:
            self._day.morning_prep(macro_score=macro_score)

    def get_recommendation(self) -> dict:
        """현재 단타 추천 정보 반환"""
        if not self._day or not self._day._recommendation:
            return {"available": False}
        rec = self._day._recommendation
        return {"available": True, **rec.to_dict()}

    def _run_macro_update(self) -> None:
        """글로벌 시장 데이터 수집 → AI 분석 → 파라미터 조정"""
        if not self._macro_analyzer:
            return
        try:
            market_data = fetch_market_data()
            historical_ctx = get_as_text(self._historical_kb)
            result = self._macro_analyzer.analyze(market_data, historical_ctx)
            with self._macro_lock:
                self._macro_result = result
            logger.info(f"[매크로] 분석 완료: {result.analysis.score} (확신도 {result.analysis.confidence:.0%})")

            macro = CFG.get("macro_analysis", {})
            if self._macro_adjuster:
                sw = self._swing if macro.get("apply_to_swing", True) else None
                dt = self._day  if macro.get("apply_to_day",   True) else None
                self._macro_adjuster.apply(result.analysis.score, sw, dt)
        except Exception as e:
            logger.error(f"[매크로] 업데이트 오류: {e}")

    def _refresh_historical_kb(self) -> None:
        """15년 통계 지식베이스 갱신"""
        try:
            kb = build_knowledge_base()
            self._historical_kb = kb
            logger.info("[매크로] 지식베이스 준비 완료")
        except Exception as e:
            logger.error(f"[매크로] 지식베이스 갱신 오류: {e}")

    def run_pipeline(self, symbols: list[str] | None = None) -> dict:
        """파이프라인 분석 실행 (백그라운드 스레드)"""
        with self._pipeline_lock:
            if self._pipeline_running:
                return {"ok": False, "msg": "이미 분석 중입니다"}
            self._pipeline_running = True

        def _worker():
            try:
                if not self._ensure_broker():
                    return
                analyzer = StockAnalyzer(self._broker, scan_top_n=30, backtest_months=3)
                results = analyzer.run(symbols)
                with self._pipeline_lock:
                    self._pipeline_results = [r.to_dict() for r in results]
                    self._pipeline_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"[파이프라인] 분석 완료: {len(results)}개 종목")
            except Exception as e:
                logger.error(f"[파이프라인] 오류: {e}")
            finally:
                with self._pipeline_lock:
                    self._pipeline_running = False

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "msg": "파이프라인 분석을 시작했습니다"}

    def get_pipeline_status(self) -> dict:
        with self._pipeline_lock:
            return {
                "running": self._pipeline_running,
                "updated_at": self._pipeline_updated_at,
                "results": self._pipeline_results,
                "count": len(self._pipeline_results),
            }

    def get_macro_status(self) -> dict:
        """대시보드용 매크로 분석 JSON"""
        with self._macro_lock:
            result = self._macro_result

        if result is None:
            return {
                "available": False,
                "score": "neutral",
                "confidence": 0,
                "key_factors": [],
                "reasoning": "아직 분석 데이터 없음",
                "us_overnight": "flat",
                "provider": "none",
                "updated_at": "",
                "indices": [],
                "usd_krw": 0,
                "usd_krw_change_pct": 0,
            }

        a = result.analysis
        md = result.market_data
        return {
            "available": True,
            "score": a.score,
            "confidence": round(a.confidence, 2),
            "key_factors": a.key_factors,
            "reasoning": a.reasoning,
            "us_overnight": a.us_overnight,
            "provider": result.provider,
            "updated_at": result.updated_at,
            "indices": [
                {
                    "name": idx.name,
                    "symbol": idx.symbol,
                    "price": idx.price,
                    "change_pct": idx.change_pct,
                }
                for idx in md.indices
            ],
            "usd_krw": md.usd_krw,
            "usd_krw_change_pct": md.usd_krw_change_pct,
        }


# ── Flask 앱 팩토리 ────────────────────────────────────────────────────────

bot = BotManager()

# ── 인증 헬퍼 ──────────────────────────────────────────────────────────────

def _get_credentials():
    username = os.getenv("WEB_USERNAME", "admin")
    password = os.getenv("WEB_PASSWORD", "")
    return username, password

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def _auth_enabled() -> bool:
    _, pwd = _get_credentials()
    return bool(pwd)

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _auth_enabled():
            return f(*args, **kwargs)
        if not session.get("logged_in"):
            # SSE/API 요청은 401 반환
            if request.path.startswith("/api") or request.path.startswith("/stream"):
                return jsonify({"error": "인증 필요"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def create_app() -> Flask:
    _install_log_handler()
    templates = Path(__file__).parent / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "krx-autotrader-secret-change-me")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not _auth_enabled():
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            username, password = _get_credentials()
            inp_user = request.form.get("username", "")
            inp_pwd  = request.form.get("password", "")
            if inp_user == username and _hash(inp_pwd) == _hash(password):
                session["logged_in"] = True
                session.permanent = False
                return redirect(url_for("index"))
            error = "아이디 또는 비밀번호가 올바르지 않습니다"
        return render_template("login.html", error=error, auth_enabled=_auth_enabled())

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    @login_required
    def api_status():
        return jsonify({
            "running": bot.running,
            "error": bot.last_error,
            "schedule": bot.get_schedule(),
            "config": bot.get_config(),
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    @app.route("/api/portfolio")
    @login_required
    def api_portfolio():
        return jsonify(bot.get_portfolio())

    @app.route("/api/watchlist")
    @login_required
    def api_watchlist():
        if not bot._ensure_broker():
            return jsonify({"error": "브로커 연결 실패"})
        sw_symbols = CFG.get("swing", {}).get("watchlist", [])
        dt_symbols = list(bot._day._watchlist) if bot._day else []

        def fetch_quotes(symbols):
            quotes = []
            for sym in symbols:
                try:
                    q = bot._broker.get_quote(sym)
                    quotes.append(q)
                except Exception as e:
                    logger.warning(f"[{sym}] 시세 조회 실패: {e}")
            return quotes

        return jsonify({
            "swing": fetch_quotes(sw_symbols),
            "daytrading": fetch_quotes(dt_symbols),
        })

    @app.route("/api/logs")
    @login_required
    def api_logs():
        return jsonify(list(_LOG_BUFFER))

    @app.route("/api/macro")
    @login_required
    def api_macro():
        return jsonify(bot.get_macro_status())

    @app.route("/api/config/all", methods=["GET"])
    @login_required
    def api_config_all():
        return jsonify(bot.get_config_all())

    @app.route("/api/config/all", methods=["POST"])
    @login_required
    def api_config_save():
        data = request.get_json(force=True) or {}
        return jsonify(bot.save_config_all(data))

    @app.route("/api/pipeline", methods=["GET"])
    @login_required
    def api_pipeline_status():
        return jsonify(bot.get_pipeline_status())

    @app.route("/api/pipeline", methods=["POST"])
    @login_required
    def api_pipeline_run():
        data = request.get_json(force=True) or {}
        symbols = data.get("symbols") or None  # None이면 자동 스캔
        return jsonify(bot.run_pipeline(symbols))

    @app.route("/api/daytrading/target", methods=["POST"])
    @login_required
    def api_set_daily_target():
        data = request.get_json(force=True) or {}
        try:
            pct = float(data.get("daily_target_pct", 0))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "올바른 숫자를 입력하세요"})
        if pct < 0 or pct > 10:
            return jsonify({"ok": False, "msg": "목표수익률은 0~10% 사이여야 합니다"})

        CFG["daytrading"]["daily_target_pct"] = pct
        if bot._day:
            bot._day.daily_target_pct = pct

        settings_path = ROOT / "config" / "settings.yaml"
        with open(settings_path, "w", encoding="utf-8") as f:
            yaml.dump(CFG, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info(f"[단타] 일일 목표수익률 변경: {pct}%")
        return jsonify({"ok": True, "msg": f"목표수익률 {pct}% 적용 완료"})

    @app.route("/api/daytrading/recommendation")
    @login_required
    def api_recommendation():
        return jsonify(bot.get_recommendation())

    @app.route("/api/daytrading/set-mode", methods=["POST"])
    @login_required
    def api_set_mode():
        """웹에서 투자 방식 직접 선택"""
        from ..pipeline.strategy_recommender import apply_allocation, MODES, _calc_allocations, recommend as _rec
        data = request.get_json(force=True) or {}
        mode = data.get("mode", "").upper()
        if mode not in MODES:
            return jsonify({"ok": False, "msg": f"유효하지 않은 모드: {mode}"})
        if not bot._day:
            return jsonify({"ok": False, "msg": "단타 트레이더 미초기화"})

        day = bot._day
        scored = list(day._pipeline_scores.values())
        buys = [s for s in scored if s.decision in ("매수", "강력매수")]
        buys.sort(key=lambda x: x.total_score, reverse=True)

        allocs = _calc_allocations(mode, buys, day.capital, day.order_amount)
        if day._recommendation:
            day._recommendation.mode = mode
            day._recommendation.mode_label = MODES[mode]
            day._recommendation.allocations = allocs
        day._mode_allocations = {a.symbol: a.amount for a in allocs}
        if mode == "HOLD":
            day._watchlist = []
        else:
            day._watchlist = [a.symbol for a in allocs]

        logger.info(f"[단타] 웹에서 투자 모드 변경: {MODES[mode]}")
        return jsonify({
            "ok": True,
            "msg": f"{MODES[mode]} 모드 적용 완료",
            "watchlist": day._watchlist,
            "allocations": [
                {"symbol": a.symbol, "company_name": a.company_name,
                 "amount": a.amount, "ratio": round(a.ratio * 100, 1)}
                for a in allocs
            ],
        })

    @app.route("/api/daytrading/status")
    @login_required
    def api_daytrading_status():
        if not bot._day:
            return jsonify({"error": "단타 트레이더 미초기화"})
        day = bot._day
        return jsonify({
            "daily_target_pct": day.daily_target_pct,
            "daily_target_amount": day.daily_target_amount,
            "daily_realized_pnl": day._daily_realized_pnl,
            "target_achieved": day.target_achieved,
            "watchlist": day._watchlist,
            "positions": day.get_positions_summary(),
            "pipeline_scores": [
                {
                    "symbol": s.symbol,
                    "company_name": s.company_name,
                    "total_score": s.total_score,
                    "decision": s.decision,
                }
                for s in day._pipeline_scores.values()
                if s.symbol in day._watchlist
            ],
        })

    @app.route("/api/start", methods=["POST"])
    @login_required
    def api_start():
        return jsonify(bot.start())

    @app.route("/api/stop", methods=["POST"])
    @login_required
    def api_stop():
        return jsonify(bot.stop())

    @app.route("/api/config/capital", methods=["POST"])
    @login_required
    def api_config_capital():
        data = request.get_json(force=True)
        try:
            sw_cap = int(data.get("swing_capital", 0))
            dt_cap = int(data.get("dt_capital", 0))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "올바른 숫자를 입력하세요"})

        if sw_cap < 0 or dt_cap < 0:
            return jsonify({"ok": False, "msg": "금액은 0 이상이어야 합니다"})

        # 예수금 초과 여부 확인
        portfolio = bot.get_portfolio()
        cash = portfolio.get("cash", 0)
        if cash > 0 and sw_cap + dt_cap > cash:
            return jsonify({
                "ok": False,
                "msg": f"배분 합계 {sw_cap + dt_cap:,}원이 예수금 {cash:,}원을 초과합니다"
            })

        # 메모리 CFG 업데이트
        CFG["swing"]["capital"] = sw_cap
        CFG["daytrading"]["capital"] = dt_cap

        # 실행 중인 트레이더에 즉시 반영
        if bot._swing:
            bot._swing.capital = sw_cap
        if bot._day:
            bot._day.capital = dt_cap

        # settings.yaml 저장
        settings_path = ROOT / "config" / "settings.yaml"
        with open(settings_path, "w", encoding="utf-8") as f:
            yaml.dump(CFG, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info(f"시드머니 변경: 스윙 {sw_cap:,}원 / 단타 {dt_cap:,}원")
        return jsonify({
            "ok": True,
            "msg": f"스윙 {sw_cap:,}원 / 단타 {dt_cap:,}원 적용 완료",
            "swing_capital": sw_cap,
            "dt_capital": dt_cap,
            "cash": cash,
            "unallocated": cash - sw_cap - dt_cap,
        })

    @app.route("/stream/logs")
    @login_required
    def stream_logs():
        q: queue.Queue = queue.Queue(maxsize=200)
        with _LOG_LOCK:
            _LOG_SUBSCRIBERS.append(q)

        def generate():
            for entry in list(_LOG_BUFFER):
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            try:
                while True:
                    try:
                        entry = q.get(timeout=25)
                        yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    except queue.Empty:
                        yield 'data: {"type":"ping"}\n\n'
            finally:
                with _LOG_LOCK:
                    try:
                        _LOG_SUBSCRIBERS.remove(q)
                    except ValueError:
                        pass

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
