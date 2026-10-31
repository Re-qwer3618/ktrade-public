"""장 마감 후 계획 만들기 — 켠 매매조건마다 select(ctx) → 엔진 손절·익절(exits)로 Plan 조립 → 엔진 검사 → 계획 파일.

    <KTRADE_HOME>/plans/<기준일>/<조건 id>.json   조건별 계획 (schema 1)
    <KTRADE_HOME>/plans/<기준일>/_rejected.json   걸러진 계획·건너뛴 조건과 사유

백테스트 먼저(strategies.require_backtest, 기본 true): 조건의 코드·파라미터·청산 설정 조합(setup)으로 백테스트한
기록이 없으면 그 조건은 계획을 만들지 않는다 — 조건을 바꾸면 `ktrade test <id>`로 확인하고 쓰라는 장치.

외부 계획(config external_plans): 다른 프로그램·언어가 만든 계획 파일. stop/target이 없으면 엔진 청산 설정으로 채운다.
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import date, datetime
from pathlib import Path

from ktrade.config import paths
from ktrade.data.store import Store
from ktrade.engine import exits as exits_mod
from ktrade.engine import experiments, guard, loader
from ktrade.sdk.context import Context, DataView
from ktrade.sdk.models import Pick, Plan, plan_file

log = logging.getLogger("ktrade.planner")


def last_trading_day(store: Store, on_or_before: str | None = None) -> str | None:
    d = store.market.index_dates(end=on_or_before or date.today().isoformat(), last=1)
    return d[0] if d else None


def compose(picks: list[Pick], rules: dict, view: DataView) -> list[Plan]:
    """매매조건의 Pick + 엔진 청산 규칙 → Plan. 표시·검사용 손절/목표는 매수 구간 가운데 가격 기준(실제는 매수가 기준)."""
    plans = []
    for pk in picks:
        ex = exits_mod.prepare(rules, view.daily(pk.symbol, bars=max(60, int(rules["stop"].get("value", 20)) + 5)))
        ref = (pk.entry.min_price + pk.entry.max_price) / 2
        stop, target = exits_mod.levels(ex, ref)
        plans.append(Plan(symbol=pk.symbol, entry=pk.entry, stop=stop, target=target, max_hold_days=int(ex["max_hold_days"]),
                          name=pk.name, market=pk.market, rank=pk.rank, reason=pk.reason, meta=pk.meta, exits=ex))
    return plans


def _external(item: dict, asof: str, rules: dict, view: DataView) -> tuple[str, str, list[Plan]] | None:
    d = Path(item["dir"]).expanduser()
    for name in (f"plans_{asof}.json", f"{asof}.json"):
        f = d / name
        if f.exists():
            doc = json.loads(f.read_text(encoding="utf-8"))
            if doc.get("schema") != 1:
                raise ValueError(f"{f}: schema 1이 아님")
            plans = []
            for p in doc["plans"]:
                pl = Plan.from_dict(p)
                if item.get("use_engine_exits") or pl.stop is None or pl.target is None:
                    ex = exits_mod.prepare(rules, view.daily(pl.symbol, 60))
                    s, t = exits_mod.levels(ex, (pl.entry.min_price + pl.entry.max_price) / 2)
                    pl.exits, pl.max_hold_days = ex, int(ex["max_hold_days"])
                    if item.get("use_engine_exits") or pl.stop is None:
                        pl.stop = s
                    if item.get("use_engine_exits") or pl.target is None:
                        pl.target = t
                plans.append(pl)
            return item.get("id") or doc["strategy"], str(doc.get("version", "v1")), plans
    return None


def run_plans(cfg: dict, store: Store, asof: str | None = None, out_dir: Path | None = None,
              only: list[str] | None = None) -> dict:
    asof = asof or last_trading_day(store)
    if not asof:
        raise RuntimeError("지수 일봉이 없어 기준일을 정할 수 없습니다 (먼저 ktrade collect 또는 import-db)")
    out = (out_dir or paths(cfg)["plans"]) / asof
    view = DataView(store, asof)
    produced: list[tuple[str, str, list[Plan], str]] = []
    errors, skipped = {}, {}
    need_bt = bool(cfg["strategies"].get("require_backtest", True))
    found = loader.discover(cfg)
    items = {(e if isinstance(e, dict) else {"id": str(e)})["id"]: (e if isinstance(e, dict) else {})
             for e in cfg["strategies"].get("enabled", [])}
    for info, cond, rules in loader.enabled(cfg):
        if only and info.id not in only:
            continue
        ik = experiments.intraday_key(found, items.get(info.id))
        if ik and (ik["id"] not in found or found[ik["id"]].kind != "intraday"):
            errors[info.id] = f"장중 조건 '{ik['id']}'를 찾지 못했습니다"
            continue
        setup = experiments.setup_hash(info, cond.params, rules, ik)
        if need_bt and not experiments.has_run(cfg, info.id, setup):
            skipped[info.id] = f"이 설정(코드·파라미터·청산)으로 백테스트한 기록이 없어 건너뜀 — ktrade test {info.id}"
            log.warning(f"[{info.id}] {skipped[info.id]}")
            continue
        try:
            picks = cond.select(Context(asof=asof, data=view, params=cond.params, strategy_id=info.id,
                                        log=logging.getLogger(f"ktrade.condition.{info.id}"))) or []
            plans = compose(list(picks), rules, view)
            for p in plans:                     # 장중 조건: 매수 시점은 장중 조건이 정한다 (감시 목록)
                p.intraday = {"id": ik["id"], "params": ik["params"]} if ik else {}
            produced.append((info.id, info.version, plans, setup))
        except Exception:  # 조건 하나가 죽어도 다른 조건 계획은 만든다
            errors[info.id] = traceback.format_exc(limit=5)
            log.error(f"[{info.id}] select() 오류\n{errors[info.id]}")
    ext_rules = exits_mod.resolve(cfg.get("exits"))
    for item in cfg.get("external_plans", []):
        if only and item.get("id") not in only:
            continue
        try:
            got = _external(item, asof, ext_rules, view)
            if got:
                produced.append((*got, "external"))
        except Exception as exc:
            errors[item.get("id", item["dir"])] = str(exc)

    symbols = sorted({p.symbol for _, _, ps, _ in produced for p in ps})
    closes = store.market.closes(asof, symbols) if symbols else {}
    ok, rejected = guard.screen([(sid, ps) for sid, _, ps, _ in produced], closes, int(cfg["risk"]["max_plans_per_strategy"]))

    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    summary = {"asof": asof, "dir": str(out), "strategies": {}, "rejected": len(rejected), "skipped": skipped, "errors": errors}
    for sid, version, _, setup in produced:
        doc = plan_file(sid, version, asof, ok.get(sid, []), generated_at=now, code_hash=setup)
        (out / f"{sid}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        summary["strategies"][sid] = len(doc["plans"])
    (out / "_rejected.json").write_text(json.dumps({"asof": asof, "rejected": [
        {"strategy": s, "symbol": sym, "reason": why} for s, sym, why in rejected], "skipped": skipped, "errors": errors},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def load_for_today(cfg: dict, store: Store, today: str) -> tuple[list[tuple[str, str, dict]], str]:
    """오늘 실행할 계획 [(조건, 버전, 계획 dict)] — 기준일이 'DB의 오늘 이전 마지막 거래일'인 폴더만(묵은 계획 방지)."""
    asof = store.last_index_date_before(today)
    if not asof:
        return [], "DB에 지수 일봉이 없음"
    d = paths(cfg)["plans"] / asof
    if not d.is_dir():
        return [], f"{asof} 계획 폴더 없음 (장 마감 후 ktrade daily가 안 돌았나?)"
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        out += [(doc["strategy"], doc["version"], p) for p in doc["plans"]]
    return out, f"{asof} 계획 {len(out)}건"
