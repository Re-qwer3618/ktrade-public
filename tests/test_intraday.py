"""장중 조건(Intraday): ① 조건검색 편입 뒤 파이썬 확인 ② 감시 목록 장중 신호 매수, 키움 데이터 정합 확인, 백테스트."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from ktrade.broker.paper import PaperBroker
from ktrade.data.store import Store
from ktrade.engine import backtest, experiments, forward, loader, planner
from ktrade.engine import exits as X
from ktrade.engine.executor import Executor
from ktrade.engine.intraday import KiwoomSource, Runner, StoredSource, verify_plans
from ktrade.engine.risk import Risk
from ktrade.engine.signals import SignalBook, load_conditions
from ktrade.sdk import Intraday, Signal

T0 = datetime(2026, 10, 26, 9, 30, 0)


class Ev:
    def __init__(self, kind, symbol, cond="단타", seq="1", time="093000"):
        self.kind, self.symbol, self.cond, self.seq, self.time, self.detail = kind, symbol, cond, seq, time, ""


class FakeRunner:
    """check(...) → 정해 둔 답. 부른 기록을 남긴다."""
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def check(self, iid, params, symbol, now, price, name="", event="watch", insert_price=None):
        self.calls.append((iid, symbol, event))
        return self.answer(symbol, price) if callable(self.answer) else self.answer


def _bars(day: str, start: str, closes: list[float], vols: list[float] | None = None) -> pd.DataFrame:
    t0 = datetime.fromisoformat(f"{day} {start}")
    vols = vols or [1000.0] * len(closes)
    return pd.DataFrame([{"ts": (t0 + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), "open": c, "high": c * 1.001,
                          "low": c * 0.999, "close": c, "volume": v} for i, (c, v) in enumerate(zip(closes, vols))])


def test_hts_insert_python_confirm(tmp_path):
    """① 편입 → 파이썬 확인 불통과면 SKIP(이유 남김), 통과면 매수."""
    store = Store(tmp_path / "m.db")
    broker = PaperBroker(tmp_path / "p.json", 10_000_000)
    px = {"A": 10000.0, "B": 5000.0}
    ex = Executor(store, broker, Risk(10_000_000, max_position_pct=10), lambda s: {"price": px[s], "open": px[s]}, role="hts")
    conds = load_conditions({"hts_conditions": [{"name": "단타", "mode": "trade", "confirm": "my_check"}]})
    runner = FakeRunner(lambda sym, price: (sym == "B", "VWAP 위" if sym == "B" else "VWAP 아래", None))
    book = SignalBook(store, conds, px.get, executor=ex, allow_trade=True, confirm=runner)
    a = book.handle(Ev("insert", "A"), T0)[0]
    assert a["action"] == "SKIP" and "파이썬 확인 'my_check' 불통과 — VWAP 아래" in a["reason"]
    b = book.handle(Ev("insert", "B", seq="2"), T0 + timedelta(seconds=5))[0]
    assert b["action"] == "BUY" and "파이썬 확인 'my_check' 통과" in b["reason"] and broker.position("B") > 0
    assert runner.calls == [("my_check", "A", "insert"), ("my_check", "B", "insert")]


def _plan(sym, pid, intraday=None):
    return {"plan_id": pid, "symbol": sym, "name": sym, "rank": 1, "stop": None, "target": None, "max_hold_days": 5,
            "entry": {"min_open": 99999, "min_price": 1, "max_price": 2, "after": "15:10"},     # Entry는 무시돼야 함
            "exits": X.resolve(), "intraday": intraday or {}}


def test_watchlist_buys_on_intraday_signal(tmp_path):
    """② 감시 목록 계획 — Entry(시가·시각·구간) 대신 장중 조건 신호로 산다. 신호 전엔 대기 이유를 남긴다."""
    store = Store(tmp_path / "m.db")
    broker = PaperBroker(tmp_path / "p.json", 10_000_000)
    px = {"A": 10000.0}
    state = {"go": False}
    runner = FakeRunner(lambda sym, price: (state["go"], "돌파" if state["go"] else "아직", 10100.0))
    ex = Executor(store, broker, Risk(10_000_000, max_position_pct=10), lambda s: {"price": px[s], "open": px[s]},
                  role="plan", intraday=runner)
    ex.set_plans([("brk", "v1", _plan("A", "p-A", {"id": "my_watch", "params": {}}))])
    ex.check("A", T0)
    assert store.active_plan_trade("A") is None
    assert any("장중 조건 'my_watch' 대기 — 아직" in d for d in store.query("SELECT detail FROM decisions")["detail"])
    state["go"] = True
    px["A"] = 10200.0                                          # 상한 10,100 위 → 안 삼
    ex.check("A", T0 + timedelta(minutes=1))
    assert store.active_plan_trade("A") is None
    px["A"] = 10050.0
    ex.check("A", T0 + timedelta(minutes=2))
    assert store.active_plan_trade("A")["status"] == "open"


def test_verify_plans_against_kiwoom():
    """내 데이터로 만든 계획 ↔ 키움 일봉 기준일 종가: 어긋나거나 확인 못 하면 뺀다."""
    items = [("s", "v1", {"symbol": "A", "plan_id": "a"}), ("s", "v1", {"symbol": "B", "plan_id": "b"}),
             ("s", "v1", {"symbol": "C", "plan_id": "c"})]
    kd = {"A": 10000.0, "B": 5000.0}

    def kiwoom_daily(sym, since):
        if sym == "C":
            raise RuntimeError("조회 실패")
        return pd.DataFrame({"date": ["2026-10-23", "2026-10-26"], "close": [1.0, kd[sym]]})
    keep, dropped = verify_plans(items, {"A": 10010.0, "B": 2500.0, "C": 100.0}, kiwoom_daily, "2026-10-26")
    assert [p["symbol"] for _, _, p in keep] == ["A"]                         # 0.1% 차이는 통과
    why = {s: w for _, s, w in dropped}
    assert "데이터 불일치" in why["B"] and "확인하지 못함" in why["C"]


class Rising(Intraday):
    default_params = {"n": 3}

    def check(self, ctx):
        m = ctx.minutes
        if len(m) > self.params["n"] and ctx.price > m["high"].iloc[-(self.params["n"] + 1):-1].max():
            return Signal.yes("3분 고가 돌파")
        return None


def test_stored_source_and_runner(tmp_path, monkeypatch):
    store = Store(tmp_path / "m.db")
    store.upsert_minute("A", _bars("2026-10-26", "09:00:00", [100, 100, 100, 100, 103]))
    src = StoredSource(store)
    assert len(src.minutes("A", datetime(2026, 10, 26, 9, 3))) == 4                # 그 시각까지만
    runner = Runner({}, src)
    info = loader.StrategyInfo(id="rising", version="v1", description="", author="", source="x", params={}, kind="intraday",
                               cls=Rising)
    monkeypatch.setattr(loader, "discover", lambda cfg: {"rising": info})
    assert runner.check("rising", None, "A", datetime(2026, 10, 26, 9, 3), 100)[0] is False
    ok, why, _ = runner.check("rising", None, "A", datetime(2026, 10, 26, 9, 4), 103)
    assert ok and why == "3분 고가 돌파"
    assert runner.check("없음", None, "A", T0, 1)[1].startswith("장중 조건 '없음' 오류")


def test_kiwoom_source_caches_minutes():
    calls = {"m": 0}

    class K:
        def minute(self, sym, since, max_pages, interval):
            calls["m"] += 1
            return _bars("2026-10-26", "09:00:00", [1, 2, 3])

        def daily(self, sym, since):
            return pd.DataFrame({"date": ["2026-10-23", "2026-10-26"], "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
    clock = {"t": 0.0}
    src = KiwoomSource(K(), min_gap=20, clock=lambda: clock["t"])
    src.minutes("A", datetime(2026, 10, 26, 9, 5))
    src.minutes("A", datetime(2026, 10, 26, 9, 5))
    assert calls["m"] == 1
    clock["t"] = 25
    src.minutes("A", datetime(2026, 10, 26, 9, 5))
    assert calls["m"] == 2
    assert list(src.daily("A", datetime(2026, 10, 26, 9, 5)).index.strftime("%Y-%m-%d")) == ["2026-10-23"]   # 오늘 제외


def test_planner_attaches_intraday_and_setup_hash(market, cfg):
    """계획에 장중 조건을 붙이면 계획 파일에 들어가고, 백테스트 지문도 장중 조건까지 포함한다."""
    store, days = market
    cfg["strategies"]["enabled"][0]["intraday"] = "example_vwap_breakout"
    res = planner.run_plans(cfg, store, days[59])
    assert res["strategies"] == {"example_breakout": 1}
    import json
    doc = json.loads((planner.paths(cfg)["plans"] / days[59] / "example_breakout.json").read_text(encoding="utf-8"))
    assert doc["plans"][0]["intraday"]["id"] == "example_vwap_breakout"
    found = loader.discover(cfg)
    info, cond, rules = loader.enabled(cfg)[0]
    h0 = experiments.setup_hash(info, cond.params, rules)
    h1 = experiments.setup_hash(info, cond.params, rules, experiments.intraday_key(found, cfg["strategies"]["enabled"][0]))
    assert h0 != h1
    cfg["strategies"]["enabled"][0]["intraday"] = "없는조건"
    assert "없는조건" in planner.run_plans(cfg, store, days[59])["errors"]["example_breakout"]


def test_backtest_with_intraday_uses_minutes(market, cfg):
    """백테스트: 실행일 1분봉이 있으면 장중 조건 첫 신호 분봉 종가에 매수, 없으면 no_minutes로 셈."""
    store, days = market
    info, cond, rules = loader.enabled(cfg)[0]
    base = backtest.run(store, info, cond, rules, days[40], days[-1])
    res = backtest.run(store, info, cond, rules, days[40], days[-1], intraday=Rising())
    assert res["no_minutes"] >= 1 and res.get("trades", 0) == 0 and base["picks"] == res["picks"]
    # 첫 계획의 실행일 분봉을 넣으면 그 계획은 장중 조건으로 산다
    view_days = [d for d in days if d >= days[40]]
    for d in view_days:
        from ktrade.sdk.context import Context, DataView
        picks = cond.select(Context(asof=d, data=DataView(store, d), params=cond.params, strategy_id=info.id))
        if picks:
            d1 = days[days.index(d) + 1]
            close = float(store.market.daily(picks[0].symbol, end=d, last=1)["close"].iloc[-1])
            store.upsert_minute(picks[0].symbol, _bars(d1, "09:00:00", [close] * 4 + [close * 1.02] + [close * 1.02] * 30))
            break
    res2 = backtest.run(store, info, cond, rules, days[40], days[-1], intraday=Rising())
    assert res2["no_minutes"] < res["no_minutes"] and res2["triggered"] >= 1   # 산 뒤 보유 중엔 같은 종목 계획을 건너뜀


def test_confirm_report_splits_passed_and_failed(tmp_path, monkeypatch):
    """① 백테스트: 받아 둔 신호 일지 + 1분봉으로 파이썬 확인 통과/불통과별 '그때 샀다면'."""
    store = Store(tmp_path / "m.db")
    for sym, path in (("A", [100, 100, 100, 100, 103, 106, 106]), ("B", [100, 99, 98, 97, 96, 95, 94])):
        store.upsert_minute(sym, _bars("2026-10-26", "09:00:00", path))
        store.add_signal("단타", "1", sym, "I", "WOULD_BUY", price=103 if sym == "A" else 96, event_time="090400",
                         ts="2026-10-26T09:04:10")
    info = loader.StrategyInfo(id="rising", version="v1", description="", author="", source="x", params={}, kind="intraday",
                               cls=Rising)
    monkeypatch.setattr(loader, "discover", lambda cfg: {"rising": info})
    rep = forward.confirm_report(store, {"hts_conditions": [{"name": "단타"}]}, ["2026-10-26"], "단타", "rising")
    t = rep["table"].set_index("symbol")
    assert bool(t.at["A", "통과"]) and not bool(t.at["B", "통과"])
    assert rep["passed"]["n"] == 1 and rep["failed"]["n"] == 1 and rep["all"]["n"] == 2


def test_ops_table_python_confirm_roundtrip():
    from ktrade.dashboard import ops
    cfg = {"hts_conditions": [{"name": "단타", "mode": "trade", "confirm": "my_check"}, {"name": "관찰"}]}
    t = ops.conditions_table(cfg)
    assert t["파이썬 확인"].tolist() == ["my_check", ""]
    t.loc[1, "파이썬 확인"] = "vwap"
    t.loc[0, "파이썬 확인"] = ""
    out = {c["name"]: c for c in ops.conditions_from_table(t, cfg["hts_conditions"], None)}
    assert "confirm" not in out["단타"] and out["관찰"]["confirm"] == "vwap"
