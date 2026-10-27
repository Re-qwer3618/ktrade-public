"""영웅문 조건식 성적표 — 신호 일지(condition_signals) + 1분봉으로 '그때 샀다면' 결과를 매긴다 (포워드 테스트).

조건식은 키움 서버가 계산해 과거 백테스트를 할 수 없으므로, 장중에 실제로 받은 신호만으로 성적을 쌓는다.
  - 모든 편입: 편입 '묶음'마다 첫 편입 시각 가격에 샀다고 보고(필터 없이) — 조건식 자체의 힘.
    같은 종목이 몇 초마다 편입·이탈을 되풀이하는 일이 잦아(한 종목이 하루에 수십 번 편입되기도 함) 이탈 뒤
    episode_gap_min(기본 10분) 안에 다시 들어오면 같은 편입으로 묶는다. 신호 일지(원자료)는 그대로 둔다.
  - 매수 신호: 시간대·가격·한도·확인을 통과한 신호(WOULD_BUY/BUY) — 내 설정까지 포함한 성적
  - 실제 매매: plan_trades(strategy = hts:<조건식>) 중 청산된 것
가상 결과 규칙 (백테스트와 같은 보수적 순서): 매수 시각 다음 분봉부터, 한 봉에서 손절가·목표가가 둘 다 닿으면 손절,
손절 → 추적 손절 → 목표가, max_hold_days 1이면 15:15 봉 종가에 청산, 비용은 backtest.cost_pct(왕복) 차감.
"""
from __future__ import annotations

import pandas as pd

from ktrade.data.store import Store
from ktrade.engine import exits as X
from ktrade.engine.signals import cond_exits, load_conditions

FINAL = ("WOULD_BUY", "BUY", "SKIP", "CONFIRM_FAIL")
INSERT_ROWS = ("WAIT",) + FINAL
EPISODE_GAP_MIN = 10


def episodes(g: pd.DataFrame, gap_min: float = EPISODE_GAP_MIN) -> pd.DataFrame:
    """한 조건식의 신호 일지 → 편입 묶음(첫 편입 행). 이탈(D) 뒤 gap_min 안의 재편입은 같은 묶음, 첫 행의 가격·시각을 쓴다.
    반환 열: date, ts, symbol, price, n_inserts(묶음 안 편입 행 수)."""
    out, last_remove, current = [], {}, {}
    for r in g.sort_values("id").itertuples():
        key = (r.date, r.symbol)
        if r.event == "D":
            last_remove[key] = pd.Timestamp(r.ts)
            continue
        if r.event != "I" or r.action not in INSERT_ROWS:
            continue
        t = pd.Timestamp(r.ts)
        cur = current.get(key)
        gone = last_remove.get(key)
        if cur is None or (gone is not None and gone >= cur["last"] and (t - gone).total_seconds() >= gap_min * 60):
            cur = {"date": r.date, "ts": r.ts, "symbol": r.symbol, "price": r.price, "n_inserts": 0, "last": t}
            current[key] = cur
            out.append(cur)
        cur["n_inserts"] += 1
        cur["last"] = t
        if (cur["price"] is None or cur["price"] != cur["price"]) and r.price == r.price:
            cur["price"] = r.price
    return pd.DataFrame(out, columns=["date", "ts", "symbol", "price", "n_inserts", "last"]).drop(columns="last")


def signal_dates(store: Store, day: str | None = None, days: int | None = None) -> list[str]:
    if day:
        return [day]
    d = store.query("SELECT DISTINCT date FROM condition_signals ORDER BY date DESC LIMIT ?", (int(days or 1),))
    return sorted(d["date"])


def _minutes(store: Store, symbol: str, day: str) -> pd.DataFrame:
    return store.query("SELECT ts, open, high, low, close, volume FROM minute_ohlcv WHERE symbol = ? AND ts BETWEEN ? AND ? "
                       "ORDER BY ts", (symbol, f"{day} 09:00:00", f"{day} 15:30:59"))


