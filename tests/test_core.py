from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

from ktrade import cli
from ktrade.broker.paper import PaperBroker
from ktrade.config import paths
from ktrade.engine import backtest, experiments, guard, live, loader, planner
from ktrade.engine import exits as X
from ktrade.engine.executor import Executor, exit_reason
from ktrade.engine.risk import Risk
from ktrade.sdk import Context, DataView, Entry, Plan


# ---- SDK ----
def test_entry_helpers():
    e = Entry.confirm(10000, chase_pct=5)
    assert (e.min_open, e.min_price, e.max_price, e.after) == (10000, 10000, 10500, "15:10")
    o = Entry.at_open(10000, 3)
    assert o.after == "09:00" and o.min_price == 9700 and o.max_price == 10300
    p = Entry.pullback(9800, 10100)
    assert p.min_open < 1 and p.after == "09:00"


def test_plan_roundtrip():
    pl = Plan("AAA001", Entry.confirm(10300), 9500, 11330, reason="r", exits=X.resolve())
    d = pl.to_dict("s", "v1", "2025-03-25")
    back = Plan.from_dict(d)
    assert d["plan_id"] == "s-v1-20250325-AAA001" and back.exits["stop"]["type"] == "pct" and back.reason == "r"


def test_dataview_has_no_future(market):
    store, days = market
    v = DataView(store, days[30])
    assert v.daily("AAA001").index.max() == pd.Timestamp(days[30])
    assert v.daily_all(5)["date"].max() == days[30]


# ---- 엔진 청산 규칙 ----
def test_exit_rules():
    ex = X.resolve()
    assert X.levels(ex, 10000) == (9300, 11000)
    r = X.resolve({"stop": {"type": "pct", "value": 5}, "take_profit": {"type": "r", "value": 2}})
    assert X.levels(r, 10000) == (9500, 11000)
    bars = pd.DataFrame({"open": [100] * 20, "high": [105] * 20, "low": [95] * 20, "close": [100] * 20})
    atr = X.prepare(X.resolve({"stop": {"type": "atr", "value": 2, "period": 14}}), bars)
    assert atr["stop"]["atr"] == pytest.approx(10) and X.levels(atr, 100)[0] == 80
    low = X.prepare(X.resolve({"stop": {"type": "low", "value": 10, "buffer_pct": 1}}), bars)
    assert X.levels(low, 100)[0] == pytest.approx(94.05)
    tr = X.resolve({"trailing": {"activate_pct": 5, "trail_pct": 4}})
    assert X.trail_level(tr, 100, 104) is None and X.trail_level(tr, 100, 110) == pytest.approx(105.6)
    with pytest.raises(ValueError):
        X.resolve({"stop": {"type": "bogus"}})
    with pytest.raises(ValueError):
        X.resolve({"stop": {"type": "none"}, "take_profit": {"type": "r", "value": 2}})
    assert "손실제한" in X.describe(ex)


# ---- 검사 ----
def test_guard_rejects_nonsense():
    good = Plan("AAA001", Entry.confirm(10300), 9500, 11330)
    assert guard.check(good, 10300) == []
    assert any("손절가" in e for e in guard.check(Plan("AAA001", Entry.confirm(10300), 10500, 11330), 10300))
    assert guard.check(Plan("AAA001", Entry.confirm(10300), None, None), 10300) == []   # 손절·익절 없음도 허용
    ok, rej = guard.screen([("a", [good]), ("b", [good])], {"AAA001": 10300}, 5)
    assert len(ok["a"]) == 1 and not ok["b"] and "우선순위" in rej[0][2]


# ---- 불러오기 ----
def test_loader_examples_and_scaffold(cfg, home):
    found = loader.discover(cfg)
    assert {"example_breakout", "example_pullback"} <= set(found)
    assert found["example_pullback"].exits["stop"]["type"] == "atr"
    cli.main(["new", "my_first"])
    info = loader.discover(cfg)["my_first"]
    assert info.source.startswith(str(home)) and len(info.code_hash) == 12
    assert loader.load(info).params["lookback"] == 20


