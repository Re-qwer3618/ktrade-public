"""백테스트 — 매매조건 + 엔진 손절·익절을 과거 일봉으로 돌려 '이 조건이 얼마나 자주, 얼마나 맞는지' 잰다.

거래일마다 그날까지의 데이터(DataView)로 select(ctx) → 엔진 청산 규칙으로 Plan 조립 → 엔진 검사 → 다음 거래일부터 일봉으로 따라간다.

매수 (실행일 일봉 근사):
  after ≤ 09:05 (장 시작 매수·눌림): 시가가 매수 구간 안이면 시가, 시가가 구간 위인데 저가가 구간에 닿으면 max_price,
                                   시가가 구간 아래인데 고가가 구간에 닿으면 min_price
  그 밖(장 후반 확인 등): 종가가 구간 안이면 종가
  공통: 시가 >= min_open 이어야 함
매도 (매수 다음 날부터, 실제 매수가 기준 손절·목표 — engine/exits.py와 같은 계산):
  하루 안에서 손절과 목표가 둘 다 닿을 수 있으면 **손절로 본다** (일봉으로는 순서를 모름 — 확률을 부풀리지 않으려는 보수적 가정)
  분할매수·분할익절(exits.scale_in / scale_out)은 exits.PositionSim이 같은 순서로 계산 — 손절·목표는 평균 매수가 기준
  stop_check: intraday = 저가가 손절가에 닿으면 손절가(시가가 이미 아래면 시가) / close = 종가 < 손절가이면 종가
  추적 손절: 전날까지의 최고가 기준, 저가가 닿으면 그 가격 / 목표: 고가 >= 목표가 → 목표가(시가가 이미 위면 시가)
  보유기간 만료: max_hold_days째 종가
비용: 왕복 cost_pct(기본 0.25% = 수수료 양쪽 + 거래세 근사)를 뺀다. 보유 중인 종목은 같은 조건이 또 골라도 사지 않는다(실전과 같음).
한계: 일봉 근사, 생존편향(상장폐지 종목이 DB에 없으면 실제보다 좋게 나옴), 체결 미끄러짐·호가 미반영.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ktrade.data.store import Store
from ktrade.engine import exits as X
from ktrade.engine import guard
from ktrade.engine.planner import compose
from ktrade.sdk.context import INDEX_CODE, Context, DataView
from ktrade.sdk.models import Plan


@dataclass
class Trade:
    strategy: str
    asof: str
    symbol: str
    name: str
    entry_date: str
    entry: float
    exit_date: str
    exit: float
    reason: str
    days: int
    ret_pct: float
    mkt_pct: float | None
    meta: str = ""


def trading_days(store: Store, start: str, end: str) -> list[str]:
    r = store.query("SELECT date FROM index_ohlcv WHERE index_code = '001' AND date BETWEEN ? AND ? ORDER BY date", (start, end))
    return list(r["date"])


def _future(store: Store, symbol: str, after: str, n: int) -> pd.DataFrame:
    return store.query("SELECT date, open, high, low, close FROM ohlcv WHERE symbol = ? AND date > ? ORDER BY date LIMIT ?",
                       (symbol, after, n))


def fill_price(plan: Plan, bar) -> float | None:
    e = plan.entry
    if bar.open < e.min_open:
        return None
    if e.after <= "09:05":
        if e.min_price < bar.open <= e.max_price:
            return float(bar.open)
        if bar.open > e.max_price and bar.low <= e.max_price:
            return float(e.max_price)
        if bar.open <= e.min_price and bar.high > e.min_price:
            return float(e.min_price)
        return None
    return float(bar.close) if e.min_price < bar.close <= e.max_price else None


def simulate(store: Store, plan: Plan, asof: str, cost_pct: float) -> dict | None:
    ex = plan.exits or X.resolve()
    hold = int(plan.max_hold_days)
    f = _future(store, plan.symbol, asof, hold + 1)
    if f.empty:
        return None
    d1 = f.iloc[0]
    entry = fill_price(plan, d1)
    if entry is None:
        return {"triggered": False}
    pos = X.PositionSim(ex, entry)
    early = plan.entry.after <= "09:05"
    # 장 시작에 산 날은 그날 안에서도 (장중) 손절이 날 수 있다 — 분할매수·익절은 다음 날부터. 장 후반 매수는 그날 청산 없음
    if early and pos.step(entry, float(d1.high), float(d1.low), float(d1.close), late=False, stop_only=True):
        return _done(d1.date, d1.date, pos, 1, cost_pct)
    rows = f.iloc[1:hold]
    for k, r in enumerate(rows.itertuples(), start=2):
        if pos.step(float(r.open), float(r.high), float(r.low), float(r.close), time_exit=k == hold):
            return _done(d1.date, r.date, pos, k, cost_pct)
    if hold == 1:
        pos.close_all(float(d1.close), "보유기간 만료")
        return _done(d1.date, d1.date, pos, 1, cost_pct)
    return {"triggered": True, "open": True}           # 데이터 끝까지 보유 중


def _done(d_in, d_out, pos: X.PositionSim, days, cost_pct) -> dict:
    r = pos.result(cost_pct)
    return {"triggered": True, "entry_date": d_in, "entry": float(r["entry"]), "exit_date": d_out, "exit": float(r["exit"]),
            "reason": r["reason"], "days": days, "ret_pct": r["ret_pct"], "fills": r["fills"]}


def _mkt(store: Store, market: str, d0: str, d1: str) -> float | None:
    r = store.query("SELECT date, close FROM index_ohlcv WHERE index_code = ? AND date IN (?, ?)",
                    (INDEX_CODE.get(market, "001"), d0, d1))
    if len(r) < 2:
        return None
    c = dict(zip(r["date"], r["close"]))
    return (c[d1] / c[d0] - 1) * 100


def run(store: Store, info, cond, rules: dict, start: str, end: str, cost_pct: float = 0.25, max_per_day: int = 20,
        oos_ratio: float = 0.3, progress=None, symbols: list[str] | None = None) -> dict:
    """symbols를 주면 매매조건에게 그 종목만 보인다(대상 종목을 좁힌 실험). None = DB에 있는 전 종목."""
    trades, n_picks, n_trig, n_rej, n_open, n_busy = [], 0, 0, 0, 0, 0
    busy: dict[str, str] = {}
    days = trading_days(store, start, end)
    for i, d in enumerate(days):
        view = DataView(store, d, set(symbols) if symbols else None)
        picks = cond.select(Context(asof=d, data=view, params=cond.params, strategy_id=info.id)) or []
        if picks:
            plans = compose(list(picks), rules, view)
            syms = [p.symbol for p in plans]
            r = store.query(f"SELECT symbol, close FROM ohlcv WHERE date = ? AND symbol IN ({','.join('?' * len(syms))})", (d, *syms))
            ok, rej = guard.screen([(info.id, plans)], dict(zip(r["symbol"], r["close"])), max_per_day)
            n_rej += len(rej)
            for p in ok[info.id]:
                n_picks += 1
                if busy.get(p.symbol, "") >= d:
                    n_busy += 1
                    continue
                res = simulate(store, p, d, cost_pct)
                if not res or not res["triggered"]:
                    continue
                n_trig += 1
                if res.get("open"):
                    n_open += 1
                    busy[p.symbol] = "9999-12-31"
                    continue
                busy[p.symbol] = res["exit_date"]
                trades.append(Trade(info.id, d, p.symbol, p.name, res["entry_date"], res["entry"], res["exit_date"], res["exit"],
                                    res["reason"], res["days"], res["ret_pct"],
                                    _mkt(store, p.market or "코스피", res["entry_date"], res["exit_date"]),
                                    str(p.meta)[:200] if p.meta else ""))
        if progress and (i + 1) % 20 == 0:
            progress(i + 1, len(days))
    return summarize(info.id, trades, {"start": start, "end": end, "days": len(days), "picks": n_picks, "triggered": n_trig,
                                       "trigger_rate": (n_trig / n_picks * 100) if n_picks else None, "rejected": n_rej,
                                       "skipped_holding": n_busy, "still_open": n_open, "cost_pct": cost_pct,
                                       "exits": X.describe(rules),
                                       "universe": f"{len(symbols)}종목 지정" if symbols else "DB 전 종목"}, oos_ratio)


# ---------------- 확률 지표 ----------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """승률의 95% 신뢰구간 (윌슨). 거래가 적으면 구간이 넓다 — 승률 숫자 하나만 믿지 않게."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h) * 100, min(1.0, c + h) * 100)


