"""실험 기록 — 백테스트 한 번 = 실험 한 건. 매매조건을 바꾸거나 더할 때마다 쌓여서 이전 실험과 비교된다.

    <KTRADE_HOME>/experiments/<조건 id>/<실행시각>_<setup>.json   지표·설정
    <KTRADE_HOME>/experiments/<조건 id>/<실행시각>_<setup>.csv    거래 목록

setup = 조건 코드 지문(code_hash) + 파라미터 + 청산 설정을 묶은 지문. 셋 중 하나라도 바뀌면 다른 실험이다.
planner는 지금 setup으로 실험한 기록이 없는 조건을 실행하지 않는다(strategies.require_backtest).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from ktrade.config import paths
from ktrade.engine import exits as X

KEYS = ("trades", "win_rate", "win_rate_ci95", "avg_ret", "avg_ret_ci95", "median_ret", "avg_win", "avg_loss", "profit_factor",
        "max_consec_losses", "max_drawdown_pct_pts", "avg_excess", "avg_days", "picks", "triggered", "trigger_rate",
        "in_sample", "out_of_sample", "consistent", "by_reason", "by_year", "start", "end", "days", "cost_pct", "still_open", "universe")


def setup_hash(info, params: dict, rules: dict) -> str:
    blob = json.dumps({"code": info.code_hash, "params": params, "exits": rules}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


def _dir(cfg: dict, sid: str) -> Path:
    return paths(cfg)["home"] / "experiments" / sid


def save(cfg: dict, info, params: dict, rules: dict, result: dict, note: str = "") -> Path:
    d = _dir(cfg, info.id)
    d.mkdir(parents=True, exist_ok=True)
    setup = setup_hash(info, params, rules)
    run_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    rec = {"id": info.id, "version": info.version, "code_hash": info.code_hash, "setup": setup, "run_at": run_at,
           "params": params, "exits": X.describe(rules), "exits_raw": rules, "note": note,
           **{k: result[k] for k in KEYS if k in result}}
    path = d / f"{run_at}_{setup}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    table = result.get("table")
    if table is not None and len(table):
        table.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return path


def history(cfg: dict, sid: str) -> list[dict]:
    d = _dir(cfg, sid)
    if not d.is_dir():
        return []
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(d.glob("*.json"), key=lambda f: (f.name[:15], f.stat().st_mtime_ns))]


def has_run(cfg: dict, sid: str, setup: str) -> bool:
    return any(r.get("setup") == setup for r in history(cfg, sid))


def _ci(v) -> str:
    return f"{v[0]:.0f}~{v[1]:.0f}" if v else "-"


def table(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        rows.append({"실행": r["run_at"], "조건": f"{r['id']} {r['version']}", "setup": r["setup"],
                     "기간": f"{r.get('start')}~{r.get('end')}", "대상": r.get("universe", "DB 전 종목"), "거래": r.get("trades", 0),
                     "승률%": round(r["win_rate"], 1) if r.get("win_rate") is not None else None,
                     "승률95%": _ci(r.get("win_rate_ci95")),
                     "평균%": round(r["avg_ret"], 2) if r.get("avg_ret") is not None else None,
                     "PF": round(r["profit_factor"], 2) if r.get("profit_factor") else None,
                     "시장대비%p": round(r["avg_excess"], 2) if r.get("avg_excess") is not None else None,
                     "앞뒤 일관": {True: "예", False: "아니오"}.get(r.get("consistent"), "-"),
                     "청산": r.get("exits", "")})
    return pd.DataFrame(rows)


def diff(prev: dict | None, cur: dict) -> str:
    if not prev:
        return "이 조건의 첫 실험입니다."
    def d(k, fmt="{:+.2f}"):
        a, b = prev.get(k), cur.get(k)
        return fmt.format(b - a) if a is not None and b is not None else "-"
    what = []
    if prev.get("code_hash") != cur.get("code_hash"):
        what.append("조건 코드")
    if prev.get("params") != cur.get("params"):
        what.append("파라미터")
    if prev.get("exits_raw") != cur.get("exits_raw"):
        what.append("청산 설정")
    if prev.get("universe", "DB 전 종목") != cur.get("universe", "DB 전 종목"):
        what.append("대상 종목")
    changed = ", ".join(what) or "바뀐 것 없음(기간·비용·DB 데이터만 다를 수 있음)"
    return (f"직전 실험({prev['run_at']}) 대비 — 바뀐 것: {changed}\n"
            f"  거래 {d('trades', '{:+.0f}')}건, 승률 {d('win_rate', '{:+.1f}')}%p, 평균 수익 {d('avg_ret')}%p, "
            f"시장 대비 {d('avg_excess')}%p")