def test_loader_rejects_non_condition(cfg, home):
    d = home / "strategies" / "old"
    d.mkdir(parents=True)
    (d / "strategy.yaml").write_text("id: old\nkind: signal\nentry: strategy.py:S\n", encoding="utf-8")
    found = loader.discover(cfg)                       # 잘못된 폴더 하나 때문에 다른 조건이 막히지 않는다
    assert "example_breakout" in found and "old" not in found
    assert any("Condition.select" in why for why in loader.INVALID.values())


# ---- 계획 ----
def test_compose_uses_engine_exits(market, cfg):
    store, days = market
    info, cond, rules = loader.enabled(cfg)[0]
    picks = cond.select(Context(asof=days[59], data=DataView(store, days[59]), params=cond.params))
    plans = planner.compose(picks, rules, DataView(store, days[59]))
    assert [p.symbol for p in plans] == ["AAA001"]
    p = plans[0]
    mid = (10300 + 10815) / 2
    assert p.stop == pytest.approx(mid * 0.93) and p.target == pytest.approx(mid * 1.10) and p.max_hold_days == 10


def test_planner_requires_backtest(market, cfg):
    store, days = market
    cfg["strategies"]["require_backtest"] = True
    res = planner.run_plans(cfg, store, days[59])
    assert res["strategies"] == {} and "ktrade test example_breakout" in res["skipped"]["example_breakout"]
    info, cond, rules = loader.enabled(cfg)[0]
    experiments.save(cfg, info, cond.params, rules, {"trades": 0})
    res = planner.run_plans(cfg, store, days[59])
    assert res["strategies"] == {"example_breakout": 1}
    # 파라미터를 바꾸면 다시 백테스트해야 한다
    cfg["strategies"]["enabled"][0]["params"]["max_plans"] = 3
    assert planner.run_plans(cfg, store, days[59])["strategies"] == {}


def test_external_plan_file_gets_engine_exits(market, cfg, tmp_path):
    store, days = market
    ext = tmp_path / "ext"
    ext.mkdir()
    no_exit = {"symbol": "AAA001", "name": "에이", "entry": {"min_open": 10300, "min_price": 10300, "max_price": 10800}}
    doc = {"schema": 1, "asof": days[59], "strategy": "other_lang", "version": "v1", "plans": [no_exit]}
    (ext / f"{days[59]}.json").write_text(json.dumps(doc), encoding="utf-8")
    cfg["strategies"]["enabled"] = []
    cfg["external_plans"] = [{"id": "other_lang", "dir": str(ext)}]
    res = planner.run_plans(cfg, store, days[59])
    assert res["strategies"] == {"other_lang": 1}
    saved = json.loads((paths(cfg)["plans"] / days[59] / "other_lang.json").read_text(encoding="utf-8"))["plans"][0]
    assert saved["stop"] and saved["target"] and saved["exits"]["stop"]["type"] == "pct"


# ---- 실행 ----
def _executor(store, broker, quotes):
    return Executor(store, broker, Risk(10_000_000), lambda s: quotes[s])


def test_executor_buy_then_target_from_actual_entry(market, cfg, tmp_path):
    store, days = market
    planner.run_plans(cfg, store, days[59])
    items, _ = planner.load_for_today(cfg, store, days[60])
    broker = PaperBroker(tmp_path / "paper.json", 10_000_000)
    quotes = {"AAA001": {"price": 10400, "open": 10350}}
    ex = _executor(store, broker, quotes)
    ex.set_plans(items)
    ex.check("AAA001", datetime.fromisoformat(f"{days[60]}T15:05:00"))
    assert store.plan_trades().empty                                     # 15:10 전
    ex.check("AAA001", datetime.fromisoformat(f"{days[60]}T15:11:00"))
    assert store.plan_trades().iloc[0]["status"] == "open" and broker.position("AAA001") == 19
    quotes["AAA001"] = {"price": 11430, "open": 11000}                   # 10400×1.10 = 11440 미만 → 아직
    ex.check("AAA001", datetime.fromisoformat(f"{days[61]}T10:00:00"))
    assert store.plan_trades().iloc[0]["status"] == "open"
    quotes["AAA001"] = {"price": 11450, "open": 11000}
    ex.check("AAA001", datetime.fromisoformat(f"{days[61]}T10:01:00"))
    tr = store.plan_trades().iloc[0]
    assert tr["status"] == "closed" and "이익실현" in tr["exit_reason"]