def stats(r: pd.Series) -> dict:
    n = len(r)
    if n == 0:
        return {"trades": 0}
    wins, losses = r[r > 0], r[r <= 0]
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    streak = worst = 0
    for v in r:
        streak = streak + 1 if v <= 0 else 0
        worst = max(worst, streak)
    eq = r.cumsum()
    return {"trades": n, "win_rate": float(len(wins) / n * 100), "win_rate_ci95": wilson(len(wins), n),
            "avg_ret": float(r.mean()),
            "avg_ret_ci95": (float(r.mean() - 1.96 * sd / math.sqrt(n)), float(r.mean() + 1.96 * sd / math.sqrt(n))) if n > 1 else None,
            "median_ret": float(r.median()), "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
            "max_consec_losses": worst, "max_drawdown_pct_pts": float((eq - eq.cummax()).min())}


def summarize(sid: str, trades: list[Trade], extra: dict, oos_ratio: float = 0.3) -> dict:
    df = pd.DataFrame([asdict(t) for t in trades])
    out = {"strategy": sid, **extra}
    if df.empty:
        return out | {"trades": 0, "table": df}
    df = df.sort_values("exit_date").reset_index(drop=True)
    out |= stats(df["ret_pct"])
    out["avg_excess"] = float((df["ret_pct"] - df["mkt_pct"].fillna(0)).mean())
    out["avg_days"] = float(df["days"].mean())
    if len(df) >= 10:
        cut = sorted(df["entry_date"])[int(len(df) * (1 - oos_ratio))]
        a, b = df[df["entry_date"] < cut]["ret_pct"], df[df["entry_date"] >= cut]["ret_pct"]
        out["in_sample"] = {"until": cut, **{k: v for k, v in stats(a).items() if k in ("trades", "win_rate", "avg_ret")}}
        out["out_of_sample"] = {"from": cut, **{k: v for k, v in stats(b).items() if k in ("trades", "win_rate", "avg_ret")}}
        out["consistent"] = bool(len(a) and len(b) and np.sign(a.mean()) == np.sign(b.mean()))
    out["by_reason"] = df.groupby("reason")["ret_pct"].agg(["count", "mean"]).round(2).to_dict("index")
    out["by_year"] = df.assign(y=df["entry_date"].str[:4]).groupby("y")["ret_pct"].agg(["count", "mean"]).round(2).to_dict("index")
    out["table"] = df
    return out
