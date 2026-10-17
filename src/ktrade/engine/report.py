"""성적 보고 — 매매조건·버전별로 (A) 실제 실행 기록과 (B) 계획 자체의 결과(체결 여부 무관, 일봉 근사)를 함께 본다.

모의 운영은 건수가 적어 실행 기록(A)만으로는 판단이 어렵다. 지난 계획 파일 전부를 백테스트와 같은 규칙으로
따라가 본 결과(B)를 같이 보면, 전략이 쓸모 있는지 더 일찍 드러난다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ktrade.data.store import Store
from ktrade.engine.backtest import simulate
from ktrade.sdk.models import Plan


def executed(store: Store) -> pd.DataFrame:
    t = store.plan_trades(100000)
    if t.empty:
        return pd.DataFrame()
    closed = t[t["status"] == "closed"].copy()
    closed["ret_pct"] = (closed["exit_price"] / closed["entry_price"] - 1) * 100
    g = closed.groupby(["strategy", "version"])["ret_pct"]
    out = pd.DataFrame({"closed": g.count(), "win_rate": g.apply(lambda s: (s > 0).mean() * 100), "avg_ret": g.mean()})
    opn = t[t["status"].isin(["submitted", "open", "closing"])].groupby(["strategy", "version"]).size().rename("open")
    return out.join(opn, how="outer").fillna({"open": 0}).round(2)


def plan_outcomes(store: Store, plans_dir: Path, cost_pct: float = 0.25) -> pd.DataFrame:
    rows = []
    for f in sorted(plans_dir.glob("*/*.json")):
        if f.name.startswith("_"):
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        for p in doc.get("plans", []):
            res = simulate(store, Plan.from_dict(p), doc["asof"], cost_pct) or {}
            rows.append({"strategy": doc["strategy"], "version": doc["version"], "asof": doc["asof"], "symbol": p["symbol"],
                         "triggered": res.get("triggered"), "closed": "exit" in res, "ret_pct": res.get("ret_pct"),
                         "reason": res.get("reason")})
    return pd.DataFrame(rows)


def text(store: Store, plans_dir: Path) -> str:
    lines = ["== A. 실행된 매매 (plan_trades, 주문가 기준 근사) =="]
    ex = executed(store)
    lines.append(ex.to_string() if not ex.empty else "아직 없음")
    lines.append("\n== B. 계획 결과 (체결 여부 무관, 일봉 근사, 비용 0.25% 반영) ==")
    po = plan_outcomes(store, plans_dir)
    if po.empty:
        lines.append("계획 파일 없음")
    else:
        for (sid, ver), g in po.groupby(["strategy", "version"]):
            c = g[g["closed"]]
            lines.append(f"{sid} {ver}: 계획 {len(g)} · 발동 {int(g['triggered'].fillna(False).sum())} · 청산 {len(c)}"
                         + (f" · 승률 {(c['ret_pct'] > 0).mean() * 100:.0f}% · 평균 {c['ret_pct'].mean():+.2f}%" if len(c) else ""))
    lines.append("\n주의: 일봉 근사·생존편향(상장폐지 종목 없음). 결과는 매매조건 version 단위로 비교하세요.")
    return "\n".join(lines)
