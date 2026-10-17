"""장 마감 후 한 번에: 수집(증분) → 계획 → 계획·보유 종목 분봉. 작업 스케줄러가 이 명령 하나(ktrade daily)만 부르면 된다.

각 단계 결과와 오류는 <KTRADE_HOME>/logs/daily_<날짜>.json 에 남긴다. 수집이 실패해도 계획 단계는 시도한다
(DB가 오늘 일봉을 못 받았으면 planner가 기준일을 어제로 잡고, 실행 쪽은 '직전 거래일 계획'만 쓰므로 묵은 계획으로
매매하지 않는다).
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import date, datetime

from ktrade.config import paths
from ktrade.data.store import Store

log = logging.getLogger("ktrade.daily")


def run_daily(cfg: dict, collect: bool = True, minute: bool = True, limit: int | None = None) -> dict:
    p = paths(cfg)
    store = Store(p["db"])
    out: dict = {"started": datetime.now().isoformat(timespec="seconds"), "steps": {}}

    def step(name, fn):
        try:
            out["steps"][name] = fn()
        except Exception:
            out["steps"][name] = {"error": traceback.format_exc(limit=4)}
            log.error(f"{name} 실패\n{out['steps'][name]['error']}")

    if collect:
        from ktrade.data.collect import collect as do_collect
        step("collect", lambda: do_collect(store, cfg, limit=limit))
    from ktrade.engine.planner import run_plans
    step("plan", lambda: run_plans(cfg, store))
    if minute and collect:
        from ktrade.data.collect import collect_minute
        asof = store.last_index_date_before("9999-12-31")
        syms = sorted(set(store.active_plan_symbols()) | _plan_symbols(p["plans"], asof))
        if syms and asof:
            step("minute", lambda: collect_minute(store, cfg, syms, asof))
    out["finished"] = datetime.now().isoformat(timespec="seconds")
    p["logs"].mkdir(parents=True, exist_ok=True)
    (p["logs"] / f"daily_{date.today():%Y%m%d}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                                                  encoding="utf-8")
    return out


def _plan_symbols(plans_dir, asof: str | None) -> set[str]:
    if not asof or not (plans_dir / asof).is_dir():
        return set()
    syms = set()
    for f in (plans_dir / asof).glob("*.json"):
        if not f.name.startswith("_"):
            syms |= {p["symbol"] for p in json.loads(f.read_text(encoding="utf-8")).get("plans", [])}
    return syms