def collect_minutes(store: Store, cfg: dict, days: list[str]) -> int:
    """신호가 있었던 종목의 그날 1분봉을 키움에서 받아 DB에 (이미 있으면 건너뜀). 받은 종목·일 수."""
    from ktrade.data.kiwoom import KiwoomData
    api, n = KiwoomData(cfg), 0
    for day in days:
        syms = store.query("SELECT DISTINCT symbol FROM condition_signals WHERE date = ? AND event = 'I'", (day,))["symbol"]
        for sym in syms:
            if len(_minutes(store, sym, day)) >= 300:
                continue
            store.upsert_minute(sym, api.minute(sym, day, max_pages=30))
            n += 1
    return n


def simulate(bars: pd.DataFrame, entry_ts: str, entry: float, ex: dict, cost_pct: float = 0.25) -> dict | None:
    """entry_ts(YYYY-MM-DD HH:MM:SS)에 entry 가격으로 샀다면 — 다음 분봉부터 엔진 청산 규칙. 분봉이 없으면 None."""
    b = bars[bars["ts"] > entry_ts[:16] + ":59"]
    if b.empty or not entry:
        return None
    pos = X.PositionSim(ex, entry)
    after = ex.get("stop_check_after", "15:10")
    one_day = int(ex["max_hold_days"]) <= 1
    exit_ts = ""
    for r in b.itertuples():
        hhmm = r.ts[11:16]
        if pos.step(float(r.open), float(r.high), float(r.low), float(r.close), late=hhmm >= after,
                    time_exit=one_day and hhmm >= "15:15", time_reason="당일 청산"):
            exit_ts = r.ts
            break
    if not pos.closed:
        pos.close_all(float(b["close"].iloc[-1]), "장 마감(분봉 끝)")
        exit_ts = b["ts"].iloc[-1]
    res = pos.result(cost_pct)
    return {"exit_ts": exit_ts, "exit_price": float(res["exit"]), "reason": res["reason"], "ret_pct": res["ret_pct"],
            "entry_avg": float(res["entry"]), "fills": res["fills"],
            "minutes": int((pd.Timestamp(exit_ts) - pd.Timestamp(entry_ts)).total_seconds() // 60)}


def _stats(rets: pd.Series) -> dict:
    rets = rets.dropna()
    if rets.empty:
        return {"n": 0}
    win, loss = rets[rets > 0], rets[rets <= 0]
    return {"n": len(rets), "win_rate": len(win) / len(rets) * 100, "avg": rets.mean(), "median": rets.median(),
            "pf": (win.sum() / -loss.sum()) if len(loss) and loss.sum() < 0 else None, "best": rets.max(), "worst": rets.min()}


def report(store: Store, cfg: dict, days: list[str], cond: str | None = None) -> dict:
    conds = {c["name"]: c for c in load_conditions(cfg)}
    gap = float((cfg.get("hts") or {}).get("episode_gap_min", EPISODE_GAP_MIN))
    cost = float(cfg.get("backtest", {}).get("cost_pct", 0.25))
    sig = pd.concat([store.signals(d, cond) for d in days], ignore_index=True) if days else pd.DataFrame()
    out = {"days": days, "conds": {}, "missing_bars": 0}
    if sig.empty:
        return out
    bars_cache: dict[tuple[str, str], pd.DataFrame] = {}
    for name, g in sig.groupby("cond_name", sort=False):
        c = conds.get(name) or next((v for k, v in conds.items() if k in name or name in k), {"name": name})
        ex = cond_exits(c)

        def sim_rows(frame: pd.DataFrame, kind: str) -> list[dict]:
            rows = []
            for r in frame.itertuples():
                if not r.price or r.price != r.price:
                    continue
                key = (r.symbol, r.date)
                if key not in bars_cache:
                    bars_cache[key] = _minutes(store, r.symbol, r.date)
                sim = simulate(bars_cache[key], r.ts.replace("T", " "), float(r.price), ex, cost)
                if sim is None:
                    out["missing_bars"] += 1
                    continue
                rows.append({"date": r.date, "time": r.ts[11:19], "symbol": r.symbol, "kind": kind,
                             "action": getattr(r, "action", ""), "inserts": getattr(r, "n_inserts", 1), "entry": r.price, **sim})
            return rows
        eps = episodes(g, gap)
        buys = g[(g["event"] == "I") & g["action"].isin(["WOULD_BUY", "BUY"])]
        all_sims = pd.DataFrame(sim_rows(eps, "편입"))
        buy_sims = pd.DataFrame(sim_rows(buys, "매수 신호"))
        sims = pd.concat([all_sims, buy_sims], ignore_index=True)
        trades = store.query("SELECT * FROM plan_trades WHERE strategy = ? AND status = 'closed'", (f"hts:{name}",))
        trades = trades[trades["exit_date"].isin(days)] if not trades.empty else trades
        tr = (trades["exit_price"] / trades["entry_price"] - 1) * 100 - cost if not trades.empty else pd.Series(dtype=float)
        open_now = store.query("SELECT COUNT(*) AS n FROM plan_trades WHERE strategy = ? AND status IN ('submitted','open','closing')",
                               (f"hts:{name}",))["n"].iloc[0]
        out["conds"][name] = {
            "mode": c.get("mode", "watch"), "exits": X.describe(ex),
            "counts": g["action"].value_counts().to_dict(),
            "insert_events": int(((g["event"] == "I") & g["action"].isin(INSERT_ROWS)).sum()),
            "episodes": len(eps), "symbols": int(eps["symbol"].nunique()) if not eps.empty else 0, "gap_min": gap,
            "all": _stats(all_sims["ret_pct"]) if not all_sims.empty else {"n": 0},
            "buy": _stats(buy_sims["ret_pct"]) if not buy_sims.empty else {"n": 0},
            "actual": _stats(tr), "open": int(open_now),
            "by_reason": all_sims.groupby("reason")["ret_pct"].agg(["count", "mean"]).round(2).to_dict("index") if not all_sims.empty else {},
            "by_hour": (all_sims.assign(h=all_sims["time"].str[:2] + "시").groupby("h")["ret_pct"].agg(["count", "mean"]).round(2)
                        .to_dict("index") if not all_sims.empty else {}),
            "table": sims, "trades": trades,
        }
    return out


def _fmt(s: dict) -> str:
    if not s.get("n"):
        return "없음"
    pf = f" · PF {s['pf']:.2f}" if s.get("pf") else ""
    return f"{s['n']}건 · 승률 {s['win_rate']:.0f}% · 평균 {s['avg']:+.2f}% · 중앙값 {s['median']:+.2f}%{pf}"


def render(rep: dict) -> str:
    lines = [f"[조건식 성적표] {rep['days'][0]}~{rep['days'][-1]}" if rep["days"] else "[조건식 성적표]"]
    if not rep["conds"]:
        return lines[0] + "\n  신호 없음"
    for name, r in rep["conds"].items():
        lines.append(f"\n■ {name} ({r['mode']}) — 청산: {r['exits']}")
        cnt = r["counts"]
        lines.append("  신호: " + " · ".join(f"{k} {v}" for k, v in cnt.items()))
        lines.append(f"  편입: 이벤트 {r['insert_events']}번 → {r['symbols']}종목 · 편입 묶음 {r['episodes']}번 "
                     f"(이탈 뒤 {r['gap_min']:.0f}분 안 재편입은 같은 편입으로)")
        lines.append(f"  모든 편입(묶음마다 첫 편입가에 샀다면): {_fmt(r['all'])}")
        lines.append(f"  매수 신호(내 설정 통과): {_fmt(r['buy'])}")
        lines.append(f"  실제 매매(청산): {_fmt(r['actual'])}" + (f" · 보유 중 {r['open']}" if r["open"] else ""))
        if r["by_reason"]:
            lines.append("  청산 사유별: " + ", ".join(f"{k} {v['count']}건 {v['mean']:+.2f}%" for k, v in r["by_reason"].items()))
        if r["by_hour"]:
            lines.append("  편입 시각별: " + ", ".join(f"{k} {v['count']}건 {v['mean']:+.2f}%" for k, v in r["by_hour"].items()))
    if rep["missing_bars"]:
        lines.append(f"\n! 1분봉이 없어 못 매긴 신호 {rep['missing_bars']}건 — ktrade hts report --collect 로 받아서 다시")
    lines.append("주의: 과거 신호에 대한 가상 결과입니다(1분봉 근사, 비용 차감). 미래 수익을 보장하지 않습니다.")
    return "\n".join(lines)
