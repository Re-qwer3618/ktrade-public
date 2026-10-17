"""ktrade 명령 — 매매조건 실험 엔진.

  매매조건 만들기·실험 (조건을 바꾸거나 더할 때마다)
    ktrade new <id>                  내 매매조건 뼈대 (~/.ktrade/strategies/<id>) — 종목 선정 + 매수 타이밍만 작성
    ktrade test <id>                 검사 + 백테스트 + 이전 실험과 비교 (한 번에)
    ktrade validate <id> [--asof]    검사만 (형식·금지 사항·실행 결과·엔진 검사)
    ktrade backtest <id> [--start --end] [--symbols …] [--exits JSON] [--params JSON]   백테스트만
                                     (기간·종목 기본값은 config.yaml backtest, 없으면 DB 전 종목·최근 years년)
    ktrade experiments <id>          이 조건의 실험 기록
    ktrade compare <id> <id> …       여러 조건의 최근 실험 나란히
    ktrade strategies                조건 목록 (켬 여부, 청산 설정, 지금 설정의 백테스트 여부)
    ktrade docs                      작성 가이드·AI용 규격(LLM_CONTEXT.md)을 ~/.ktrade/docs 에 복사

  데이터 (키움에서 받을 범위는 config.yaml collect, 옵션은 그 실행에서만 덮어씀)
    ktrade collect [--symbols 005930,000660 | --symbols-file 파일] [--markets 거래소,코스닥] [--start --end | --years]
                   [--minute-symbols …] [--limit N] [--dry-run]
    ktrade data [--symbols … | --all]   DB에 받아 둔 범위 (종목 수·기간·분봉)
    ktrade import-db <경로>             다른 SQLite DB에서 가져오기

  운영
    ktrade init | daily | plan [--asof] | run [--once] [--live-confirm]
    ktrade status | report | doctor [--offline] | schedule install|remove|show [--dry-run] | dashboard
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from ktrade import __version__
from ktrade import config as config_mod
from ktrade.config import paths

_TEMPLATE = '''"""{id} — 내 매매조건.

여기서 정하는 것은 두 가지뿐:
  1) 종목 선정: 기준일(ctx.asof) 장 마감 기준으로 어떤 종목을 고를지
  2) 매수 타이밍: 다음 거래일에 어떤 가격 조건에서 살지 (Entry.confirm / Entry.at_open / Entry.pullback)
손절·이익실현·보유기간은 엔진이 정한다 (config.yaml 또는 strategy.yaml의 exits).
바꿀 때마다: ktrade test {id}
"""
from ktrade.sdk import Condition, Context, Entry, Pick


class MyCondition(Condition):
    default_params = {{"lookback": 20, "min_price": 2000, "min_avg_value": 5e8, "max_plans": 5}}

    def select(self, ctx: Context) -> list[Pick]:
        p = self.params
        bars = ctx.data.daily_all(bars=p["lookback"] + 2)     # 전 종목 최근 일봉 (symbol, date, open, high, low, close, volume)
        uni = ctx.data.universe()                             # code, name, market, market_name, state
        uni = uni[uni["market_name"].isin(["거래소", "코스닥"]) & ~uni["state"].fillna("").str.contains("관리종목|거래정지")]
        info = uni.set_index("code")
        picks = []
        for symbol, g in bars.groupby("symbol"):
            if symbol not in info.index or len(g) < p["lookback"] + 1 or g["date"].iloc[-1] != ctx.asof:
                continue
            close = float(g["close"].iloc[-1])
            if close < p["min_price"] or (g["close"] * g["volume"]).tail(20).mean() < p["min_avg_value"]:
                continue

            # ── 1) 종목 선정: 여기에 내 조건을 쓴다 (예: 종가가 lookback일 최고 종가를 넘음) ──
            if close <= g["close"].iloc[-(p["lookback"] + 1):-1].max():
                continue

            picks.append(Pick(
                symbol=symbol, name=str(info.at[symbol, "name"]), market=str(info.at[symbol, "market"]),
                # ── 2) 매수 타이밍: 다음 날 시가가 오늘 종가 이상 + 15:10 이후에도 그 위면, +5%까지만 ──
                entry=Entry.confirm(close, chase_pct=5, after="15:10"),
                reason=f"{{p['lookback']}}일 신고가 종가 {{close:,.0f}}원",
            ))
        return sorted(picks, key=lambda x: x.symbol)[: p["max_plans"]]
'''

_MANIFEST = """id: {id}
version: v1
kind: condition
entry: strategy.py:MyCondition
description: 내 매매조건 설명
author: ""
params: {{}}          # 기본값은 strategy.py의 default_params. 여기나 config.yaml에서 덮어쓴다
# 손절·익절을 이 조건만 다르게 하려면 (없으면 config.yaml exits):
# exits:
#   stop: {{type: pct, value: 5}}
#   take_profit: {{type: r, value: 2}}
#   max_hold_days: 10
"""

_STRATEGIES_README = """# 내 매매조건 폴더

