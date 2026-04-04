"""15년 KOSDAQ 상관관계 지식베이스 (캐시: 7일 TTL)"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_PATH = Path("data/macro_cache/historical_kb.json")
_CACHE_TTL_DAYS = 7


def _is_cache_valid() -> bool:
    if not _CACHE_PATH.exists():
        return False
    mtime = datetime.fromtimestamp(_CACHE_PATH.stat().st_mtime)
    return datetime.now() - mtime < timedelta(days=_CACHE_TTL_DAYS)


def _download_and_compute() -> dict:
    """yfinance로 15년치 다운로드 후 통계 계산"""
    try:
        import pandas as pd
        import yfinance as yf
    except ImportError:
        logger.error("yfinance/pandas 미설치")
        return {}

    logger.info("[매크로] 15년치 데이터 다운로드 시작...")
    end = datetime.now()
    start = end.replace(year=end.year - 15)
    s = start.strftime("%Y-%m-%d")
    e = end.strftime("%Y-%m-%d")

    try:
        kosdaq = yf.download("^KQ11",   start=s, end=e, progress=False)
        sp500  = yf.download("^GSPC",   start=s, end=e, progress=False)
        nikkei = yf.download("^N225",   start=s, end=e, progress=False)
        usdkrw = yf.download("KRW=X",   start=s, end=e, progress=False)
    except Exception as e_dl:
        logger.error(f"[매크로] 데이터 다운로드 실패: {e_dl}")
        return {}

    if kosdaq.empty or sp500.empty:
        logger.warning("[매크로] 다운로드 데이터 없음")
        return {}

    # 일간 수익률
    kq_ret = kosdaq["Close"].pct_change().shift(-1)  # KOSDAQ 익일 수익률
    sp_ret = sp500["Close"].pct_change()
    ni_ret = nikkei["Close"].pct_change()
    fx_ret = usdkrw["Close"].pct_change()

    df = pd.DataFrame({
        "kq_next": kq_ret,
        "sp_ret":  sp_ret,
        "ni_ret":  ni_ret,
        "fx_ret":  fx_ret,
    }).dropna()

    # MultiIndex 컬럼 플래튼
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    stats: dict = {}

    # 1. S&P500 5구간별 KOSDAQ 익일 패턴
    bins_sp = [-float("inf"), -0.02, -0.005, 0.005, 0.02, float("inf")]
    labels_sp = ["대폭락(-2%↓)", "소폭락(-2~-0.5%)", "보합(-0.5~0.5%)", "소폭등(0.5~2%)", "대폭등(2%↑)"]
    df["sp_bin"] = pd.cut(df["sp_ret"], bins=bins_sp, labels=labels_sp)
    sp_stats = []
    for lbl in labels_sp:
        sub = df[df["sp_bin"] == lbl]["kq_next"]
        if len(sub) > 0:
            sp_stats.append({
                "range": lbl,
                "count": int(len(sub)),
                "avg_ret": round(float(sub.mean()) * 100, 3),
                "up_prob": round(float((sub > 0).mean()) * 100, 1),
            })
    stats["sp500_lead_lag"] = sp_stats

    # 2. USD/KRW 충격 4구간별 KOSDAQ 익일 패턴
    bins_fx = [-float("inf"), -0.01, 0.0, 0.01, float("inf")]
    labels_fx = ["원화강세(-1%↓)", "소폭강세(-1~0%)", "소폭약세(0~1%)", "원화약세(1%↑)"]
    df["fx_bin"] = pd.cut(df["fx_ret"], bins=bins_fx, labels=labels_fx)
    fx_stats = []
    for lbl in labels_fx:
        sub = df[df["fx_bin"] == lbl]["kq_next"]
        if len(sub) > 0:
            fx_stats.append({
                "range": lbl,
                "count": int(len(sub)),
                "avg_ret": round(float(sub.mean()) * 100, 3),
                "up_prob": round(float((sub > 0).mean()) * 100, 1),
            })
    stats["usdkrw_impact"] = fx_stats

    # 3. 월별 계절성
    kq_monthly = kosdaq["Close"].squeeze().pct_change()
    months = kq_monthly.index.month
    monthly_stats = []
    for m in range(1, 13):
        sub = kq_monthly[months == m].dropna()
        if len(sub) > 0:
            monthly_stats.append({
                "month": m,
                "avg_ret": round(float(sub.mean()) * 100, 3),
                "up_prob": round(float((sub > 0).mean()) * 100, 1),
            })
    stats["monthly_seasonality"] = monthly_stats

    # 4. 복합 시나리오
    scenarios = []
    for name, mask in [
        ("미국+일본 동반 하락(각 1%↓)", (df["sp_ret"] < -0.01) & (df["ni_ret"] < -0.01)),
        ("미국+일본 동반 상승(각 1%↑)", (df["sp_ret"] > 0.01)  & (df["ni_ret"] > 0.01)),
        ("미국 급등 + 원화약세",          (df["sp_ret"] > 0.01)  & (df["fx_ret"] > 0.005)),
    ]:
        sub = df[mask]["kq_next"]
        if len(sub) > 0:
            scenarios.append({
                "name": name,
                "count": int(len(sub)),
                "avg_ret": round(float(sub.mean()) * 100, 3),
                "up_prob": round(float((sub > 0).mean()) * 100, 1),
            })
    stats["compound_scenarios"] = scenarios
    stats["computed_at"] = datetime.now().isoformat()
    stats["data_years"] = 15

    logger.info("[매크로] 지식베이스 계산 완료")
    return stats


def build_knowledge_base() -> dict:
    """캐시가 유효하면 로드, 아니면 재계산"""
    if _is_cache_valid():
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                logger.info("[매크로] 지식베이스 캐시 로드")
                return json.load(f)
        except Exception:
            pass

    data = _download_and_compute()
    if data:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def get_as_text(kb: dict) -> str:
    """AI 프롬프트용 텍스트 변환"""
    if not kb:
        return "역사적 데이터 없음"

    lines = ["=== KOSDAQ 15년 상관관계 통계 ==="]

    for s in kb.get("sp500_lead_lag", []):
        if lines[-1] != "[S&P500 → KOSDAQ 익일 패턴]":
            lines.append("\n[S&P500 → KOSDAQ 익일 패턴]")
        lines.append(f"  {s['range']}: 평균수익률 {s['avg_ret']:+.2f}% | 상승확률 {s['up_prob']:.0f}% (n={s['count']})")

    for s in kb.get("usdkrw_impact", []):
        if "[USD/KRW → KOSDAQ 익일 패턴]" not in lines:
            lines.append("\n[USD/KRW → KOSDAQ 익일 패턴]")
        lines.append(f"  {s['range']}: 평균수익률 {s['avg_ret']:+.2f}% | 상승확률 {s['up_prob']:.0f}% (n={s['count']})")

    monthly = kb.get("monthly_seasonality", [])
    if monthly:
        lines.append("\n[월별 계절성 (KOSDAQ 일평균 수익률)]")
        for s in monthly:
            lines.append(f"  {s['month']}월: 평균 {s['avg_ret']:+.3f}% | 상승확률 {s['up_prob']:.0f}%")

    scenarios = kb.get("compound_scenarios", [])
    if scenarios:
        lines.append("\n[복합 시나리오]")
        for s in scenarios:
            lines.append(f"  {s['name']}: 평균수익률 {s['avg_ret']:+.2f}% | 상승확률 {s['up_prob']:.0f}% (n={s['count']})")

    return "\n".join(lines)
