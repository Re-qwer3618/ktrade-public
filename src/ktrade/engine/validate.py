"""전략 검사 (ktrade validate) — 내가 만든(또는 AI가 써 준) 전략을 켜기 전에 확인한다. 아무것도 바꾸지 않는다.

1) 정적 검사: strategy.py가 엔진 내부(주문·저장소·수집기)를 import하면 오류, 네트워크·외부 명령·파일 쓰기는 경고.
   전략은 ktrade.sdk와 pandas·numpy 같은 계산 라이브러리만 쓰는 게 원칙이다(실전·백테스트에서 똑같이 돌게).
2) 실행 검사: 데이터가 있으면 기준일(기본 마지막 거래일)에 select()를 한 번 돌려 돌려준 값의 형식,
   엔진 청산 규칙으로 만든 계획의 엔진 검사(guard) 통과 여부, 걸린 시간을 본다.
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path

from ktrade.config import paths
from ktrade.engine import exits as exits_mod
from ktrade.engine import guard, loader
from ktrade.engine.planner import compose
from ktrade.sdk.context import Context, DataView
from ktrade.sdk.models import Pick

FORBIDDEN = ("ktrade.broker", "ktrade.engine", "ktrade.data", "kiwoom_client", "sqlite3")
CAUTION = {"requests": "네트워크", "httpx": "네트워크", "urllib": "네트워크", "socket": "네트워크", "aiohttp": "네트워크",
           "subprocess": "외부 명령", "shutil": "파일 조작"}
CAUTION_CALLS = {"open": "파일 열기", "exec": "동적 실행", "eval": "동적 실행", "system": "외부 명령"}
SLOW_SEC = 60


@dataclass
class Report:
    strategy: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [f"전략 검사: {self.strategy} — {'통과' if self.ok else '실패'}"]
        lines += [f"  ✖ {e}" for e in self.errors] + [f"  ! {w}" for w in self.warnings] + [f"  · {n}" for n in self.notes]
        if self.ok:
            lines.append("다음: ktrade test <조건>  (검사 + 백테스트 + 이전 실험과 비교)")
        return "\n".join(lines)


def static_check(path: Path, rep: Report) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        rep.errors.append(f"문법 오류 {path.name}:{exc.lineno} {exc.msg}")
        return
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module]
        for m in mods:
            if any(m == f or m.startswith(f + ".") for f in FORBIDDEN):
                rep.errors.append(f"{path.name}:{node.lineno} '{m}' import — 전략은 ktrade.sdk만 쓴다 (주문·저장소·수집은 엔진 몫)")
            elif m.split(".")[0] in CAUTION:
                rep.warnings.append(f"{path.name}:{node.lineno} '{m}' ({CAUTION[m.split('.')[0]]}) — 백테스트와 실전 결과가 달라질 수 있음")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name in CAUTION_CALLS:
                rep.warnings.append(f"{path.name}:{node.lineno} {name}() ({CAUTION_CALLS[name]})")


def validate(cfg: dict, strategy_id: str, asof: str | None = None, params: dict | None = None) -> Report:
    rep = Report(strategy_id)
    found = loader.discover(cfg)
    if strategy_id not in found:
        rep.errors.append(f"전략 '{strategy_id}'를 찾지 못했습니다 — 폴더 위치: {paths(cfg)['strategies']}/<id>/strategy.yaml")
        return rep
    info = found[strategy_id]
    rep.notes.append(f"위치 {info.source}, 버전 {info.version}, 코드 지문 {info.code_hash}")
    if not info.source.startswith("entry-point:"):
        f = Path(info.source) / info.entry.partition(":")[0]
        if not f.exists():
            rep.errors.append(f"entry 파일이 없습니다: {f}")
            return rep
        static_check(f, rep)
    try:
        strat = loader.load(info, params)
    except Exception as exc:
        rep.errors.append(f"불러오기 실패: {type(exc).__name__}: {exc}")
        return rep
    if info.kind == "intraday":
        return _validate_intraday(strat, rep)
    db = paths(cfg)["db"]
    if not db.exists():
        rep.warnings.append("시세 DB가 없어 실행 검사는 건너뜀 (ktrade collect 또는 import-db 후 다시)")
        return rep
    from ktrade.data.store import Store
    from ktrade.engine.planner import last_trading_day
    store = Store(db, readonly=True)
    day = asof or last_trading_day(store)
    if not day:
        rep.warnings.append("지수 일봉이 없어 실행 검사는 건너뜀")
        return rep
    ctx = Context(asof=day, data=DataView(store, day), params=strat.params, strategy_id=info.id)
    try:
        rules = loader.exits_for(cfg, info)
        rep.notes.append(f"청산(엔진): {exits_mod.describe(rules)}")
    except ValueError as exc:
        rep.errors.append(str(exc))
        return rep
    t0 = time.perf_counter()
    try:
        out = strat.select(ctx)
        el = time.perf_counter() - t0
        if not isinstance(out, list) or not all(isinstance(p, Pick) for p in out):
            rep.errors.append(f"select()는 Pick 목록을 돌려줘야 함 (받은 것: {type(out).__name__}"
                              + (f"[{type(out[0]).__name__}]" if isinstance(out, list) and out else "") + ")")
            return rep
        plans = compose(out, rules, ctx.data)
        syms = [p.symbol for p in plans]
        closes = store.market.closes(day, syms) if syms else {}
        ok, rej = guard.screen([(info.id, plans)], closes, int(cfg["risk"]["max_plans_per_strategy"]))
        rep.notes.append(f"{day} 기준 select(): 후보 {len(out)}개, 엔진 검사 통과 {len(ok[info.id])}개, {el:.1f}초")
        for _, sym, why in rej[:5]:
            rep.warnings.append(f"엔진 검사에서 걸러짐 {sym}: {why}")
        for p in ok[info.id][:3]:
            st = f"{p.stop:,.0f}" if p.stop else "없음"
            tg = f"{p.target:,.0f}" if p.target else "없음"
            rep.notes.append(f"예: {p.symbol} {p.name} 매수 {p.entry.min_price:,.0f}~{p.entry.max_price:,.0f} ({p.entry.after} 이후) "
                             f"→ 손절 {st} / 목표 {tg} — {p.reason[:50]}")
        if not out:
            rep.notes.append("이날은 후보가 없음 (조건이 너무 엄격한지 다른 날로도 보세요: --asof)")
        if el > SLOW_SEC:
            rep.warnings.append(f"{el:.0f}초 걸림 — 백테스트는 거래일마다 부르므로 오래 걸립니다 (daily_all bars를 줄여 보세요)")
    except Exception as exc:
        import traceback
        rep.errors.append("실행 중 오류:\n" + "".join(traceback.format_exception(exc, limit=-3)).rstrip())
    return rep


def _validate_intraday(strat, rep: Report) -> Report:
    """장중 조건 — 가짜 1분봉(오르는 날·내리는 날)으로 check()를 불러 오류 없이 None 또는 Signal을 돌려주는지."""
    import numpy as np
    import pandas as pd

    from ktrade.sdk.intraday import IntradayContext, Signal
    rep.notes.append("장중 조건(intraday) — 가짜 분봉으로 check() 실행 검사 (수익과 무관)")
    day = pd.Timestamp("2026-01-05 09:00")
    for label, drift in (("오르는 날", 0.0008), ("내리는 날", -0.0008)):
        n = 120
        close = 10000 * np.cumprod(1 + np.full(n, drift))
        mins = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close,
                             "volume": np.full(n, 1000.0)}, index=pd.date_range(day, periods=n, freq="min"))
        daily = pd.DataFrame({"open": 10000.0, "high": 10100.0, "low": 9900.0, "close": 10000.0, "volume": 100000.0},
                             index=pd.date_range("2025-06-01", periods=150, freq="B"))
        ctx = IntradayContext(symbol="000000", now=mins.index[-1].to_pydatetime(), price=float(close[-1]), minutes=mins,
                              daily=daily, params=strat.params, event="watch")
        t0 = time.perf_counter()
        try:
            out = strat.check(ctx)
        except Exception as exc:
            import traceback
            rep.errors.append(f"{label} check() 오류:\n" + "".join(traceback.format_exception(exc, limit=-3)).rstrip())
            continue
        if out is not None and not isinstance(out, (Signal, bool)):
            rep.errors.append(f"{label}: check()는 Signal 또는 None을 돌려줘야 합니다 (받은 값 {type(out).__name__})")
            continue
        el = time.perf_counter() - t0
        rep.notes.append(f"{label}: " + ("매수 신호 — " + (out.reason if isinstance(out, Signal) else "") if out else "신호 없음")
                         + f" ({el * 1000:.0f}ms)")
        if el > 1.0:
            rep.warnings.append(f"{label} check()가 {el:.1f}초 — 장중엔 종목마다 자주 부르므로 가볍게 쓰세요")
    return rep
