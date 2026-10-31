"""성적 보고 — 매매조건·버전별로 (A) 실제 실행 기록과 (B) 계획 자체의 결과(체결 여부 무관, 일봉 근사)를 함께 본다.

모의 운영은 건수가 적어 실행 기록(A)만으로는 판단이 어렵다. 지난 계획 파일 전부를 백테스트와 같은 규칙으로
따라가 본 결과(B)를 같이 보면, 전략이 쓸모 있는지 더 일찍 드러난다.
"""
from __future__ import annotations

import json
from datetime import datetime
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


# ───────── 계획 탭: 계획마다 실제로 어떻게 됐나(이유) + 계획대로였다면 ─────────
STATUS_KO = {"submitted": "매수 주문 중", "open": "잔고", "closing": "매도 주문 중", "closed": "매도 완료",
             "unfilled": "주문했지만 미체결", "rejected": "주문 거부"}


def plan_docs(plans_dir: Path) -> dict[str, list[dict]]:
    """{기준일: [조건별 계획 문서]} — 최근 날짜 먼저. '_'로 시작하는 파일(_rejected.json)은 뺀다."""
    out: dict[str, list[dict]] = {}
    for f in sorted(Path(plans_dir).glob("*/*.json"), reverse=True):
        if not f.name.startswith("_"):
            out.setdefault(f.parent.name, []).append(json.loads(f.read_text(encoding="utf-8")))
    return out


def rejected(plans_dir: Path, asof: str) -> dict:
    f = Path(plans_dir) / asof / "_rejected.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"rejected": [], "skipped": {}, "errors": {}}


def exec_day(store: Store, asof: str) -> str:
    """계획을 실행하는 날 = 기준일 다음 거래일 (지수 일봉에 없으면 다음 평일)."""
    import numpy as np
    d = [x for x in store.market.index_dates(start=asof, last=None) if x > asof]
    return d[0] if d else str(np.busday_offset(asof, 1, roll="forward"))


def plan_status(store: Store, pid: str, day: str, now: datetime | None = None, close: str = "15:30") -> tuple[str, str]:
    """(결과, 이유) — 실제 실행 기록(plan_trades)과 판단 기록(decisions)으로."""
    now = now or datetime.now()
    today, after_close = now.strftime("%Y-%m-%d"), now.strftime("%H:%M") >= close
    t = store.query("SELECT * FROM plan_trades WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    if not t.empty:
        r = t.iloc[0]
        why = STATUS_KO.get(r["status"], r["status"])
        if r["status"] == "closed" and r.get("exit_price") and r.get("entry_price"):
            why += f" — {r.get('exit_reason') or ''} ({(r['exit_price'] / r['entry_price'] - 1) * 100:+.2f}%)"
        elif r["status"] == "rejected":
            why += f" — {str(r.get('note') or '')[:120]}"
        return ("매수함" if r["status"] not in ("rejected", "unfilled") else "못 삼"), why
    d = store.query("SELECT signal, detail FROM decisions WHERE detail LIKE ? ORDER BY id DESC LIMIT 1", (pid + " %",))
    ended = day < today or (day == today and after_close)
    if not d.empty:
        sig, detail = d.iloc[0]["signal"], d.iloc[0]["detail"][len(pid):].strip()
        if sig == "PLAN_WAIT":
            return ("안 삼", "장 마감까지 매수 구간에 안 들어옴 (" + detail.split(" — ")[0] + ")") if ended else ("대기", detail)
        return "안 삼", detail
    if ended:
        return "실행 안 됨", "그날 실행 기록 없음 — ktrade run이 안 돌았음 (PC 꺼짐·자동 실행 미등록)"
    return "대기", ("실행일 " + day) if day > today else "매수 조건 시각 전이거나 아직 확인 전"


def plan_review(store: Store, docs: list[dict], asof: str, cost_pct: float = 0.25, simulate_fn=None,
                now: datetime | None = None) -> pd.DataFrame:
    """그날 계획 한 줄씩: 매수 조건 · 실제 결과·이유 · 계획대로였다면(일봉 근사)."""
    sim = simulate_fn or simulate
    day = exec_day(store, asof)
    rows = []
    for doc in docs:
        for p in doc.get("plans", []):
            e = p["entry"]
            got, why = plan_status(store, p["plan_id"], day, now)
            try:
                r = sim(store, Plan.from_dict(p), asof, cost_pct) or {}
            except Exception:
                r = {}
            if not r:
                virt = "데이터 없음"
            elif not r.get("triggered"):
                virt = "조건 미도달"
            elif r.get("open"):
                virt = "도달 · 보유 중"
            else:
                virt = f"도달 · {r.get('reason')} {r.get('ret_pct', 0):+.2f}%"
            rows.append({"매매조건": f"{doc['strategy']} {doc['version']}", "순위": p.get("rank"), "종목": p.get("name"),
                         "코드": p["symbol"], "매수 조건": f"시가≥{e['min_open']:,.0f} · {e['after']} 이후 "
                         f"{e['min_price']:,.0f}~{e['max_price']:,.0f}", "손실제한가": p.get("stop"), "이익실현가": p.get("target"),
                         "결과": got, "이유": why, "계획대로였다면": virt, "가상 수익%": r.get("ret_pct"),
                         "선정 사유": p.get("basis", "")})
    return pd.DataFrame(rows)


def plan_summary(store: Store, all_docs: dict[str, list[dict]], days: int, cost_pct: float = 0.25,
                 now: datetime | None = None) -> pd.DataFrame:
    """최근 days개 기준일의 계획을 매매조건별로: 계획 수 · 실제 매수 · 조건 도달률 · 계획대로였다면 승률·평균."""
    frames = []
    for asof in list(all_docs)[:days]:
        t = plan_review(store, all_docs[asof], asof, cost_pct, now=now)
        if not t.empty:
            frames.append(t.assign(기준일=asof))
    if not frames:
        return pd.DataFrame()
    t = pd.concat(frames)
    out = []
    for sid, g in t.groupby("매매조건"):
        hit = g["계획대로였다면"].str.startswith("도달")
        closed = g["가상 수익%"].notna()
        rets = g.loc[closed, "가상 수익%"].astype(float)
        out.append({"매매조건": sid, "기준일 수": g["기준일"].nunique(), "계획": len(g), "실제 매수": int((g["결과"] == "매수함").sum()),
                    "조건 도달률%": round(hit.mean() * 100, 1), "가상 청산": int(closed.sum()),
                    "가상 승률%": round((rets > 0).mean() * 100, 1) if len(rets) else None,
                    "가상 평균%": round(rets.mean(), 2) if len(rets) else None})
    return pd.DataFrame(out)


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
