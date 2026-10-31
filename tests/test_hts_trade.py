"""1c·1d: 조건식 매매(paper) + 성적표(1분봉 가상 결과) + 실전 차단."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from ktrade import cli
from ktrade.broker.paper import PaperBroker
from ktrade.data.store import Store
from ktrade.engine import forward
from ktrade.engine.executor import Executor
from ktrade.engine.risk import Risk
from ktrade.engine.signals import HTS_EXITS, SignalBook, cond_exits, load_conditions
from ktrade.engine import exits as X

T0 = datetime(2026, 10, 26, 9, 15, 0)
NAME = "단타전략-2"


class Ev:
    def __init__(self, kind, symbol, cond=NAME, seq="14", time="091500"):
        self.kind, self.symbol, self.cond, self.seq, self.time, self.detail = kind, symbol, cond, seq, time, ""


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "m.db")
    broker = PaperBroker(tmp_path / "paper.json", 10_000_000)
    prices = {"A": 10000.0, "B": 5000.0}
    risk = Risk(starting_cash=10_000_000, max_position_pct=10, max_concurrent_positions=5, slippage_pct=0.5, max_new_orders_per_day=10)
    ex = Executor(store, broker, risk, lambda s: {"price": prices.get(s), "open": prices.get(s)} if prices.get(s) else {})
    conds = load_conditions({"hts_conditions": [{"name": NAME, "mode": "trade", "exit_on_remove": True,
                                                 "exits": {"stop": {"type": "pct", "value": 2}, "take_profit": {"type": "pct", "value": 3}}}]})
    book = SignalBook(store, conds, prices.get, executor=ex, allow_trade=True)
    return store, broker, prices, ex, book


def test_trade_buy_stop_and_remove_exit(setup):
    store, broker, prices, ex, book = setup
    d = book.handle(Ev("insert", "A"), T0)[0]
    assert d["action"] == "BUY" and "100주" in d["reason"]                    # 1,000만 × 10% / 10,000원
    t = store.active_plan_trade("A")
    assert t["status"] == "open" and t["strategy"] == f"hts:{NAME}" and broker.position("A") == 100
    prices["A"] = 9790.0                                                       # -2.1% → 손절 (장중 즉시)
    ex.check("A", T0 + timedelta(minutes=3))
    closed = store.query("SELECT * FROM plan_trades WHERE symbol='A'").iloc[0]
    assert closed["status"] == "closed" and "손실제한" in closed["exit_reason"] and broker.position("A") == 0
    assert book.handle(Ev("insert", "B"), T0 + timedelta(minutes=5))[0]["action"] == "BUY"
    acts = [x["action"] for x in book.handle(Ev("remove", "B"), T0 + timedelta(minutes=6))]
    assert acts == ["REMOVE", "SELL"] and broker.position("B") == 0
    assert "조건 이탈 매도" in store.query("SELECT exit_reason FROM plan_trades WHERE symbol='B'").iloc[0]["exit_reason"]


def test_trade_mode_without_permission_is_watch(setup):
    store, broker, prices, ex, _ = setup
    book = SignalBook(store, load_conditions({"hts_conditions": [{"name": NAME, "mode": "trade"}]}), prices.get,
                      executor=ex, allow_trade=False, no_trade_reason="실전 막힘")
    d = book.handle(Ev("insert", "A"), T0)[0]
    assert d["action"] == "WOULD_BUY" and "실전 막힘" in d["reason"] and broker.position("A") == 0
    assert store.signals("2026-10-26").iloc[-1]["mode"] == "trade→watch"


def test_risk_limit_reason_recorded(setup):
    store, broker, prices, ex, book = setup
    ex.risk.max_concurrent_positions = 0
    d = book.handle(Ev("insert", "A"), T0)[0]
    assert d["action"] == "SKIP" and "동시 보유 한도" in d["reason"]


def test_hts_exits_defaults_and_validation():
    ex = cond_exits({"name": "x"})
    assert ex["max_hold_days"] == 1 and ex["stop_check"] == "intraday" and ex["stop"]["value"] == HTS_EXITS["stop"]["value"]
    with pytest.raises(ValueError, match="pct만"):
        cond_exits({"name": "x", "exits": {"stop": {"type": "atr", "value": 2}}})


def bars(day, start, closes, spread=0.002):
    t0 = datetime.fromisoformat(f"{day} {start}")
    return pd.DataFrame([{"ts": (t0 + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), "open": c, "high": c * (1 + spread),
                          "low": c * (1 - spread), "close": c, "volume": 100} for i, c in enumerate(closes)])


def test_simulate_rules():
    ex = X.resolve(HTS_EXITS, {"trailing": {"activate_pct": None, "trail_pct": None}})
    b = bars("2026-10-26", "09:16:00", [10000, 10100, 10280, 10310])           # +3% 목표가에 봉 안에서 닿음
    r = forward.simulate(b, "2026-10-26 09:15:20", 10000, ex, cost_pct=0.0)
    assert r["reason"] == "이익실현" and r["exit_price"] == pytest.approx(10300) and r["ret_pct"] == pytest.approx(3.0)
    gap = bars("2026-10-26", "09:16:00", [10000, 10350])                       # 목표가 위로 바로 시작한 봉 → 그 봉 시가
    assert forward.simulate(gap, "2026-10-26 09:15:20", 10000, ex, 0.0)["exit_price"] == pytest.approx(10350)
    b = bars("2026-10-26", "09:16:00", [10000, 9790])
    assert forward.simulate(b, "2026-10-26 09:15:20", 10000, ex, 0.0)["reason"] == "손실제한"
    wide = pd.DataFrame([{"ts": "2026-10-26 09:16:00", "open": 10000, "high": 10400, "low": 9700, "close": 10000, "volume": 1}])
    assert forward.simulate(wide, "2026-10-26 09:15:20", 10000, ex, 0.0)["reason"] == "손실제한"     # 한 봉에 둘 다 → 손절
    late = bars("2026-10-26", "15:13:00", [10000, 10010, 10020, 10030])
    assert forward.simulate(late, "2026-10-26 15:12:10", 10000, ex, 0.0)["reason"] == "당일 청산"
    trail = X.resolve(HTS_EXITS, {"take_profit": {"type": "none"}, "trailing": {"activate_pct": 2, "trail_pct": 1}})
    up = bars("2026-10-26", "09:16:00", [10100, 10250, 10180, 10050], spread=0.0)
    assert forward.simulate(up, "2026-10-26 09:15:20", 10000, trail, 0.0)["reason"] == "트레일링스탑"
    assert forward.simulate(bars("2026-10-26", "09:00:00", [1]), "2026-10-26 09:15:20", 10000, ex) is None


def test_report_and_cli(cfg, capsys, monkeypatch, tmp_path):
    from ktrade.config import paths
    cfg["hts_conditions"] = [{"name": NAME, "mode": "trade"}]
    store = Store(paths(cfg)["db"])
    store.add_signal(NAME, "14", "A", "I", "BUY", "매수", 10000.0, "091500", "trade", "p1", ts="2026-10-26T09:15:20")
    store.add_signal(NAME, "14", "B", "I", "SKIP", "가격 범위 밖", 5000.0, "091600", "trade", ts="2026-10-26T09:16:10")
    store.add_signal(NAME, "14", "C", "I", "SKIP", "x", 7000.0, "091700", "trade", ts="2026-10-26T09:17:10")   # 분봉 없음
    store.upsert_minute("A", bars("2026-10-26", "09:16:00", [10000, 10350]))
    store.upsert_minute("B", bars("2026-10-26", "09:17:00", [5000, 4890]))
    tid = store.add_plan_trade({"plan_id": "p1", "symbol": "A"}, f"hts:{NAME}", "hts", 100, 10000, "2026-10-26T09:15:20")
    store.update_plan_trade(tid, status="closed", entry_price=10000, exit_price=10300, exit_date="2026-10-26", exit_reason="목표가")
    rep = forward.report(store, cfg, ["2026-10-26"])
    r = rep["conds"][NAME]
    assert r["all"]["n"] == 2 and r["buy"]["n"] == 1 and r["actual"]["n"] == 1 and rep["missing_bars"] == 1
    assert r["buy"]["avg"] == pytest.approx(3.5 - 0.25)                       # 10,350 시가로 갭 → 목표가 위 체결
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    cli.main(["hts", "report", "--date", "2026-10-26"])
    out = capsys.readouterr().out
    assert "■ 단타전략-2 (trade)" in out and "매수 신호(내 설정 통과): 1건 · 승률 100%" in out and "못 매긴 신호 1건" in out


def test_trade_allowed_only_paper_or_mock():
    assert cli._hts_trade_allowed({"broker": "paper", "kiwoom": {"is_mock": False}})[0]
    assert cli._hts_trade_allowed({"broker": "kiwoom", "kiwoom": {"is_mock": True}})[0]
    ok, why = cli._hts_trade_allowed({"broker": "kiwoom", "kiwoom": {"is_mock": False}})
    assert not ok and "실전" in why


def test_episodes_group_flapping():
    import pandas as pd
    rows, i = [], 0

    def add(ts, sym, ev, action="SKIP", price=1000.0):
        nonlocal i
        i += 1
        rows.append({"id": i, "ts": f"2026-10-26T{ts}", "date": "2026-10-26", "symbol": sym, "event": ev, "action": action, "price": price})
    add("09:00:00", "A", "I", "BUY", 1000)
    for k in range(5):                                 # 몇 초 간격 깜빡임 → 같은 묶음
        add(f"09:00:{10 + k * 2:02d}", "A", "D", "REMOVE")
        add(f"09:00:{11 + k * 2:02d}", "A", "I", "SKIP", 1010)
    add("09:01:00", "A", "D", "REMOVE")
    add("09:20:00", "A", "I", "SKIP", 1100)            # 19분 뒤 재편입 → 새 묶음
    add("09:05:00", "B", "I", "WAIT", 500)
    add("09:05:30", "B", "I", "WOULD_BUY", 505)        # 확인 대기 → 판단: 같은 묶음
    from ktrade.engine.forward import episodes
    eps = episodes(pd.DataFrame(rows), 10)
    assert sorted(zip(eps.symbol, eps.price, eps.n_inserts)) == [("A", 1000, 6), ("A", 1100, 1), ("B", 500, 2)]


def _plan(sym, pid, price):
    return {"plan_id": pid, "symbol": sym, "name": sym, "rank": 1, "stop": None, "target": None, "max_hold_days": 5,
            "entry": {"min_open": 0.01, "min_price": price * 0.9, "max_price": price * 1.1, "after": "09:00"},
            "exits": X.resolve({"stop": {"type": "pct", "value": 2}, "stop_check": "intraday"})}


def test_only_owner_manages_until_owner_engine_is_gone(tmp_path):
    """A: 계획으로 산 종목은 ktrade run(plan)만 매도 판단 — hts 엔진은 run이 꺼졌을 때(heartbeat 오래됨)만 넘겨받는다."""
    store = Store(tmp_path / "m.db")
    broker = PaperBroker(tmp_path / "paper.json", 10_000_000)
    px = {"A": 10000.0}
    q = lambda s: {"price": px[s], "open": px[s]}  # noqa: E731
    plan_ex = Executor(store, broker, Risk(10_000_000, max_position_pct=10), q, role="plan")
    hts_ex = Executor(store, broker, Risk(10_000_000, max_position_pct=10), q, role="hts")
    plan_ex.set_plans([("brk", "v1", _plan("A", "p-A", 10000))])
    plan_ex.check("A", T0)
    assert store.active_plan_trade("A")["status"] == "open"
    px["A"] = 9700.0                                                          # -3% → 손절 조건 (장중 즉시)
    store.set_setting("live.heartbeat", {"ts": T0.isoformat()})               # run 살아 있음
    hts_ex.check("A", T0 + timedelta(seconds=30))
    assert store.active_plan_trade("A") is not None and broker.position("A") == 100   # hts는 손대지 않음
    hts_ex.check("A", T0 + timedelta(minutes=10))                             # run heartbeat 10분 전 → 꺼진 것으로 보고 넘겨받음
    assert store.active_plan_trade("A") is None and broker.position("A") == 0


def test_priority_plan_first_blocks_plan_symbols_and_reserves_slots(tmp_path):
    """B: 계획 우선 — 오늘 계획 종목은 조건식이 안 사고, 계획 몫 자리를 남긴다. split·hts 모드도."""
    store = Store(tmp_path / "m.db")
    broker = PaperBroker(tmp_path / "paper.json", 10_000_000)
    px = {s: 10000.0 for s in "ABCD"}
    q = lambda s: {"price": px[s], "open": px[s]}  # noqa: E731
    info = lambda today: ({"A"}, 2)  # noqa: E731                              오늘 계획 2건, 그중 A
    risk = Risk(10_000_000, max_position_pct=5, max_concurrent_positions=3, priority={"mode": "plan", "plan_reserve": 2})
    ex = Executor(store, broker, risk, q, role="hts", plan_info=info)
    for s in "AB":
        ex.add_plan("hts:c", "hts", {**_plan(s, f"h-{s}", 10000), "symbol": s})
        ex.check(s, T0)
    assert store.active_plan_trade("A") is None                              # 계획 종목
    assert store.active_plan_trade("B") is not None                          # 3자리 중 계획 몫 2 → 조건식은 1자리
    ex.add_plan("hts:c", "hts", _plan("C", "h-C", 10000))
    ex.check("C", T0)
    assert store.active_plan_trade("C") is None
    why = store.query("SELECT detail FROM decisions WHERE detail LIKE 'h-C%'")["detail"].tolist()
    assert any("계획 몫 2자리" in w for w in why) and any("계획에 있는 종목" in w
                                                         for w in store.query("SELECT detail FROM decisions")["detail"])
    ex.risk.priority = {"mode": "hts"}                                       # 선착순 — 막지 않음
    ex.check("C", T0 + timedelta(seconds=1))
    assert store.active_plan_trade("C") is not None
    ex.risk.priority = {"mode": "split", "split": {"hts": {"positions": 2}}}
    ex.add_plan("hts:c", "hts", _plan("D", "h-D", 10000))
    ex.check("D", T0 + timedelta(seconds=2))
    assert store.active_plan_trade("D") is None and any("나눠 쓰기" in w for w in
                                                         store.query("SELECT detail FROM decisions WHERE detail LIKE 'h-D%'")["detail"])
