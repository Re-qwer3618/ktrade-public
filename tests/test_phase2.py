"""2단계: 분할매수·분할매도(백테스트·성적표·실행 공용 PositionSim) + 계좌 한도(하루 손익·시간 청산·긴급 정지) + 미체결 취소."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ktrade.broker.base import Broker
from ktrade.broker.paper import PaperBroker
from ktrade.data.store import Store
from ktrade.engine import exits as X
from ktrade.engine import forward, live
from ktrade.engine.executor import HALT_KEY, PNL_KEY, Executor, book, set_halt, tranche_qty
from ktrade.engine.risk import Risk

T0 = datetime(2026, 10, 27, 9, 10, 0)
SCALED = {"stop": {"type": "pct", "value": 8}, "take_profit": {"type": "pct", "value": 6}, "stop_check": "intraday",
          "max_hold_days": 1, "scale_in": [{"pct": 50}, {"pct": 50, "drop_pct": 4}],
          "scale_out": [{"pct": 50, "gain_pct": 3}]}


def _plan(sym="A", ex=None):
    return {"plan_id": f"p-{sym}", "symbol": sym, "name": sym, "max_hold_days": 1,
            "entry": {"after": "09:00", "min_open": 0, "min_price": 0, "max_price": 1e9},
            "exits": X.resolve(ex or SCALED)}


def _risk(**kw):
    return Risk(starting_cash=10_000_000, max_position_pct=10, max_concurrent_positions=5, slippage_pct=0.5,
                max_new_orders_per_day=10, **kw)


# ---------------- 설정 검사 ----------------
def test_scale_validation():
    X.resolve(SCALED)
    with pytest.raises(ValueError, match="합은 100"):
        X.resolve({"scale_in": [{"pct": 50}, {"pct": 30, "drop_pct": 3}]})
    with pytest.raises(ValueError, match="첫 차수 0"):
        X.resolve({"scale_in": [{"pct": 50, "drop_pct": 2}, {"pct": 50, "drop_pct": 3}]})
    with pytest.raises(ValueError, match="분할매수 전에 손절"):
        X.resolve({"stop": {"type": "pct", "value": 3}, "scale_in": [{"pct": 50}, {"pct": 50, "drop_pct": 4}]})
    with pytest.raises(ValueError, match="점점 커져야"):
        X.resolve({"scale_out": [{"pct": 50, "gain_pct": 5}, {"pct": 50, "gain_pct": 3}]})
    assert tranche_qty(101, SCALED["scale_in"]) == [50, 51] and tranche_qty(1, SCALED["scale_in"]) == [1]


# ---------------- 공용 시뮬레이터 ----------------
def test_position_sim_scale_in_moves_average_and_levels():
    ex = X.resolve(SCALED)
    pos = X.PositionSim(ex, 10000)
    assert not pos.step(10000, 10050, 9550, 9600)          # -4.5% → 2차 매수 9,600 체결, 평균 9,800
    assert pos.filled == 1.0 and pos.avg == pytest.approx(9800)
    assert not pos.step(9600, 10000, 9600, 9990)            # 평균 +3% = 10,094 아직
    assert not pos.step(10000, 10100, 10000, 10050)         # 10,094 도달 → 절반 익절
    assert pos.held == pytest.approx(0.5)
    assert pos.step(10050, 10400, 10050, 10350)             # 평균 +6% = 10,388 → 나머지 목표가
    r = pos.result(cost_pct=0)
    assert r["reason"] == "분할매도 +3% → 이익실현" and r["fills"] == 2
    assert r["ret_pct"] == pytest.approx(((10094 + 10388) / 2 / 9800 - 1) * 100, abs=0.01)


def test_position_sim_stop_from_average_and_conservative_order():
    pos = X.PositionSim(X.resolve(SCALED), 10000)
    assert pos.step(10000, 10500, 8900, 9000)               # 2차 9,600 체결 → 평균 9,800, 손절 9,016 같은 봉에서 → 손절
    r = pos.result(cost_pct=0)
    assert r["reason"] == "손실제한" and r["exit"] == pytest.approx(9016)


def test_forward_simulate_uses_scaling():
    bars = pd.DataFrame([{"ts": f"2026-10-27 09:{m:02d}:00", "open": o, "high": h, "low": low, "close": c}
                         for m, (o, h, low, c) in enumerate([(10000, 10010, 9990, 10000), (10000, 10310, 10000, 10300),
                                                             (10300, 10620, 10300, 10600)], start=10)])
    sim = forward.simulate(bars, "2026-10-27 09:09:30", 10000, X.resolve(SCALED), cost_pct=0)
    assert sim["reason"] == "분할매도 +3% → 이익실현" and sim["exit_price"] == pytest.approx(10450)


# ---------------- 실행 (paper: 즉시 체결) ----------------
def test_executor_scale_in_then_scale_out_then_target(tmp_path):
    store, broker = Store(tmp_path / "m.db"), PaperBroker(tmp_path / "p.json", 10_000_000)
    px = {"A": 10000.0}
    ex = Executor(store, broker, _risk(), lambda s: {"price": px[s], "open": px[s]})
    ex.add_plan("hts:x", "hts", _plan())
    ex.check("A", T0)
    t = store.active_plan_trade("A")
    assert broker.position("A") == 50 and t["target_qty"] == 100 and t["status"] == "open"
    px["A"] = 9600.0                                                       # -4% → 2차
    ex.check("A", T0 + timedelta(minutes=1))
    t = store.active_plan_trade("A")
    assert broker.position("A") == 100 and t["entry_price"] == pytest.approx(9800)
    ex.check("A", T0 + timedelta(minutes=2))                               # 같은 가격 — 3차는 없음
    assert broker.position("A") == 100
    px["A"] = 10100.0                                                      # 평균 +3.06% → 절반 익절
    ex.check("A", T0 + timedelta(minutes=3))
    assert broker.position("A") == 50 and store.active_plan_trade("A")["status"] == "open"
    ex.check("A", T0 + timedelta(minutes=4))                               # 같은 차수는 한 번만
    assert broker.position("A") == 50
    px["A"] = 10400.0                                                      # 평균 +6% = 10,388 → 목표가
    ex.check("A", T0 + timedelta(minutes=5))
    r = store.query("SELECT * FROM plan_trades").iloc[0]
    assert r["status"] == "closed" and broker.position("A") == 0
    assert r["exit_reason"].startswith("분할매도 +3% → 이익실현") and r["exit_price"] == pytest.approx(10250)
    fills, sells, pend = book(r.to_dict())
    assert [f["qty"] for f in fills] == [50, 50] and [s["qty"] for s in sells] == [50, 50] and pend is None


def test_order_amount_and_buy_window(tmp_path):
    store, broker = Store(tmp_path / "m.db"), PaperBroker(tmp_path / "p.json", 10_000_000)
    ex = Executor(store, broker, _risk(order_amount=500_000, buy_window=("09:00", "09:05")),
                  lambda s: {"price": 10000.0, "open": 10000.0})
    ex.add_plan("s", "1", _plan(ex={"max_hold_days": 1}))
    ex.check("A", T0)                                                      # 09:10 — 시간대 밖
    assert broker.position("A") == 0
    assert "매수 시간대" in store.query("SELECT detail FROM decisions").iloc[-1]["detail"]
    ex.risk.buy_window = None
    ex.check("A", T0)
    assert broker.position("A") == 50                                      # 50만 원 / 1만 원


# ---------------- 계좌 단위 ----------------
def test_daily_loss_limit_halts_and_flattens(tmp_path):
    store, broker = Store(tmp_path / "m.db"), PaperBroker(tmp_path / "p.json", 10_000_000)
    px = {"A": 10000.0, "B": 10000.0}
    ex = Executor(store, broker, _risk(daily_loss_limit=30_000), lambda s: {"price": px[s], "open": px[s]})
    ex.add_plan("s", "1", _plan("A", {"stop": {"type": "none"}, "max_hold_days": 1}))
    ex.check("A", T0)
    px["A"] = 9600.0                                                       # 100주 × -400 = -40,000원
    pnl = ex.guard(T0 + timedelta(minutes=1))
    assert pnl["total"] == -40_000 and store.get_setting(HALT_KEY)["flatten"]
    assert store.get_setting(PNL_KEY)["unrealized"] == -40_000
    ex.check("A", T0 + timedelta(minutes=1))
    assert broker.position("A") == 0 and "하루 손실 한도" in store.query("SELECT exit_reason FROM plan_trades").iloc[0]["exit_reason"]
    ex.add_plan("s", "1", _plan("B", {"max_hold_days": 1}))
    ex.check("B", T0 + timedelta(minutes=2))
    assert broker.position("B") == 0                                       # 정지 중 — 신규 매수 없음
    assert ex.guard(T0 + timedelta(minutes=2))["realized"] == -40_000
    ex.check("B", T0 + timedelta(days=1))                                  # 다음 날엔 저절로 풀림
    assert broker.position("B") == 100


def test_emergency_stop_and_flatten_at(tmp_path):
    store, broker = Store(tmp_path / "m.db"), PaperBroker(tmp_path / "p.json", 10_000_000)
    ex = Executor(store, broker, _risk(flatten_at="15:00"), lambda s: {"price": 10000.0, "open": 10000.0})
    for s in ("A", "B"):
        ex.add_plan("s", "1", _plan(s, {"max_hold_days": 5}))
        ex.check(s, T0)
    ex.check("A", T0.replace(hour=15, minute=1))
    assert broker.position("A") == 0 and broker.position("B") == 100
    assert "시간 청산" in store.query("SELECT exit_reason FROM plan_trades WHERE symbol='A'").iloc[0]["exit_reason"]
    set_halt(store, T0.strftime("%Y-%m-%d"), "긴급 정지 (화면)")
    ex.check("B", T0 + timedelta(minutes=1))
    assert broker.position("B") == 0


# ---------------- 미체결 (주문 접수만 되는 브로커) ----------------
class SlowBroker(Broker):
    """접수만 하고, 테스트가 fill()을 불러야 체결된다."""

    def __init__(self):
        self.pos, self.cancelled, self.n = {}, [], 0

    def cash(self):
        return 10_000_000

    def total_deposit(self):
        return 10_000_000

    def positions(self):
        return {s: {"qty": q, "avg_price": None} for s, q in self.pos.items() if q > 0}

    def place_order(self, symbol, side, qty, price, slippage_pct=0.5):
        self.n += 1
        return {"status": "SUBMITTED", "order_no": f"{self.n:07d}"}

    def cancel(self, symbol, order_no):
        self.cancelled.append(order_no)
        return {"status": "CANCELLED"}

    def fill(self, symbol, qty):
        self.pos[symbol] = self.pos.get(symbol, 0) + qty


def test_order_timeout_cancels_rest_and_keeps_partial(tmp_path):
    store, broker = Store(tmp_path / "m.db"), SlowBroker()
    ex = Executor(store, broker, _risk(order_timeout_sec=30), lambda s: {"price": 10000.0, "open": 10000.0})
    ex.add_plan("s", "1", _plan(ex={"max_hold_days": 1}))
    ex.check("A", T0)
    t = store.active_plan_trade("A")
    assert t["status"] == "submitted" and book(t)[2]["order_no"] == "0000001"
    broker.fill("A", 40)
    ex.check("A", T0 + timedelta(seconds=10))                              # 아직 30초 전 — 기다림
    assert store.active_plan_trade("A")["status"] == "submitted"
    ex.check("A", T0 + timedelta(seconds=31))                              # 100주 중 40주 → 나머지 취소
    t = store.active_plan_trade("A")
    assert broker.cancelled == ["0000001"] and t["status"] == "open" and t["qty"] == 40 and book(t)[2] is None


def test_unfilled_buy_without_any_fill(tmp_path):
    store, broker = Store(tmp_path / "m.db"), SlowBroker()
    ex = Executor(store, broker, _risk(order_timeout_sec=20), lambda s: {"price": 10000.0, "open": 10000.0})
    ex.add_plan("s", "1", _plan(ex={"max_hold_days": 1}))
    ex.check("A", T0)
    ex.check("A", T0 + timedelta(seconds=25))
    assert store.query("SELECT status FROM plan_trades").iloc[0]["status"] == "unfilled"


# ---------------- 기존 DB·프로세스 ----------------
def test_old_db_gets_new_columns(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE plan_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, symbol TEXT NOT NULL,
                 name TEXT, strategy TEXT, version TEXT, status TEXT NOT NULL, plan TEXT, qty INTEGER, buy_submitted_at TEXT,
                 entry_date TEXT, entry_price REAL, exit_submitted_at TEXT, exit_date TEXT, exit_price REAL, exit_reason TEXT,
                 note TEXT DEFAULT '', updated_at TEXT NOT NULL)""")
    c.execute("INSERT INTO plan_trades (plan_id, symbol, status, plan, qty, entry_price, entry_date, updated_at) "
              "VALUES ('p', 'A', 'open', '{}', 10, 5000, '2026-10-26', '2026-10-26')")
    c.commit()
    c.close()
    store = Store(p)
    t = store.active_plan_trade("A")
    fills, sells, pend = book(t)
    assert fills == [{"step": 0, "qty": 10, "price": 5000.0, "at": "2026-10-26"}] and sells == [] and pend is None


def test_stop_request_ends_live_loop(cfg, tmp_path):
    from ktrade.config import paths
    store = Store(paths(cfg)["db"])
    t = [datetime(2026, 10, 27, 20, 0, 0)]

    def tick(_):
        t[0] += timedelta(seconds=60)
        if t[0].minute == 2:
            live.request_stop(store, "run", t[0])
    live.run(cfg, quote_source=object(), broker=PaperBroker(tmp_path / "p.json", 1e7), now_fn=lambda: t[0], sleep=tick)


def test_daily_backtest_with_scaling(market, cfg):
    from ktrade.engine import backtest, loader
    store, days = market
    info, cond, rules = loader.enabled(cfg)[0]
    r = X.resolve(rules, {"scale_in": [{"pct": 50}, {"pct": 50, "drop_pct": 3}], "scale_out": [{"pct": 50, "gain_pct": 2}]})
    res = backtest.run(store, info, cond, r, days[21], days[-1])
    t = res["table"]
    assert len(t) >= 1 and t["reason"].str.contains("분할매도").any()