매매조건 하나 = 이 아래 폴더 하나 (폴더 이름 = 조건 id). 안에 strategy.yaml + strategy.py.
작성하는 것: 종목 선정 + 매수 타이밍. 손절·이익실현·보유기간·실행·백테스트는 엔진이 한다.

    ktrade new my_condition         # 뼈대 만들기
    ktrade test my_condition        # 검사 + 백테스트 + 이전 실험과 비교 (조건을 바꿀 때마다)
    → 결과가 마음에 들면 config.yaml 의 strategies.enabled 에 '- id: my_condition' 추가

직접 쓰거나, ../docs/LLM_CONTEXT.md 를 Claude·Gemini·ChatGPT에 붙여 넣고 요청 양식을 채워 AI에게 맡기세요.
자세한 건 ../docs/CONDITION_GUIDE.md. 이 엔진은 매매기법을 실험하는 도구이며 수익을 보장하지 않습니다.
"""


def _cfg() -> dict:
    return config_mod.load()


def _docs_src() -> Path | None:
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "docs", here.parent / "_docs"):  # 소스 체크아웃 / 설치본
        if cand.is_dir() and (cand / "LLM_CONTEXT.md").exists():
            return cand
    return None


def _copy_docs(dest: Path) -> list[Path]:
    import shutil
    src = _docs_src()
    if src is None:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(src.glob("*.md")):
        shutil.copy2(f, dest / f.name)
        out.append(dest / f.name)
    return out


def _json(s: str | None) -> dict | None:
    return json.loads(s) if s else None


# ---------------- 매매조건 ----------------
def cmd_init(a) -> None:
    h = config_mod.home()
    for sub in ("data", "plans", "strategies", "logs", "experiments"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    cfg_path, env_path = h / "config.yaml", h / ".env"
    if not cfg_path.exists():
        cfg_path.write_text(config_mod.TEMPLATE, encoding="utf-8")
    if not env_path.exists():
        env_path.write_text("# 키움 REST API 앱키 (키움 OpenAPI 사이트에서 본인 계좌로 발급)\n"
                            "KIWOOM_APP_KEY=\nKIWOOM_APP_SECRET=\n", encoding="utf-8")
    readme = h / "strategies" / "README.md"
    if not readme.exists():
        readme.write_text(_STRATEGIES_README, encoding="utf-8")
    docs = _copy_docs(h / "docs")
    print(f"사용자 폴더: {h}\n  설정 {cfg_path}\n  키   {env_path}  ← 본인 앱키를 적으세요 (공유·커밋 금지)")
    if docs:
        print(f"  문서 {h / 'docs'}  ← 매매조건 작성 가이드(CONDITION_GUIDE.md), AI에게 붙여 넣는 규격(LLM_CONTEXT.md)")
    print("다음: ktrade collect --limit 100   (또는 ktrade import-db <기존 DB 경로>)")
    print("매매조건(종목 선정·매수 타이밍)은 직접 만듭니다: ktrade new <이름> → ktrade test <이름>")


def cmd_strategies(a) -> None:
    from ktrade.engine import experiments, loader
    cfg = _cfg()
    on = {(e["id"] if isinstance(e, dict) else e): (e if isinstance(e, dict) else {}) for e in cfg["strategies"]["enabled"]}
    for sid, info in loader.discover(cfg).items():
        item = on.get(sid, {})
        try:
            from ktrade.engine import exits as X
            rules = loader.exits_for(cfg, info, item.get("exits"))
            params = {**info.params, **(item.get("params") or {})}
            cls = loader._load_class(info)
            setup = experiments.setup_hash(info, {**cls.default_params, **params}, rules)
            bt = "백테스트됨" if experiments.has_run(cfg, sid, setup) else "백테스트 필요"
            ex = X.describe(rules)
        except Exception as exc:
            bt, ex = "오류", str(exc)[:60]
        mark = "켬" if sid in on else " -"
        print(f"{mark:2} {sid:22} {info.version:4} [{bt}] {ex}\n     {info.description[:70]}  ({info.source})")
    for folder, why in loader.INVALID.items():
        print(f"!! 무시한 폴더 {folder}\n     {why}")


def cmd_new(a) -> None:
    d = paths(_cfg())["strategies"] / a.id
    if d.exists():
        sys.exit(f"이미 있습니다: {d}")
    d.mkdir(parents=True)
    (d / "strategy.yaml").write_text(_MANIFEST.format(id=a.id), encoding="utf-8")
    (d / "strategy.py").write_text(_TEMPLATE.format(id=a.id), encoding="utf-8")
    print(f"만들었습니다: {d}\n  1) strategy.py 에서 '종목 선정'과 '매수 타이밍' 두 곳을 고치고\n"
          f"  2) ktrade test {a.id}\n  3) config.yaml strategies.enabled 에 '- id: {a.id}' 추가")


def cmd_validate(a) -> None:
    from ktrade.engine.validate import validate
    rep = validate(_cfg(), a.id, a.asof, _json(a.params))
    print(rep.render())
    if not rep.ok:
        sys.exit(1)


def _period(cfg, store, start, end) -> tuple[str, str]:
    from ktrade.engine.planner import last_trading_day
    bt = cfg["backtest"]
    last = last_trading_day(store)
    if not last:
        sys.exit("DB에 지수 일봉이 없습니다 — 먼저 데이터를 받으세요 (ktrade collect 또는 import-db, 범위 확인은 ktrade data)")
    end = min(str(end or bt.get("end") or last), last)
    start = str(start or bt.get("start") or
                (date.fromisoformat(end) - timedelta(days=int(365 * float(bt["years"])))).isoformat())
    first = store.index_range("001")[0]
    if first and start < first:
        print(f"! DB 지수 일봉이 {first}부터라 시작일을 {start} → {first}로 맞춥니다 (더 앞을 보려면 collect.start를 앞당겨 수집)",
              file=sys.stderr)
        start = first
    if start > end:
        sys.exit(f"백테스트 기간이 비었습니다: {start} ~ {end} (DB 범위는 ktrade data)")
    return start, end


def _bt_symbols(cfg, store, symbols=None, symbols_file=None) -> list[str] | None:
    """백테스트에서 매매조건에게 보일 종목. None = DB 전 종목."""
    from ktrade.data.collect import _file_codes, parse_codes
    bt = cfg["backtest"]
    home = paths(cfg)["home"]
    try:
        if symbols or symbols_file:
            syms = parse_codes((symbols or "").split(",")) + (_file_codes(symbols_file, home) if symbols_file else [])
        else:
            syms = parse_codes(bt.get("symbols")) + (_file_codes(bt["symbols_file"], home) if bt.get("symbols_file") else [])
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(str(exc))
    syms = list(dict.fromkeys(syms))
    if not syms:
        return None
    have = set(store.symbol_ranges(syms)["symbol"])
    missing = [x for x in syms if x not in have]
    if missing:
        print(f"! DB에 일봉이 없는 종목 {len(missing)}개는 빠집니다: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}"
              f"  (받으려면 ktrade collect --symbols {','.join(missing[:5])})", file=sys.stderr)
    syms = [x for x in syms if x in have]
    if not syms:
        sys.exit("지정한 종목이 DB에 하나도 없습니다 — 먼저 ktrade collect --symbols … 로 받으세요")
    return syms


def _run_backtest(cfg, sid, start=None, end=None, params=None, exits_override=None, note="",
                  symbols=None, symbols_file=None) -> tuple[dict, dict | None, Path]:
    from ktrade.data.store import Store
    from ktrade.engine import backtest, experiments, loader
    found = loader.discover(cfg)
    if sid not in found:
        sys.exit(f"매매조건 '{sid}'가 없습니다 (ktrade strategies)")
    info = found[sid]
    item = next((e for e in cfg["strategies"]["enabled"] if isinstance(e, dict) and e.get("id") == sid), {})
    cond = loader.load(info, {**(item.get("params") or {}), **(params or {})})
    rules = loader.exits_for(cfg, info, {**(item.get("exits") or {}), **(exits_override or {})})
    store = Store(paths(cfg)["db"], readonly=True)
    start, end = _period(cfg, store, start, end)
    syms = _bt_symbols(cfg, store, symbols, symbols_file)
    print(f"백테스트 {sid} {info.version} {start} ~ {end} · 대상 {f'{len(syms)}종목' if syms else 'DB 전 종목'}"
          f" · 청산: {backtest.X.describe(rules)}", file=sys.stderr)
    res = backtest.run(store, info, cond, rules, start, end, float(cfg["backtest"]["cost_pct"]),
                       int(cfg["risk"]["max_plans_per_strategy"]), float(cfg["backtest"]["oos_ratio"]),
                       progress=lambda i, n: print(f"  {i}/{n}일", file=sys.stderr), symbols=syms)
    prev = (experiments.history(cfg, sid) or [None])[-1]
    path = experiments.save(cfg, info, cond.params, rules, res, note)
    return res, prev, path


def _print_result(res: dict) -> None:
    def f(v, nd=2):
        return "-" if v is None else f"{v:.{nd}f}"
    print(f"\n[{res['strategy']}] {res['start']} ~ {res['end']} · {res.get('universe', '')} · 청산 {res['exits']}"
          f" · 비용 왕복 {res['cost_pct']}%")
    print(f"  후보 {res['picks']} → 매수 {res['triggered']} (발동률 {f(res.get('trigger_rate'), 0)}%) → 청산 {res['trades']}건"
          + (f", 보유 중 {res['still_open']}" if res.get("still_open") else ""))
    if not res["trades"]:
        print("  청산된 거래가 없습니다 — 기간을 늘리거나 조건을 완화해 보세요.")
        return
    lo, hi = res["win_rate_ci95"]
    print(f"  승률 {res['win_rate']:.1f}% (95% 구간 {lo:.0f}~{hi:.0f}%) · 평균 {res['avg_ret']:+.2f}% · 중앙값 {res['median_ret']:+.2f}%"
          f" · 이익 평균 {res['avg_win']:+.2f}% / 손실 평균 {res['avg_loss']:+.2f}% · PF {f(res.get('profit_factor'))}")
    print(f"  시장(지수) 대비 평균 {res['avg_excess']:+.2f}%p · 평균 보유 {res['avg_days']:.1f}일 · 최대 연속 손실 {res['max_consec_losses']}회")
    if "in_sample" in res:
        a, b = res["in_sample"], res["out_of_sample"]
        print(f"  앞 기간(~{a['until']}) {a['trades']}건 승률 {f(a.get('win_rate'), 1)}% 평균 {f(a.get('avg_ret'))}%  |  "
              f"뒤 기간({b['from']}~) {b['trades']}건 승률 {f(b.get('win_rate'), 1)}% 평균 {f(b.get('avg_ret'))}%  → "
              + ("같은 방향" if res["consistent"] else "방향이 다름 (한 기간에만 맞는 조건일 수 있음)"))
    print("  청산 사유별: " + ", ".join(f"{k} {v['count']}건 {v['mean']:+.2f}%" for k, v in res["by_reason"].items()))
    print("  연도별: " + ", ".join(f"{k} {v['count']}건 {v['mean']:+.2f}%" for k, v in res["by_year"].items()))
    if res["trades"] < 30:
        print("  ! 거래가 30건 미만이라 승률·평균의 오차가 큽니다 (95% 구간 참고).")


def cmd_backtest(a) -> None:
    cfg = _cfg()
    res, prev, path = _run_backtest(cfg, a.id, a.start, a.end, _json(a.params), _json(a.exits), a.note or "",
                                    a.symbols, a.symbols_file)
    _print_result(res)
    from ktrade.engine import experiments
    print("\n" + experiments.diff(prev, json.loads(path.read_text(encoding="utf-8"))))
    print(f"실험 기록: {path}\n주의: 일봉 근사·생존편향(상장폐지 종목 없음). 과거 확률이며 미래 수익을 보장하지 않습니다.")


def cmd_test(a) -> None:
    from ktrade.engine import experiments
    from ktrade.engine.validate import validate
    cfg = _cfg()
    rep = validate(cfg, a.id, None, _json(a.params))
    print(rep.render().replace("다음: ktrade test <조건>  (검사 + 백테스트 + 이전 실험과 비교)", ""))
    if not rep.ok:
        sys.exit(1)
    res, prev, path = _run_backtest(cfg, a.id, a.start, a.end, _json(a.params), _json(a.exits), a.note or "",
                                    a.symbols, a.symbols_file)
    _print_result(res)
    print("\n" + experiments.diff(prev, json.loads(path.read_text(encoding="utf-8"))))
    print(f"실험 기록: {path}")
    print("이 설정으로 백테스트했으므로 켜 두면(config.yaml strategies.enabled) 다음 장 마감부터 계획을 만듭니다.")


def cmd_experiments(a) -> None:
    import pandas as pd
    from ktrade.engine import experiments
    rows = experiments.history(_cfg(), a.id)
    if not rows:
        sys.exit(f"'{a.id}' 실험 기록이 없습니다 (ktrade test {a.id})")
    with pd.option_context("display.width", 220, "display.max_columns", 20, "display.max_colwidth", 40):
        print(experiments.table(rows).to_string(index=False))


def cmd_compare(a) -> None:
    import pandas as pd
    from ktrade.engine import experiments
    cfg = _cfg()
    rows = []
    for sid in a.ids:
        h = experiments.history(cfg, sid)
        if h:
            rows.append(h[-1])
        else:
            print(f"'{sid}' 실험 기록 없음 — ktrade test {sid}", file=sys.stderr)
    if rows:
        with pd.option_context("display.width", 220, "display.max_columns", 20, "display.max_colwidth", 40):
            print(experiments.table(rows).to_string(index=False))
        print("\n같은 기간·같은 비용으로 비교하려면 각 조건을 같은 --start/--end 로 ktrade test 하세요.")


def cmd_docs(a) -> None:
    dest = paths(_cfg())["home"] / "docs"
    files = _copy_docs(dest)
    if not files:
        sys.exit("문서를 찾지 못했습니다 (설치본에 docs가 없음)")
    print(dest)
    for f in files:
        print(f"  {f.name}")
    print("AI로 매매조건을 쓰려면 LLM_CONTEXT.md 내용을 통째로 AI 채팅에 붙여 넣고 맨 아래 요청 양식을 채우세요.")


# ---------------- 데이터·운영 ----------------
def _split(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v is not None else None


def cmd_collect(a) -> None:
    from ktrade.data.collect import collect, resolve_scope, target_symbols
    from ktrade.data.store import Store
    cfg = _cfg()
    try:
        scope = resolve_scope(cfg, symbols=_split(a.symbols), symbols_file=a.symbols_file, markets=_split(a.markets),
                              start=a.start, end=a.end, years=a.years, limit=a.limit,
                              minute_symbols=_split(a.minute_symbols), minute_start=a.minute_start)
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(str(exc))
    store = Store(paths(cfg)["db"])
    print(f"수집 범위: {scope.describe()}", file=sys.stderr)
    if a.dry_run:
        syms = target_symbols(store, scope)
        if scope.symbols is None and not syms:
            print("  (시장 전체 — 종목 목록은 실제 수집 때 키움에서 받습니다)")
        else:
            print(f"  대상 {len(syms)}종목: {', '.join(syms[:20])}{' …' if len(syms) > 20 else ''}")
        print("  --dry-run: 아무것도 받지 않았습니다.")
        return
    print(json.dumps(collect(store, cfg, scope), ensure_ascii=False))


def cmd_data(a) -> None:
    import pandas as pd
    from ktrade.data.collect import parse_codes
    from ktrade.data.store import Store
    cfg = _cfg()
    p = paths(cfg)["db"]
    if not p.exists():
        sys.exit("아직 DB가 없습니다 — ktrade collect (받을 범위는 config.yaml collect)")
    store = Store(p, readonly=True)
    if a.symbols:
        try:
            t = store.symbol_ranges(parse_codes(_split(a.symbols)))
        except ValueError as exc:
            sys.exit(str(exc))
        print(t.to_string(index=False) if not t.empty else "그 종목들의 일봉이 DB에 없습니다")
        return
    s = store.summary()
    d, m = s["daily"], s["minute"]
    print(f"DB: {p}")
    print(f"일봉: {d['symbols']}종목 · {d['rows']:,}행" + (f" · {d['from']} ~ {d['to']}" if d["rows"] else ""))
    for code, v in s["index"].items():
        print(f"지수 {({'001': '코스피', '101': '코스닥'}).get(code, code)}: {v['rows']}일 · {v['from']} ~ {v['to']}")
    print(f"분봉: {m['symbols']}종목 · {m['rows']:,}행" + (f" · {m['from']} ~ {m['to']}" if m["rows"] else ""))
    if s["universe"]:
        print("종목 목록: " + ", ".join(f"{k or '(빈 값)'} {v}" for k, v in s["universe"].items()))
    if a.all:
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(store.symbol_ranges().to_string(index=False))
    else:
        print("종목별 범위: ktrade data --all  또는  ktrade data --symbols 005930,000660")


def cmd_import_db(a) -> None:
    from ktrade.data.importer import import_db
    from ktrade.data.store import Store
    cfg = _cfg()
    print(json.dumps(import_db(Store(paths(cfg)["db"]), a.path, since=a.since, minute_days=a.minute_days), ensure_ascii=False))


def cmd_plan(a) -> None:
    from ktrade.data.store import Store
    from ktrade.engine.planner import run_plans
    cfg = _cfg()
    print(json.dumps(run_plans(cfg, Store(paths(cfg)["db"]), a.asof, only=a.only), ensure_ascii=False, indent=1))


def cmd_run(a) -> None:
    from ktrade.engine import live
    live.run(_cfg(), live_confirm=a.live_confirm, once=a.once)


def cmd_status(a) -> None:
    from ktrade.data.store import Store
    from ktrade.engine import loader
    cfg = _cfg()
    p = paths(cfg)
    print(f"ktrade {__version__}  home={p['home']}  broker={cfg['broker']}  is_mock={cfg['kiwoom'].get('is_mock', True)}")
    print(f"키: {'있음' if cfg['_secrets']['app_key'] else '없음 (~/.ktrade/.env)'}")
    try:
        print("켠 매매조건:", [f"{i.id} {i.version}" for i, _, _ in loader.enabled(cfg)])
    except Exception as exc:
        print("매매조건 오류:", exc)
    if p["db"].exists():
        s = Store(p["db"], readonly=True)
        r = s.query("SELECT COUNT(DISTINCT symbol) AS n, MAX(date) AS last FROM ohlcv")
        print(f"일봉: {int(r['n'].iloc[0])}종목, 마지막 {r['last'].iloc[0]}")
        t = s.plan_trades(10)
        if not t.empty:
            print(t[["strategy", "symbol", "status", "entry_date", "entry_price", "exit_reason"]].to_string(index=False))
    plans = sorted(p["plans"].glob("*/")) if p["plans"].exists() else []
    if plans:
        print("최근 계획 폴더:", plans[-1])


def cmd_daily(a) -> None:
    from ktrade.engine.daily import run_daily
    res = run_daily(_cfg(), collect=not a.no_collect, minute=not a.no_minute, limit=a.limit)
    print(json.dumps({k: (v if k != "steps" else {s: ("오류" if isinstance(x, dict) and "error" in x else "완료")
                                                    for s, x in v.items()}) for k, v in res.items()}, ensure_ascii=False))
    if any(isinstance(x, dict) and "error" in x for x in res["steps"].values()):
        sys.exit(1)


def cmd_report(a) -> None:
    from ktrade.data.store import Store
    from ktrade.engine import report
    cfg = _cfg()
    print(report.text(Store(paths(cfg)["db"], readonly=True), paths(cfg)["plans"]))


def cmd_doctor(a) -> None:
    from ktrade.engine import doctor
    rows = doctor.checks(_cfg(), online=not a.offline)
    print(doctor.render(rows))
    if any(ok is False for _, ok, _ in rows):
        sys.exit(1)


def cmd_schedule(a) -> None:
    from ktrade.engine import schedule
    print(schedule.install(a.dry_run) if a.action == "install" else schedule.remove(a.dry_run) if a.action == "remove"
          else schedule.status())


def cmd_dashboard(a) -> None:
    import subprocess
    try:
        import streamlit  # noqa: F401
    except ImportError:
        sys.exit('대시보드는 선택 설치입니다: uv tool install "ktrade[dashboard]" (또는 pip install "ktrade[dashboard]")')
    app = Path(__file__).parent / "dashboard" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app), "--server.address", "127.0.0.1",
                    "--server.headless", "true", "--browser.gatherUsageStats", "false"])


def _fix_console_encoding() -> None:
    """한국어 Windows에서 출력을 파일·파이프로 보내면(작업 스케줄러 로그 등) 기본 인코딩이 cp949라 '—' 같은 글자에서
    죽는다(실측). 파일·파이프는 UTF-8로, 콘솔은 그대로 두되 못 쓰는 글자는 바꿔 찍어 절대 죽지 않게 한다."""
    for s in (sys.stdout, sys.stderr):
        try:
            if s.isatty():
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> None:
    _fix_console_encoding()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="ktrade", description="ktrade — 키움 REST API 기반 매매조건 실험 엔진 (수익 보장 없음)",
                                 epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"ktrade {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, aliases=()):
        s = sub.add_parser(name, aliases=list(aliases))
        for spec in args:
            flags, kw = spec
            s.add_argument(*flags, **kw)
        s.set_defaults(fn=fn)
        return s

    bt_args = [(("--start",), {}), (("--end",), {}),
               (("--symbols",), {"help": "이 종목만 대상으로 (쉼표 구분, 예: 005930,000660)"}),
               (("--symbols-file",), {"help": "대상 종목 파일 (한 줄에 하나)"}),
               (("--params",), {"help": 'JSON, 예: \'{"lookback": 30}\''}),
               (("--exits",), {"help": 'JSON, 예: \'{"stop": {"type": "pct", "value": 5}}\''}),
               (("--note",), {"help": "이 실험 메모"})]
    add("init", cmd_init)
    add("strategies", cmd_strategies, aliases=("conditions",))
    add("new", cmd_new, (("id",), {}), aliases=("new-condition", "new-strategy"))
    add("validate", cmd_validate, (("id",), {}), (("--asof",), {}), (("--params",), {}))
    add("test", cmd_test, (("id",), {}), *bt_args)
    add("backtest", cmd_backtest, (("id",), {}), *bt_args)
    add("experiments", cmd_experiments, (("id",), {}))
    add("compare", cmd_compare, (("ids",), {"nargs": "+"}))
    add("docs", cmd_docs)
    add("collect", cmd_collect, (("--symbols",), {"help": "이 종목만 (쉼표 구분)"}), (("--symbols-file",), {}),
        (("--markets",), {"help": "시장 이름 (쉼표 구분, 예: 거래소,코스닥,ETF)"}),
        (("--start",), {}), (("--end",), {}), (("--years",), {"type": float}),
        (("--minute-symbols",), {"help": "1분봉도 받을 종목 (쉼표 구분)"}), (("--minute-start",), {}),
        (("--limit",), {"type": int}), (("--dry-run",), {"action": "store_true", "help": "범위만 보여 주고 받지 않음"}))
    add("data", cmd_data, (("--symbols",), {}), (("--all",), {"action": "store_true"}))
    add("import-db", cmd_import_db, (("path",), {}), (("--since",), {}), (("--minute-days",), {"type": int, "default": 0}))
    add("plan", cmd_plan, (("--asof",), {}), (("--only",), {"nargs": "*"}))
    add("daily", cmd_daily, (("--no-collect",), {"action": "store_true"}), (("--no-minute",), {"action": "store_true"}),
        (("--limit",), {"type": int}))
    add("run", cmd_run, (("--once",), {"action": "store_true"}), (("--live-confirm",), {"action": "store_true"}))
    add("status", cmd_status)
    add("report", cmd_report)
    add("doctor", cmd_doctor, (("--offline",), {"action": "store_true"}))
    add("schedule", cmd_schedule, (("action",), {"choices": ["install", "remove", "show"]}), (("--dry-run",), {"action": "store_true"}))
    add("dashboard", cmd_dashboard)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
