"""
단타 전략 백테스트 엔진 — 주도주 갭업 첫 눌림 (VWAP)

데이터 소스: yfinance
  - 일봉 (1년): EMA 추세 필터, 갭업 계산
  - 5분봉 (60일): VWAP 전략 시뮬레이션

실행 예시:
    from autotrader.backtest.daytrading_backtest import DayTradingBacktest
    bt = DayTradingBacktest()
    report = bt.run(["005930", "000660"], days=45)
    bt.print_report(report)
    bt.plot(report)          # 자본금 변화 그래프
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from ..strategy.vwap_pullback_strategy import VWAPPullbackStrategy
from ..strategy.base import Signal

logger = logging.getLogger(__name__)

# 한국 주식 거래 비용 (왕복)
_BUY_COST_PCT  = 0.00015   # 매수 수수료 0.015%
_SELL_COST_PCT = 0.00315   # 매도 수수료(0.015%) + 증권거래세(0.3%)


# ── 결과 데이터 클래스 ──────────────────────────────────────────────────────

@dataclass
class DayTrade:
    symbol:      str
    date:        str   # YYYYMMDD
    entry_time:  str
    exit_time:   str
    entry_price: float
    exit_price:  float
    quantity:    int
    pnl:         float       # 실현 손익 (비용 포함)
    pnl_pct:     float       # 수익률 %
    reason:      str         # 청산 이유 (익절/손절/VWAP이탈/장마감)
    gap_pct:     float = 0.0 # 당일 갭업률


@dataclass
class SymbolReport:
    symbol:          str
    trades:          list[DayTrade] = field(default_factory=list)
    skipped_days:    int = 0   # EMA/갭업 필터로 제외된 날

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl <= 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.num_trades * 100 if self.num_trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl <= 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")


@dataclass
class BacktestReport:
    symbols:         list[str]
    initial_capital: float
    order_amount:    float
    hard_stop_pct:   float
    take_profit_pct: float
    start_date:      str
    end_date:        str
    results:         list[SymbolReport] = field(default_factory=list)

    @property
    def all_trades(self) -> list[DayTrade]:
        trades = []
        for r in self.results:
            trades.extend(r.trades)
        trades.sort(key=lambda t: (t.date, t.entry_time))
        return trades

    @property
    def total_pnl(self) -> float:
        return sum(r.total_pnl for r in self.results)

    @property
    def total_return_pct(self) -> float:
        return self.total_pnl / self.initial_capital * 100

    @property
    def overall_win_rate(self) -> float:
        all_t = self.all_trades
        return sum(1 for t in all_t if t.pnl > 0) / len(all_t) * 100 if all_t else 0.0


# ── 메인 백테스트 엔진 ─────────────────────────────────────────────────────

class DayTradingBacktest:
    def __init__(
        self,
        hard_stop_pct:   float = 1.0,    # 손절 %
        take_profit_pct: float = 2.0,    # 익절 %
        gap_up_min_pct:  float = 0.5,    # 갭업 최소 기준 %
        vwap_touch_pct:  float = 0.005,
        max_above_vwap:  float = 0.025,
        vol_confirm:     float = 1.3,
        order_amount:    float = 600_000,
        initial_capital: float = 2_000_000,
    ):
        self.hard_stop_pct   = hard_stop_pct
        self.take_profit_pct = take_profit_pct
        self.gap_up_min_pct  = gap_up_min_pct / 100
        self.order_amount    = order_amount
        self.initial_capital = initial_capital
        self._strategy = VWAPPullbackStrategy(
            vwap_touch_pct     = vwap_touch_pct,
            max_above_vwap_pct = max_above_vwap,
            vol_confirm_ratio  = vol_confirm,
        )

    # ── 공개 API ──────────────────────────────────────────────────────────

    def run(
        self,
        symbols: list[str],
        days: int = 45,
    ) -> BacktestReport:
        """
        symbols: 종목 코드 리스트 (예: ["005930", "000660"])
        days: 백테스트 기간 (최대 60, yfinance 5분봉 한계)
        """
        try:
            import yfinance as yf
        except ImportError:
            raise RuntimeError("yfinance가 필요합니다: pip install yfinance")

        days = min(days, 59)
        end   = datetime.now()
        start = end - timedelta(days=days + 10)  # 여유분 포함

        start_str = start.strftime("%Y-%m-%d")
        end_str   = end.strftime("%Y-%m-%d")

        report = BacktestReport(
            symbols         = symbols,
            initial_capital = self.initial_capital,
            order_amount    = self.order_amount,
            hard_stop_pct   = self.hard_stop_pct,
            take_profit_pct = self.take_profit_pct,
            start_date      = start_str,
            end_date        = end_str,
        )

        for symbol in symbols:
            logger.info(f"[백테스트] {symbol} 시작...")
            try:
                sr = self._run_symbol(symbol, yf, days)
                report.results.append(sr)
            except Exception as e:
                logger.error(f"[백테스트] {symbol} 실패: {e}")

        return report

    # ── 종목별 시뮬레이션 ────────────────────────────────────────────────

    def _run_symbol(self, symbol: str, yf, days: int) -> SymbolReport:
        ticker_ks = f"{symbol}.KS"
        ticker_kq = f"{symbol}.KQ"

        # 5분봉 다운로드
        intra = yf.download(ticker_ks, period=f"{days}d", interval="5m",
                            progress=False, auto_adjust=True)
        if intra.empty:
            intra = yf.download(ticker_kq, period=f"{days}d", interval="5m",
                                progress=False, auto_adjust=True)
        if intra.empty:
            logger.warning(f"[백테스트] {symbol}: 5분봉 데이터 없음")
            return SymbolReport(symbol=symbol)

        # 일봉 다운로드 (EMA 계산용, 6개월)
        daily = yf.download(ticker_ks, period="180d", interval="1d",
                            progress=False, auto_adjust=True)
        if daily.empty:
            daily = yf.download(ticker_kq, period="180d", interval="1d",
                                progress=False, auto_adjust=True)

        # MultiIndex 평탄화
        for df in (intra, daily):
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

        intra.index = pd.to_datetime(intra.index)
        daily.index = pd.to_datetime(daily.index)

        # 한국 시간대 보정 (UTC → KST)
        if intra.index.tz is not None:
            import pytz
            intra.index = intra.index.tz_convert("Asia/Seoul").tz_localize(None)

        return self._simulate(symbol, intra, daily)

    def _simulate(self, symbol: str, intra: pd.DataFrame, daily: pd.DataFrame) -> SymbolReport:
        sr = SymbolReport(symbol=symbol)

        # 거래일 목록
        trading_days = sorted(intra.index.normalize().unique())

        for day in trading_days:
            day_str = day.strftime("%Y%m%d")

            # 당일 5분봉
            day_mask = intra.index.normalize() == day
            day_df   = intra[day_mask]
            if len(day_df) < 6:  # 데이터 너무 적으면 스킵
                continue

            # ── [필터1] 일봉 EMA(5) > EMA(20) ─────────────────────────────
            daily_until = daily[daily.index.normalize() < day]
            if len(daily_until) >= 20:
                closes = daily_until["Close"].tolist()
                ema5  = self._calc_ema(closes, 5)
                ema20 = self._calc_ema(closes, 20)
                if ema5 <= ema20:
                    sr.skipped_days += 1
                    continue

            # ── [필터2] 갭업 확인 ───────────────────────────────────────
            prev_days = daily[daily.index.normalize() < day]
            today_open = float(day_df.iloc[0]["Open"])
            if not prev_days.empty:
                yesterday_close = float(prev_days.iloc[-1]["Close"])
                gap_pct = (today_open - yesterday_close) / yesterday_close if yesterday_close > 0 else 0
                if gap_pct < self.gap_up_min_pct:
                    sr.skipped_days += 1
                    continue
            else:
                gap_pct = 0.0

            # ── 당일 분봉 시뮬레이션 ─────────────────────────────────────
            trade = self._simulate_day(symbol, day_str, gap_pct, day_df)
            if trade:
                sr.trades.append(trade)

        logger.info(
            f"[백테스트] {symbol}: "
            f"거래 {sr.num_trades}회 | 승률 {sr.win_rate:.0f}% | "
            f"손익 {sr.total_pnl:+,.0f}원 | 필터제외 {sr.skipped_days}일"
        )
        return sr

    def _simulate_day(
        self,
        symbol: str,
        day_str: str,
        gap_pct: float,
        day_df: pd.DataFrame,
    ) -> DayTrade | None:
        """단일 거래일 5분봉 시뮬레이션"""
        position_price = 0.0
        position_qty   = 0
        entry_time     = ""

        # 오늘 봉 누적하면서 전략 신호 체크
        rows = []
        for idx, row in day_df.iterrows():
            time_str = idx.strftime("%H%M")
            candle = {
                "date":   day_str + time_str,
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": int(row["Volume"]),
            }
            rows.append(candle)

            # 15:00 이후 강제청산
            if time_str >= "1500" and position_qty > 0:
                return self._close_position(
                    symbol, day_str, entry_time, time_str,
                    position_price, float(row["Close"]), position_qty,
                    gap_pct, "장마감"
                )

            current_price = float(row["Close"])

            if position_qty == 0:
                # 진입 체크 (10:00 이후만)
                if time_str < "1000":
                    continue
                if len(rows) < 5:
                    continue

                result = self._strategy.generate_signal(symbol, rows)
                if result.signal == Signal.BUY:
                    qty = int(self.order_amount / current_price)
                    if qty > 0:
                        position_price = current_price * (1 + _BUY_COST_PCT)
                        position_qty   = qty
                        entry_time     = time_str

            else:
                # 청산 체크
                pnl_pct = (current_price - position_price) / position_price * 100

                if pnl_pct >= self.take_profit_pct:
                    return self._close_position(
                        symbol, day_str, entry_time, time_str,
                        position_price, current_price, position_qty,
                        gap_pct, f"익절 +{pnl_pct:.2f}%"
                    )
                if pnl_pct <= -self.hard_stop_pct:
                    return self._close_position(
                        symbol, day_str, entry_time, time_str,
                        position_price, current_price, position_qty,
                        gap_pct, f"손절 {pnl_pct:.2f}%"
                    )

                exit_result = self._strategy.check_exit(symbol, rows, entry_price=position_price)
                if exit_result.signal == Signal.SELL:
                    return self._close_position(
                        symbol, day_str, entry_time, time_str,
                        position_price, current_price, position_qty,
                        gap_pct, exit_result.reason
                    )

        # 당일 미청산 → 종가 청산
        if position_qty > 0:
            last_price = float(day_df.iloc[-1]["Close"])
            return self._close_position(
                symbol, day_str, entry_time, day_df.index[-1].strftime("%H%M"),
                position_price, last_price, position_qty, gap_pct, "장마감"
            )
        return None

    def _close_position(
        self,
        symbol, day_str, entry_time, exit_time,
        entry_price, exit_price, qty, gap_pct, reason,
    ) -> DayTrade:
        sell_price = exit_price * (1 - _SELL_COST_PCT)
        pnl        = (sell_price - entry_price) * qty
        pnl_pct    = (sell_price - entry_price) / entry_price * 100
        return DayTrade(
            symbol      = symbol,
            date        = day_str,
            entry_time  = entry_time,
            exit_time   = exit_time,
            entry_price = round(entry_price, 2),
            exit_price  = round(exit_price, 2),
            quantity    = qty,
            pnl         = round(pnl, 0),
            pnl_pct     = round(pnl_pct, 3),
            reason      = reason,
            gap_pct     = round(gap_pct * 100, 2),
        )

    # ── 리포트 출력 ──────────────────────────────────────────────────────

    def print_report(self, report: BacktestReport) -> None:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"단타 백테스트 결과 ({report.start_date} ~ {report.end_date})")
        print(f"전략: 주도주 갭업 첫 눌림 (VWAP)")
        print(f"손절: -{self.hard_stop_pct}%  익절: +{self.take_profit_pct}%  갭업: +{self.gap_up_min_pct*100:.1f}%+")
        print(sep)

        for sr in report.results:
            print(f"\n[{sr.symbol}] 거래 {sr.num_trades}회 | 필터제외 {sr.skipped_days}일")
            if sr.num_trades == 0:
                print("  → 진입 신호 없음")
                continue
            print(f"  승률        : {sr.win_rate:.1f}%  ({sr.wins}승 {sr.losses}패)")
            print(f"  평균 수익   : {sr.avg_win:+.3f}%")
            print(f"  평균 손실   : {sr.avg_loss:+.3f}%")
            print(f"  수익 배수   : {sr.profit_factor:.2f}x")
            print(f"  총 손익     : {sr.total_pnl:+,.0f}원")
            print()
            print(f"  {'날짜':<10} {'진입':>6} {'청산':>6} {'수익률':>8}  사유")
            print(f"  {'-'*55}")
            for t in sr.trades:
                marker = "▲" if t.pnl > 0 else "▼"
                print(
                    f"  {t.date}  {t.entry_time:>6} {t.exit_time:>6} "
                    f"{marker}{t.pnl_pct:>+7.3f}%  {t.reason}"
                )

        print(f"\n{sep}")
        all_t = report.all_trades
        if all_t:
            print(f"전체 요약:")
            print(f"  총 거래    : {len(all_t)}회")
            print(f"  전체 승률  : {report.overall_win_rate:.1f}%")
            print(f"  총 손익    : {report.total_pnl:+,.0f}원")
            print(f"  수익률     : {report.total_return_pct:+.2f}% "
                  f"({report.initial_capital:,.0f}원 기준)")
        print(sep)

    def plot(self, report: BacktestReport, save_path: str | None = None) -> None:
        """자본금 변화 및 거래 분포 차트"""
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            plt.rcParams["font.family"] = ["AppleGothic", "Malgun Gothic", "sans-serif"]
            plt.rcParams["axes.unicode_minus"] = False
        except ImportError:
            print("matplotlib가 필요합니다: pip install matplotlib")
            return

        all_trades = report.all_trades
        if not all_trades:
            print("거래 데이터 없음 — 차트 생략")
            return

        fig, axes = plt.subplots(2, 2, figsize=(14, 8))
        fig.suptitle(
            f"단타 백테스트: {', '.join(report.symbols)}\n"
            f"손절 -{self.hard_stop_pct}% | 익절 +{self.take_profit_pct}% | "
            f"갭업 +{self.gap_up_min_pct*100:.1f}%+",
            fontsize=12,
        )

        # 1. 누적 손익 곡선
        ax1 = axes[0, 0]
        cum_pnl = [0.0]
        for t in all_trades:
            cum_pnl.append(cum_pnl[-1] + t.pnl)
        ax1.plot(cum_pnl, color="steelblue", linewidth=1.5)
        ax1.axhline(0, color="gray", linestyle="--", linewidth=0.8)
        ax1.set_title("누적 손익 (원)")
        ax1.set_xlabel("거래 횟수")
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

        # 2. 수익률 분포 히스토그램
        ax2 = axes[0, 1]
        pnl_pcts = [t.pnl_pct for t in all_trades]
        wins  = [p for p in pnl_pcts if p > 0]
        loses = [p for p in pnl_pcts if p <= 0]
        if wins:
            ax2.hist(wins,  bins=15, color="salmon",   alpha=0.7, label=f"수익 ({len(wins)}회)")
        if loses:
            ax2.hist(loses, bins=15, color="steelblue", alpha=0.7, label=f"손실 ({len(loses)}회)")
        ax2.axvline(0, color="black", linewidth=0.8)
        ax2.set_title("수익률 분포 (%)")
        ax2.legend()

        # 3. 청산 사유 파이차트
        ax3 = axes[1, 0]
        reasons: dict[str, int] = {}
        for t in all_trades:
            key = t.reason.split(" ")[0]  # "익절", "손절", "장마감", "VWAP"
            reasons[key] = reasons.get(key, 0) + 1
        ax3.pie(reasons.values(), labels=reasons.keys(), autopct="%1.0f%%",
                colors=["#90EE90", "#FFB6C1", "#87CEEB", "#DDA0DD"])
        ax3.set_title("청산 사유")

        # 4. 갭업 크기별 수익률 산점도
        ax4 = axes[1, 1]
        gap_pcts = [t.gap_pct for t in all_trades]
        pnls     = [t.pnl_pct for t in all_trades]
        colors   = ["salmon" if p > 0 else "steelblue" for p in pnls]
        ax4.scatter(gap_pcts, pnls, c=colors, alpha=0.6, s=30)
        ax4.axhline(0, color="gray", linestyle="--", linewidth=0.8)
        ax4.set_xlabel("갭업률 (%)")
        ax4.set_ylabel("수익률 (%)")
        ax4.set_title("갭업 크기 vs 수익률")

        plt.tight_layout()
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"차트 저장: {save_path}")
        else:
            plt.show()

    # ── 내부 유틸 ─────────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(values: list[float], period: int) -> float:
        if len(values) < period:
            return values[-1] if values else 0.0
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema
