"""
KRX 자동매매 웹 대시보드
Flask + SSE 기반 실시간 모니터링 & 제어
"""
import json
import logging
import queue
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

ROOT = Path(__file__).parents[3]
load_dotenv(ROOT / ".env")

with open(ROOT / "config" / "settings.yaml") as f:
    CFG = yaml.safe_load(f)

from ..broker.ls_broker import LSBroker
from ..monitor.portfolio import PortfolioMonitor
from ..monitor.risk_manager import RiskManager
from ..notification.telegram_notifier import TelegramNotifier
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
        )

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
            self._scheduler.add_job(self._day.morning_prep, "cron",
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


# ── Flask 앱 팩토리 ────────────────────────────────────────────────────────

bot = BotManager()


def create_app() -> Flask:
    _install_log_handler()
    templates = Path(__file__).parent / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.config["SECRET_KEY"] = "krx-autotrader"

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def api_status():
        return jsonify({
            "running": bot.running,
            "error": bot.last_error,
            "schedule": bot.get_schedule(),
            "config": bot.get_config(),
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    @app.route("/api/portfolio")
    def api_portfolio():
        return jsonify(bot.get_portfolio())

    @app.route("/api/watchlist")
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
    def api_logs():
        return jsonify(list(_LOG_BUFFER))

    @app.route("/api/start", methods=["POST"])
    def api_start():
        return jsonify(bot.start())

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        return jsonify(bot.stop())

    @app.route("/api/config/capital", methods=["POST"])
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
