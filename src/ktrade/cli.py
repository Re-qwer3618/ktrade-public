"""ktrade 명령 — 매매조건 실험 엔진.

  매매조건 만들기·실험 (조건을 바꾸거나 더할 때마다) — test·backtest·experiments·compare는 이용 코드 로그인 필요(ktrade access login)
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
                   [--exclude-kinds ETF,우선주 | --keep-all-kinds] [--price-min --price-max] [--cap-min --cap-max(억)]
                   [--no-daily] [--minute-symbols …|all --minute-intervals 1,5] [--tick-symbols …|all --tick-units 1,10]
                   [--limit N] [--dry-run]
    ktrade data [--symbols … | --all]   받아 둔 범위 (종목 수·기간·분봉·틱) + 종류별 용량
    ktrade data-dir [새 폴더] [--no-move]   데이터 폴더 보기·바꾸기 (기존 데이터도 옮김)
    ktrade import-db <경로>             다른 SQLite DB에서 가져오기

  영웅문 조건검색식 (영웅문4 '조건검색 0150'에서 만든 조건식 — 키움 서버가 계산, 주문 없음)
    ktrade hts list                    조건식 목록 (번호·이름)
    ktrade hts scan "<이름|번호>" [--csv 파일] [--limit N]   지금 그 조건식에 걸린 종목
    ktrade hts watch ["<이름>" …] [--until HH:MM]   장중 실시간 편입·이탈 → 신호 일지. config에서 mode: trade인 조건식은 매매
                                       (실전은 kiwoom.real_orders: true 동의 뒤), 손절·익절·당일 청산은 엔진 규칙
    ktrade hts signals [--date YYYY-MM-DD] [--cond 이름]   신호 일지 요약
    ktrade hts report [--date | --days N] [--cond 이름] [--collect]   성적표: 신호마다 '그때 샀다면'(1분봉, 엔진 청산) + 실제 매매

  운영
    ktrade login [--real] | logout     키움 앱키 입력·삭제 (대시보드 로그인 화면과 같음, OS 자격 증명 저장소에 보관)
    ktrade real-orders on|off|show     실전 계좌 주문 동의 (조건식 매매가 실전 키로 주문하려면 on)
    ktrade access login|logout|status  백테스트 이용 코드 로그인 (관리자에게 받은 아이디·코드)
    ktrade admin init|issue <아이디> [--role backtest|admin] [--days N]|list   관리자: 이용 코드 발급
    ktrade init | daily | plan [--asof] | run [--once] [--live-confirm]
    ktrade status | report | doctor [--offline] | schedule install|remove|show [--dry-run]
    ktrade dashboard [--open]        보기 전용 대시보드 (이 PC에서만)
    ktrade shortcut install|remove|show   바탕화면·시작 메뉴 바로가기 (대시보드·명령창·폴더)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
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


def _require_backtest() -> None:
    from ktrade import access
    if not access.can_backtest(access.current()):
        sys.exit("백테스트는 이용 코드 로그인이 필요합니다 — ktrade access login (코드는 관리자에게 받습니다)\n"
                 "코드 없이 예제 매매조건을 보려면: ktrade dashboard --open → 🧪 백테스트 체험판")


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
    cfg_path = h / "config.yaml"
    if not cfg_path.exists():
        cfg_path.write_text(config_mod.TEMPLATE, encoding="utf-8")
    readme = h / "strategies" / "README.md"
    if not readme.exists():
        readme.write_text(_STRATEGIES_README, encoding="utf-8")
    docs = _copy_docs(h / "docs")
    has_key = bool(config_mod.load()["_secrets"]["app_key"])
    print(f"사용자 폴더: {h}\n  설정 {cfg_path}\n  키   " + ("저장됨" if has_key else
          "아직 없음 ← 대시보드(ktrade dashboard --open)의 로그인 화면 또는 ktrade login 으로 입력"))
    if docs:
        print(f"  문서 {h / 'docs'}  ← 매매조건 작성 가이드(CONDITION_GUIDE.md), AI에게 붙여 넣는 규격(LLM_CONTEXT.md)")
    print("대시보드: ktrade dashboard --open → 키움 로그인 (차트 데이터 없이도 운영 탭을 씀)")
    print("다음: ktrade collect --limit 100   (또는 ktrade import-db <기존 DB 경로>)")
    print("매매조건(종목 선정·매수 타이밍)은 직접 만듭니다: ktrade new <이름> → ktrade test <이름>")


def cmd_login(a) -> None:
    import getpass

    from ktrade import credentials
    print("키움 REST API 앱키를 입력합니다 (화면에 보이지 않음). 저장 위치: " + credentials.where())
    key = (a.app_key or getpass.getpass("앱키 (App Key): ")).strip()
    secret = getpass.getpass("시크릿키 (Secret Key): ").strip()
    if not key or not secret:
        sys.exit("앱키와 시크릿키를 모두 넣어 주세요.")
    mock = not a.real
    if not a.no_check:
        from ktrade.data.kiwoom import KiwoomData
        cfg = _cfg()
        cfg["_secrets"] = {"app_key": key, "app_secret": secret}
        cfg["kiwoom"] = {**cfg["kiwoom"], "is_mock": mock}
        try:
            price = KiwoomData(cfg).quote("005930").get("price")
        except Exception as exc:
            sys.exit(f"연결 실패: {str(exc)[:300]}\n키 종류(모의는 기본, 실전은 --real)와 키를 확인하세요. 확인 없이 저장: --no-check")
        print(f"연결 성공 — 삼성전자 현재가 {price:,.0f}원 (조회만)" if price else "연결 성공")
    where = credentials.save({"KIWOOM_APP_KEY": key, "KIWOOM_APP_SECRET": secret})
    config_mod.set_kiwoom(is_mock=mock, real_orders=False)
    print(f"저장했습니다 ({where}) · kiwoom.is_mock: {str(mock).lower()}"
          + ("\n실전 키입니다. 실전 주문은 동의 전까지 나가지 않습니다 (ktrade real-orders on)" if not mock else ""))


def cmd_real_orders(a) -> None:
    cfg = _cfg()
    if a.action != "show":
        on = a.action == "on"
        if on and not a.yes:
            print("실전 계좌 주문에 동의합니다: 조건식 매매(mode: trade)가 실전 계좌로 실제 돈을 주문합니다.\n"
                  "손실은 본인 책임이며 ktrade는 수익을 보장하지 않습니다.")
            if input("동의하면 '동의'를 입력: ").strip() != "동의":
                sys.exit("취소했습니다.")
        config_mod.set_kiwoom(real_orders=on)
        cfg = _cfg()
    allow, how = _hts_trade_allowed(cfg)
    print(f"kiwoom.real_orders: {str(bool(cfg['kiwoom'].get('real_orders'))).lower()} · 조건식 매매 주문: "
          + (how if allow else f"안 함 — {how}"))


def cmd_access(a) -> None:
    import getpass

    from ktrade import access
    if a.action == "login":
        user_id = (a.id or input("아이디: ")).strip()
        code = (a.code or getpass.getpass("이용 코드 (화면에 보이지 않음): ")).strip()
        try:
            info = access.login(user_id, code)
        except ValueError as exc:
            sys.exit(f"로그인 실패: {exc}")
        print(f"로그인했습니다 — {access.describe(info)}")
    elif a.action == "logout":
        access.logout()
        print("이용 코드 로그인을 지웠습니다.")
    else:
        print(f"백테스트 이용 권한: {access.describe(access.current())}")


def cmd_admin(a) -> None:
    from ktrade import access
    if a.action == "init":
        pub, new = access.admin_init()
        print(("관리자 서명 키를 만들었습니다" if new else "관리자 서명 키가 이미 있습니다")
              + f": {access._admin_dir() / 'signing_key'}  (백업해 두고 git·배포에 넣지 마세요)")
        print(f"공개 키: {pub}")
        if not access.key_registered(access.signing_seed()):
            print("! 이 공개 키가 ktrade/access.py PUBLIC_KEYS에 없습니다 — 넣고 새 버전을 배포해야 이 키로 발급한 코드가 통합니다.")
    elif a.action == "issue":
        if not a.id:
            sys.exit("아이디를 주세요 — ktrade admin issue <아이디> [--role backtest|admin] [--days N]")
        try:
            code = access.admin_issue(a.id, a.role, a.days or None, a.note or "")
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"아이디: {a.id}\n역할: {a.role}\n이용 코드: {code}\n받는 사람: ktrade access login (또는 대시보드 → 백테스트 로그인)")
    else:
        rows = access.issued()
        if not rows:
            print("발급 기록이 없습니다.")
        for r in rows:
            print(f"{r['at']}  {r['id']:<20} {r['role']:<9} 만료 {r.get('exp') or '없음'}  {r.get('note') or ''}")


def cmd_logout(a) -> None:
    from ktrade import credentials
    credentials.clear()
    print("저장된 키움 키를 지웠습니다." + ("  (~/.ktrade/.env 나 환경변수에 적은 키는 직접 지우세요)"
                                       if (config_mod.home() / ".env").exists() else ""))


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
    _require_backtest()
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
    _require_backtest()
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
    _require_backtest()
    rows = experiments.history(_cfg(), a.id)
    if not rows:
        sys.exit(f"'{a.id}' 실험 기록이 없습니다 (ktrade test {a.id})")
    with pd.option_context("display.width", 220, "display.max_columns", 20, "display.max_colwidth", 40):
        print(experiments.table(rows).to_string(index=False))


def cmd_compare(a) -> None:
    import pandas as pd
    from ktrade.engine import experiments
    _require_backtest()
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
    def rng(lo, hi):
        return None if lo is None and hi is None else {"min": lo, "max": hi}
    kinds = [] if a.keep_all_kinds else _split(a.exclude_kinds)
    try:
        scope = resolve_scope(cfg, symbols=_split(a.symbols), symbols_file=a.symbols_file, markets=_split(a.markets),
                              start=a.start, end=a.end, years=a.years, limit=a.limit,
                              minute_symbols=a.minute_symbols, minute_start=a.minute_start,
                              exclude_kinds=kinds, price=rng(a.price_min, a.price_max), market_cap=rng(a.cap_min, a.cap_max),
                              daily=False if a.no_daily else None, minute_intervals=a.minute_intervals,
                              tick_symbols=a.tick_symbols, tick_units=a.tick_units, tick_start=a.tick_start)
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


def cmd_data_dir(a) -> None:
    import shutil
    cfg = _cfg()
    cur = paths(cfg)["data"]
    if not a.path:
        free = shutil.disk_usage(cur if cur.exists() else cur.anchor).free / 2**30
        print(f"데이터 폴더: {cur}" + ("" if cfg.get("data_dir") else "  (기본값 — 사용자 폴더 안)"))
        print(f"  남은 공간 {free:,.1f} GB · 바꾸려면: ktrade data-dir <새 폴더>  (기존 데이터도 옮김, --no-move면 옮기지 않음)")
        return
    try:
        dst, moved = config_mod.set_data_dir(cfg, a.path, move=not a.no_move)
    except (ValueError, RuntimeError, OSError) as exc:
        sys.exit(f"바꾸지 못했습니다: {exc}")
    print(f"데이터 폴더: {cur} → {dst}")
    print(f"  옮긴 것: {', '.join(moved)}" if moved else "  옮긴 것 없음")
    print("  ktrade run·hts watch·대시보드를 켜 둔 상태였다면 다시 시작하세요.")


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
    d = s["daily"]
    print(f"시세: {store.market.root}  (종목 목록·매매 기록: {p})")
    print(f"일봉: {d['symbols']}종목 · {d['rows']:,}행" + (f" · {d['from']} ~ {d['to']}" if d["rows"] else ""))
    for code, v in s["index"].items():
        print(f"지수 {({'001': '코스피', '101': '코스닥'}).get(code, code)}: {v['rows']}일 · {v['from']} ~ {v['to']}")
    for iv, m in s["minute"].items():
        print(f"{iv}분봉: {m['symbols']}종목 · {m['rows']:,}행" + (f" · {m['from']} ~ {m['to']}" if m["rows"] else ""))
    for t, n in s["tick"].items():
        print(f"{t}틱: {n}일치 파일")
    if s["disk"]:
        print("용량: " + ", ".join(f"{k} {v['mb']:,} MB ({v['files']}개 파일)" for k, v in s["disk"].items()))
    if s["universe"]:
        print("종목 목록: " + ", ".join(f"{k or '(빈 값)'} {v}" for k, v in s["universe"].items()))
    if a.all:
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(store.symbol_ranges().to_string(index=False))
    else:
        print("종목별 범위: ktrade data --all  또는  ktrade data --symbols 005930,000660")


def cmd_hts(a) -> None:
    import pandas as pd
    from ktrade.data.hts import HtsClient, HtsError
    cfg = _cfg()
    server = "모의투자" if cfg["kiwoom"].get("is_mock", True) else "실전"
    if a.action == "watch":
        return _hts_watch(a, cfg, server)
    if a.action == "signals":
        from ktrade.data.store import Store
        return _hts_summary(Store(paths(cfg)["db"]), a.date or date.today().isoformat(), a.cond, rows=a.limit)
    if a.action == "report":
        return _hts_report(a, cfg)
    client = HtsClient(cfg)
    try:
        if a.action == "list":
            conds = client.list()
            if not conds:
                print(f"조건식이 없습니다 ({server} 서버) — 영웅문4 '조건검색(0150)'에서 만들고 저장하세요.")
                return
            print(f"영웅문 조건식 {len(conds)}개 ({server} 서버)")
            for c in conds:
                print(f"  {c.seq:>3}  {c.name}")
            print('\n지금 걸린 종목 보기: ktrade hts scan "<이름 또는 번호>"')
            return
        if not a.names:
            sys.exit('조건식 이름이나 번호를 주세요 — 예: ktrade hts scan "01_오전스켈핑"  (목록: ktrade hts list)')
        cond, df = client.scan(a.names[0])
    except HtsError as exc:
        sys.exit(str(exc))
    print(f"[{cond.seq}] {cond.name} — {len(df)}종목 ({server} 서버, {datetime.now():%Y-%m-%d %H:%M} 기준, 주문 없음)")
    if df.empty:
        return
    if a.csv:
        df.to_csv(a.csv, index=False, encoding="utf-8-sig")
        print(f"CSV 저장: {a.csv}")
    show = df.head(a.limit).copy()
    show["rate"] = show["rate"].map(lambda v: "" if pd.isna(v) else f"{v:+.2f}%")
    for c in ("price", "volume"):
        show[c] = show[c].map(lambda v: "" if pd.isna(v) else f"{v:,.0f}")
    show = show[["code", "name", "price", "rate", "volume", "kind"]].rename(
        columns={"code": "종목", "name": "이름", "price": "현재가", "rate": "등락률", "volume": "거래량", "kind": "구분"})
    with pd.option_context("display.max_rows", None, "display.width", 160, "display.unicode.east_asian_width", True):
        print(show.to_string(index=False))
    if len(df) > a.limit:
        print(f"… 외 {len(df) - a.limit}종목 (--limit 또는 --csv)")


_ACTION_KO = {"INIT": "등록 시 편입", "WAIT": "확인 대기", "WOULD_BUY": "매수 신호", "SKIP": "건너뜀", "CONFIRM_FAIL": "확인 탈락",
              "REMOVE": "이탈", "WOULD_SELL": "이탈 매도 신호", "BUY": "매수", "SELL": "이탈 매도"}


def _hts_trade_allowed(cfg: dict) -> tuple[bool, str]:
    """조건식 매매를 실제로 주문해도 되는지 — paper(가상 체결)·키움 모의투자는 바로, 실전은 사용자가 실전 주문에 동의했을 때만
    (kiwoom.real_orders: true — 대시보드 운영 탭 5단계 또는 ktrade real-orders on. 키를 새로 저장하면 동의는 풀린다)."""
    if cfg.get("broker", "paper") == "paper":
        return True, "paper(가상 체결)"
    if cfg["kiwoom"].get("is_mock", True):
        return True, "키움 모의투자"
    if cfg["kiwoom"].get("real_orders"):
        return True, "키움 실전 (실전 주문 동의함)"
    return False, "실전 계좌는 실전 주문 동의 전까지 주문하지 않음 (운영 탭 5단계 또는 ktrade real-orders on)"


def _hts_report(a, cfg: dict) -> None:
    import pandas as pd
    from ktrade.data.store import Store
    from ktrade.engine import forward
    store = Store(paths(cfg)["db"])
    days = forward.signal_dates(store, a.date, a.days)
    if not days:
        sys.exit("신호 일지가 없습니다 (장중에 ktrade hts watch)")
    if a.collect:
        n = forward.collect_minutes(store, cfg, days)
        print(f"1분봉 받음: {n}종목·일")
    rep = forward.report(store, cfg, days, a.cond)
    print(forward.render(rep))


def _hts_watch(a, cfg: dict, server: str) -> None:
    """장중 관찰: 조건식 실시간 등록 → 편입·이탈마다 SignalBook 판단 → 신호 일지. 주문 모듈(broker)은 불러오지도 않는다."""
    import time
    from ktrade.data.store import Store
    from ktrade.data.stream import ConditionStream
    from ktrade.engine.executor import halt_state
    from ktrade.engine.live import stop_requested
    from ktrade.engine.signals import SignalBook, load_conditions
    conds = load_conditions(cfg, a.names or None)
    if not conds:
        sys.exit('관찰할 조건식을 주세요 — ktrade hts watch "01_오전스켈핑"  또는 config.yaml hts_conditions (목록: ktrade hts list)')
    if not cfg["_secrets"].get("app_key"):
        sys.exit("키움 앱키가 없습니다 — 대시보드 로그인 화면 또는 ktrade login")
    store = Store(paths(cfg)["db"])
    rest_cache: dict[str, tuple[float, float]] = {}
    rest = None
    trade_conds = [c["name"] for c in conds if c.get("mode") == "trade"]
    allow, how = _hts_trade_allowed(cfg)
    executor = None
    if trade_conds and allow:
        from ktrade.broker import build as build_broker
        from ktrade.engine.executor import Executor
        from ktrade.engine.risk import Risk
        broker = build_broker(cfg, store)
        from ktrade.engine.planner import load_for_today
        plan_cache: dict[str, tuple[set, int]] = {}

        def plan_info(today: str) -> tuple[set, int]:     # 계획 우선 판단용 — 오늘 실행할 장 마감 후 계획
            if today not in plan_cache:
                try:
                    items, _ = load_for_today(cfg, store, today)
                except Exception:
                    items = []
                plan_cache[today] = ({p["symbol"] for _, _, p in items}, len(items))
            return plan_cache[today]

        executor = Executor(store, broker, Risk.from_config(cfg, broker.total_deposit()),
                            lambda s: {"price": price(s), "open": price(s)} if price(s) else {}, role="hts", plan_info=plan_info)

    def price(sym: str) -> float | None:
        nonlocal rest
        p = stream.price(sym)
        if p:
            return p
        hit = rest_cache.get(sym)
        if hit and time.monotonic() - hit[1] < 3:
            return hit[0]
        try:
            if rest is None:
                from ktrade.data.kiwoom import KiwoomData
                rest = KiwoomData(cfg)
            q = rest.quote(sym) or {}
        except Exception:
            return hit[0] if hit else None
        if q.get("price"):
            rest_cache[sym] = (float(q["price"]), time.monotonic())
            return float(q["price"])
        return None

    until = a.until or cfg["live"].get("market_close", "15:30")
    names = [c["name"] for c in conds]
    trading = f"매매: {', '.join(trade_conds)} ({how})" if executor else (
        f"매매 설정 {', '.join(trade_conds)} — 관찰로 동작 ({how})" if trade_conds else "주문 없음")
    print(f"조건식 실시간 ({server} 서버) — {', '.join(names)} · {until}까지 (Ctrl+C로 끝) · {trading}")
    print("  같은 키로 실시간 시세·조건검색을 쓰는 다른 프로그램이 켜져 있으면 서로 끊깁니다(계정당 웹소켓 하나).")
    stream = ConditionStream(cfg, names).start()
    book = SignalBook(store, conds, price, executor=executor, allow_trade=executor is not None, no_trade_reason=how)
    started = datetime.now()
    last_check: dict[str, float] = {}
    last_guard = 0.0

    printed: dict[tuple, float] = {}

    def show(d: dict) -> None:
        # 같은 종목이 몇 초마다 편입·이탈을 되풀이하면 같은 줄이 수천 번 찍힌다 → 같은 (조건식·종목·판단·사유)는 10분에 한 번만
        if d["action"] in ("SKIP", "REMOVE", "WAIT", "CONFIRM_FAIL"):
            key = (d["cond"], d["symbol"], d["action"], (d.get("reason") or "").split(" (")[0])
            if time.monotonic() - printed.get(key, -1e9) < 600:
                return
            printed[key] = time.monotonic()
        pr = f" {d['price']:,.0f}원" if d.get("price") else ""
        print(f"{datetime.now():%H:%M:%S} [{d['cond']}] {d['event']} {d['symbol']}{pr} → {_ACTION_KO.get(d['action'], d['action'])}"
              + (f" — {d['reason']}" if d.get("reason") and d["action"] != "INIT" else ""), flush=True)
    try:
        while datetime.now().strftime("%H:%M") < until:
            now = datetime.now()
            while True:
                try:
                    ev = stream.events.get_nowait()
                except Exception:
                    break
                if ev.kind in ("status", "error", "registered"):
                    tag = {"status": "상태", "error": "오류", "registered": "등록"}[ev.kind]
                    print(f"{now:%H:%M:%S} ({tag}) {('[' + ev.cond + '] ') if ev.cond else ''}{ev.detail}", flush=True)
                    store.log_decision("hts:" + (ev.cond or "-"), "-", "HTS_" + ev.kind.upper(), "NONE", ev.detail,
                                       now.isoformat(timespec="seconds"))
                    continue
                if ev.kind == "insert":
                    stream.watch_price(ev.symbol)
                for d in book.handle(ev, now):
                    if d["action"] != "INIT" or a.verbose:
                        show(d)
            for d in book.tick(now):
                show(d)
            if time.monotonic() - last_guard >= 5:
                last_guard = time.monotonic()
                store.set_setting("hts.heartbeat", {"ts": now.isoformat(timespec="seconds"), "conds": names,
                                                    "registered": list(getattr(stream, "registered", [])), "restarts": getattr(stream, "restarts", 0),
                                                    "trade": executor is not None, "until": until})
                if stop_requested(store, "hts", started):
                    print(f"{now:%H:%M:%S} 화면에서 중지 요청 — 끝냅니다", flush=True)
                    break
                if executor is not None:       # 하루 손익 한도 → 정지(+전량 매도)
                    halted = halt_state(store, now.strftime("%Y-%m-%d"))
                    executor.guard(now)
                    h = halt_state(store, now.strftime("%Y-%m-%d"))
                    if h and not halted:
                        print(f"{now:%H:%M:%S} 계좌 정지 — {h.get('reason')}", flush=True)
            if executor is not None:           # 보유 종목 손절·익절·분할·당일 청산 (종목마다 2초에 한 번)
                for sym in executor.symbols():
                    if time.monotonic() - last_check.get(sym, 0) < 2:
                        continue
                    last_check[sym] = time.monotonic()
                    before = store.active_plan_trade(sym)
                    try:
                        executor.check(sym, now)
                    except Exception as exc:
                        print(f"{now:%H:%M:%S} [{sym}] 실행 오류: {exc}", flush=True)
                        continue
                    if before is not None:
                        _print_fills(store, before, now)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n중지")
    finally:
        stream.stop()
    print(f"\n관찰 {started:%H:%M}~{datetime.now():%H:%M}, 재연결 {stream.restarts}번")
    _hts_summary(store, date.today().isoformat(), None, rows=0)
    if executor is not None:
        held = [s for s in store.active_plan_symbols() if (store.active_plan_trade(s) or {}).get("strategy", "").startswith("hts:")]
        if held:
            print(f"! 아직 보유 중 {len(held)}종목 ({', '.join(held)}) — 다음 ktrade hts watch / ktrade run 때 손절·익절·청산 규칙으로 계속 관리")


def _print_fills(store, before: dict, now) -> None:
    """한 번의 확인 사이에 새로 기록된 매수 차수·매도를 찍는다."""
    from ktrade.engine.executor import book
    t = store.query("SELECT * FROM plan_trades WHERE id = ?", (int(before["id"]),)).iloc[0].to_dict()
    f0, s0, _ = book(before)
    f1, s1, _ = book(t)
    name = str(t["strategy"]).removeprefix("hts:")
    for f in f1[len(f0):]:
        if f["qty"] > 0 and f["step"] > 0:
            print(f"{now:%H:%M:%S} [{name}] {f['step'] + 1}차 분할매수 {t['symbol']} {f['qty']}주 {f['price']:,.0f}원 "
                  f"(평균 {t['entry_price']:,.0f}원)", flush=True)
    for s in s1[len(s0):]:
        if s["qty"] > 0:
            ret = (s["price"] / t["entry_price"] - 1) * 100 if t["entry_price"] else 0
            print(f"{now:%H:%M:%S} [{name}] 매도 {t['symbol']} {s['qty']}주 {s['price']:,.0f}원 ({ret:+.2f}%) — {s['reason']}",
                  flush=True)


def _hts_summary(store, day: str, cond: str | None, rows: int = 30) -> None:
    df = store.signals(day, cond)
    if df.empty:
        print(f"{day} 신호 일지가 비어 있습니다 (장중에 ktrade hts watch)")
        return
    from ktrade.engine.forward import EPISODE_GAP_MIN, episodes
    print(f"[신호 일지 {day}]" + (f" {cond}" if cond else ""))
    for name, g in df.groupby("cond_name", sort=False):
        eps = episodes(g, EPISODE_GAP_MIN)
        if len(eps):
            print(f"  {name}: 편입 {eps['symbol'].nunique()}종목 · 편입 묶음 {len(eps)}번 (원자료 편입 이벤트 {int(eps['n_inserts'].sum())}번)")
        cnt = g["action"].value_counts()
        parts = [f"{_ACTION_KO.get(k, k)} {cnt[k]}" for k in ("INIT", "WOULD_BUY", "SKIP", "CONFIRM_FAIL", "REMOVE", "WOULD_SELL", "WAIT")
                 if k in cnt]
        print(f"    판단: " + " · ".join(parts))
        skips = g[g["action"].isin(["SKIP", "CONFIRM_FAIL"])]["reason"].str.replace(r"\s*\(.*$", "", regex=True).value_counts().head(4)
        if len(skips):
            print("    건너뛴 이유: " + ", ".join(f"{r} {n}" for r, n in skips.items()))
    if rows:
        last = df[df["action"] != "INIT"].tail(rows)
        for r in last.itertuples():
            pr = f" {r.price:,.0f}원" if r.price == r.price and r.price else ""
            print(f"  {r.ts[11:19]} [{r.cond_name}] {r.event} {r.symbol}{pr} → {_ACTION_KO.get(r.action, r.action)}"
                  + (f" — {r.reason}" if r.reason else ""))


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
    print(f"키: {'있음' if cfg['_secrets']['app_key'] else '없음 (대시보드 로그인 또는 ktrade login)'}")
    try:
        print("켠 매매조건:", [f"{i.id} {i.version}" for i, _, _ in loader.enabled(cfg)])
    except Exception as exc:
        print("매매조건 오류:", exc)
    if p["db"].exists():
        s = Store(p["db"], readonly=True)
        d = s.market.daily_stats()
        print(f"일봉: {d['symbols']}종목, 마지막 {d['to']}")
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


def cmd_doctor(a, exit_on_fail: bool = True) -> None:
    from ktrade.engine import doctor
    rows = doctor.checks(_cfg(), online=not a.offline)
    print(doctor.render(rows))
    if exit_on_fail and any(ok is False for _, ok, _ in rows):
        sys.exit(1)


def cmd_schedule(a) -> None:
    from ktrade.engine import schedule
    cfg = _cfg()
    if a.action == "install":
        print(schedule.install(a.dry_run, cfg))
    elif a.action == "remove":
        print(schedule.remove(a.dry_run))
    else:
        s = schedule.settings(cfg)
        print(f"설정: daily {s['daily'] or '안 함'} · run {s['run'] or '안 함'} · {','.join(s['days'])}  (config.yaml schedule)")
        print(schedule.status())


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cmd_dashboard(a) -> None:
    import subprocess
    import time
    import webbrowser

    def stop(msg):
        if a.open:  # 바로가기로 연 창은 오류를 읽을 틈 없이 닫히므로 기다린다
            print(msg)
            try:
                input("\nEnter를 누르면 창을 닫습니다...")
            except EOFError:
                pass
            sys.exit(1)
        sys.exit(msg)

    try:
        import streamlit  # noqa: F401
    except ImportError:
        stop('대시보드 구성 요소가 설치되지 않았습니다. 설치 스크립트를 다시 실행하거나:\n'
             '  uv tool install --python 3.12 --force "ktrade[dashboard] @ <릴리스의 .whl 주소>"')
    url = f"http://127.0.0.1:{a.port}"
    if _port_open(a.port):
        print(f"대시보드가 이미 떠 있습니다: {url}")
        if a.open:
            webbrowser.open(url)
            time.sleep(2)
        return
    app = Path(__file__).parent / "dashboard" / "app.py"
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(app), "--server.address", "127.0.0.1",
                             "--server.port", str(a.port), "--server.headless", "true",
                             "--browser.gatherUsageStats", "false"])
    print(f"ktrade 대시보드: {url}  (이 창을 닫거나 Ctrl+C 하면 꺼집니다)")
    if a.open:
        for _ in range(120):
            if _port_open(a.port) or proc.poll() is not None:
                break
            time.sleep(0.5)
        if proc.poll() is None:
            webbrowser.open(url)
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        code = 0
    if code and a.open:
        stop(f"대시보드가 오류로 끝났습니다 (코드 {code}). 위 메시지를 확인하세요.")


def cmd_intro(a) -> None:
    """'ktrade 명령창' 바로가기가 처음 보여 주는 화면."""
    cmd_doctor(argparse.Namespace(offline=True), exit_on_fail=False)
    print(f"\nktrade {__version__} — 명령을 입력하세요. 자주 쓰는 것:")
    print("  ktrade collect --dry-run   받을 데이터 범위 확인  →  ktrade collect")
    print("  ktrade new 이름            매매조건 만들기       →  ktrade test 이름 (백테스트)")
    print("  ktrade data                받아 둔 데이터 범위    ·  ktrade dashboard --open")
    print("  ktrade --help              전체 목록")


def cmd_shortcut(a) -> None:
    from ktrade import shortcut
    try:
        print(shortcut.install(a.dry_run) if a.action == "install" else shortcut.remove(a.dry_run)
              if a.action == "remove" else shortcut.status())
    except RuntimeError as exc:
        sys.exit(f"바로가기를 만들지 못했습니다: {exc}")


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
    add("login", cmd_login, (("--app-key",), {"help": "앱키 (없으면 물어봄). 시크릿키는 항상 물어봄"}),
        (("--real",), {"action": "store_true", "help": "실전투자 키 (기본은 모의투자 키)"}),
        (("--no-check",), {"action": "store_true", "help": "키움 연결 확인 없이 저장"}))
    add("logout", cmd_logout)
    add("real-orders", cmd_real_orders, (("action",), {"choices": ["on", "off", "show"]}),
        (("--yes",), {"action": "store_true", "help": "확인 질문 없이 동의"}))
    add("access", cmd_access, (("action",), {"choices": ["login", "logout", "status"]}), (("--id",), {}),
        (("--code",), {"help": "이용 코드 (없으면 물어봄)"}))
    add("admin", cmd_admin, (("action",), {"choices": ["init", "issue", "list"]}), (("id",), {"nargs": "?"}),
        (("--role",), {"choices": ["backtest", "admin"], "default": "backtest"}),
        (("--days",), {"type": int, "default": 365, "help": "유효 기간(일). 0 = 만료 없음"}), (("--note",), {}))
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
        (("--exclude-kinds",), {"help": "시장 범위에서 뺄 종류 (쉼표 구분: ETF,ETN,스팩,리츠,우선주)"}),
        (("--keep-all-kinds",), {"action": "store_true", "help": "종류로 빼지 않음"}),
        (("--price-min",), {"type": float, "help": "전일 종가(원) 이상"}), (("--price-max",), {"type": float}),
        (("--cap-min",), {"type": float, "help": "시가총액(억 원) 이상"}), (("--cap-max",), {"type": float}),
        (("--no-daily",), {"action": "store_true", "help": "일봉은 받지 않음 (분봉·틱만)"}),
        (("--minute-symbols",), {"help": "분봉도 받을 종목 (쉼표 구분, all = 위 범위 전체)"}), (("--minute-start",), {}),
        (("--minute-intervals",), {"help": "분 단위 (쉼표 구분: 1,3,5,10,15,30,45,60)"}),
        (("--tick-symbols",), {"help": "틱도 받을 종목 (쉼표 구분, all = 위 범위 전체)"}), (("--tick-start",), {}),
        (("--tick-units",), {"help": "틱 단위 (쉼표 구분: 1,3,5,10,30)"}),
        (("--limit",), {"type": int}), (("--dry-run",), {"action": "store_true", "help": "범위만 보여 주고 받지 않음"}))
    add("data", cmd_data, (("--symbols",), {}), (("--all",), {"action": "store_true"}))
    add("data-dir", cmd_data_dir, (("path",), {"nargs": "?", "help": "새 데이터 폴더 (없으면 지금 폴더만 보여 줌)"}),
        (("--no-move",), {"action": "store_true", "help": "기존 데이터를 옮기지 않고 폴더만 바꿈"}))
    add("hts", cmd_hts, (("action",), {"choices": ["list", "scan", "watch", "signals", "report"]}),
        (("names",), {"nargs": "*", "help": "조건식 이름 또는 번호 (scan: 하나, watch: 여러 개)"}),
        (("--csv",), {"help": "검색 결과를 CSV로 저장"}), (("--limit",), {"type": int, "default": 50, "help": "화면에 보일 줄 수"}),
        (("--until",), {"help": "watch를 끝낼 시각 HH:MM (기본 장 마감)"}), (("--date",), {"help": "signals 날짜 (기본 오늘)"}),
        (("--cond",), {"help": "signals·report: 이 조건식만"}), (("--verbose",), {"action": "store_true", "help": "watch: 등록 시점 편입 종목도 표시"}),
        (("--days",), {"type": int, "help": "report: 최근 N일 (신호가 있는 날 기준)"}),
        (("--collect",), {"action": "store_true", "help": "report: 신호 종목 1분봉을 키움에서 받아 계산"}))
    add("import-db", cmd_import_db, (("path",), {}), (("--since",), {}), (("--minute-days",), {"type": int, "default": 0}))
    add("plan", cmd_plan, (("--asof",), {}), (("--only",), {"nargs": "*"}))
    add("daily", cmd_daily, (("--no-collect",), {"action": "store_true"}), (("--no-minute",), {"action": "store_true"}),
        (("--limit",), {"type": int}))
    add("run", cmd_run, (("--once",), {"action": "store_true"}), (("--live-confirm",), {"action": "store_true"}))
    add("status", cmd_status)
    add("report", cmd_report)
    add("doctor", cmd_doctor, (("--offline",), {"action": "store_true"}))
    add("schedule", cmd_schedule, (("action",), {"choices": ["install", "remove", "show"]}), (("--dry-run",), {"action": "store_true"}))
    add("dashboard", cmd_dashboard, (("--open",), {"action": "store_true", "help": "브라우저로 열기 (바로가기용)"}),
        (("--port",), {"type": int, "default": 8501}))
    sub.add_parser("intro", help=argparse.SUPPRESS).set_defaults(fn=cmd_intro)
    add("shortcut", cmd_shortcut, (("action",), {"choices": ["install", "remove", "show"]}),
        (("--dry-run",), {"action": "store_true"}))
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