def test_exit_reason_stop_timing_and_trailing():
    plan = {"exits": X.resolve(), "max_hold_days": 10}
    trade = {"entry_price": 10000, "entry_date": "2025-03-27", "peak": None}

    def t(hm):
        return datetime.fromisoformat(f"2025-03-28T{hm}:00")
    assert exit_reason(trade, plan, 9200, t("10:00"), "2025-03-28")[0] == ""           # close 모드: 장중엔 손절 안 함
    assert "손실제한" in exit_reason(trade, plan, 9200, t("15:11"), "2025-03-28")[0]
    plan_i = {"exits": X.resolve({"stop_check": "intraday"}), "max_hold_days": 10}
    assert "손실제한" in exit_reason(trade, plan_i, 9290, t("10:00"), "2025-03-28")[0]
    plan_t = {"exits": X.resolve({"take_profit": {"type": "none"}, "trailing": {"activate_pct": 5, "trail_pct": 4}}),
              "max_hold_days": 10}
    r, peak = exit_reason(trade, plan_t, 11000, t("10:00"), "2025-03-28")
    assert r == "" and peak == 11000
    r, _ = exit_reason({**trade, "peak": 11000}, plan_t, 10500, t("10:05"), "2025-03-28")
    assert "트레일링스탑" in r                                                              # 11000 × 0.96 = 10560


def test_executor_skips_when_open_below(market, cfg, tmp_path):
    store, days = market
    planner.run_plans(cfg, store, days[59])
    items, _ = planner.load_for_today(cfg, store, days[60])
    ex = _executor(store, PaperBroker(tmp_path / "p.json", 1e7), {"AAA001": {"price": 10400, "open": 10100}})
    ex.set_plans(items)
    ex.check("AAA001", datetime.fromisoformat(f"{days[60]}T15:11:00"))
    assert store.plan_trades().empty and list(store.query("SELECT signal FROM decisions")["signal"]) == ["PLAN_SKIP"]


# ---- 백테스트 ----
def test_backtest_condition(market, cfg):
    store, days = market
    info, cond, rules = loader.enabled(cfg)[0]
    res = backtest.run(store, info, cond, rules, days[21], days[-1], cost_pct=0.0)
    first = res["table"].iloc[0]
    exit_close = 10400 + (12000 - 10400) / 19 * 9                     # 10일 안에 +10%(11,440) 못 닿음 → 만료
    assert first["asof"] == days[59] and first["reason"] == "보유기간 만료"
    assert first["ret_pct"] == pytest.approx((exit_close / 10400 - 1) * 100)
    assert res["skipped_holding"] > 0 and res["win_rate_ci95"][0] <= res["win_rate"] <= res["win_rate_ci95"][1]
    # 청산 규칙만 바꿔 같은 조건을 다시 실험: 익절 +5% → 목표가로 청산
    r5 = X.resolve({"take_profit": {"type": "pct", "value": 5}})
    res5 = backtest.run(store, info, cond, r5, days[21], days[-1], cost_pct=0.0)
    assert res5["table"].iloc[0]["reason"] == "이익실현"


def test_simulate_is_conservative_when_stop_and_target_same_day(market):
    store, days = market
    ex = X.resolve({"stop": {"type": "pct", "value": 0.1}, "take_profit": {"type": "pct", "value": 0.1}, "stop_check": "intraday"})
    pl = Plan("AAA001", Entry.confirm(10300, 10), None, None, exits=ex)
    res = backtest.simulate(store, pl, days[59], 0.0)                 # 다음 날 저가(-0.2%)·고가(+1.8%)가 ±0.1%를 둘 다 넘음
    assert res["reason"] == "손실제한"


def test_wilson_interval():
    lo, hi = backtest.wilson(6, 10)
    assert 30 < lo < 60 < hi < 85


# ---- 실험 기록 ----
def test_experiments_history_and_diff(market, cfg):
    store, days = market
    info, cond, rules = loader.enabled(cfg)[0]
    r1 = backtest.run(store, info, cond, rules, days[21], days[-1])
    p1 = experiments.save(cfg, info, cond.params, rules, r1)
    r5 = X.resolve({"take_profit": {"type": "pct", "value": 5}})
    p2 = experiments.save(cfg, info, cond.params, r5, backtest.run(store, info, cond, r5, days[21], days[-1]))
    h = experiments.history(cfg, info.id)
    assert len(h) == 2 and p1.with_suffix(".csv").exists()
    assert "청산 설정" in experiments.diff(h[0], json.loads(p2.read_text(encoding="utf-8")))
    assert set(experiments.table(h).columns) >= {"승률%", "승률95%", "평균%", "앞뒤 일관"}


# ---- 장중 한 번 ----
class FakeQuotes:
    def __init__(self, today):
        self.today = today

    def quotes(self, syms):
        return {s: self.quote(s) for s in syms}

    def quote(self, s):
        return {"symbol": s, "price": 10400, "open": 10350, "high": 10450, "low": 10300, "volume": 1, "date": self.today}


def test_live_once(market, cfg, tmp_path):
    store, days = market
    planner.run_plans(cfg, store, days[59])
    broker = PaperBroker(tmp_path / "paper.json", 10_000_000)
    now = datetime.fromisoformat(f"{days[60]}T15:12:00")
    live.run(cfg, once=True, quote_source=FakeQuotes(days[60].replace("-", "")), broker=broker, now_fn=lambda: now)
    assert broker.position("AAA001") > 0
    broker2 = PaperBroker(tmp_path / "paper2.json", 10_000_000)
    live.run(cfg, once=True, quote_source=FakeQuotes("20000101"), broker=broker2,
             now_fn=lambda: datetime.fromisoformat(f"{days[61]}T15:12:00"))
    assert broker2.positions() == {}


def test_live_refuses_real_account_without_confirm(cfg):
    cfg["broker"], cfg["kiwoom"]["is_mock"] = "kiwoom", False
    with pytest.raises(SystemExit, match="--live-confirm"):
        live.run(cfg, once=True)



def test_screen_spec_filters_stored_daily(market):
    """자연어 검색의 계산 쪽: spec(JSON) → 받아 둔 일봉에 적용. AI가 만든 코드는 실행하지 않는다."""
    from ktrade.engine import screen
    store, days = market
    asof = days[-1]
    allrows = screen.run(store, {"rules": [], "limit": 999, "exclude_kinds": False}, asof)
    assert len(allrows) > 0 and {"종목코드", "종목명", "현재가"} <= set(allrows.columns)
    bars = store.market.daily_between(days[-6], asof, None)
    chg = {s: (g["close"].iloc[-1] / g["close"].iloc[0] - 1) * 100 for s, g in bars.groupby("symbol")}
    up = screen.run(store, {"rules": [{"type": "change", "days": 5, "min": 0}], "limit": 999, "exclude_kinds": False,
                            "sort": {"by": "change", "days": 5}}, asof)
    assert set(up["종목코드"]) == {s for s, v in chg.items() if v >= 0}
    assert list(up["5일 등락률%"]) == sorted(up["5일 등락률%"], reverse=True)
    assert screen.run(store, {"price": {"min": 10**9}, "exclude_kinds": False}, asof).empty
    with pytest.raises(ValueError):
        screen.run(store, {"rules": [{"type": "뉴스"}]}, asof)


def test_ai_parse_json_and_skill(tmp_path):
    from ktrade import ai
    assert ai.parse_json('설명\n```json\n{"rules": [], // 주석\n "limit": 5}\n```')["limit"] == 5
    (tmp_path / "references").mkdir()
    (tmp_path / "SKILL.md").write_text("# 차트 심리", encoding="utf-8")
    (tmp_path / "references" / "a.md").write_text("매물대", encoding="utf-8")
    text, used = ai.skill_text({"ai": {"skill_dir": str(tmp_path)}})
    assert used == ["SKILL.md", "references/a.md"] and "매물대" in text
    assert "개인 분석 스킬" in ai.stock_system({"ai": {"skill_dir": str(tmp_path)}})
    assert ai.stock_system({}) == ai.STOCK_SYSTEM
