"""1b: 조건식 실시간 스트림(가짜 웹소켓) + SignalBook 판단 + hts watch/signals."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from ktrade import cli
from ktrade.data import stream as S
from ktrade.data.store import Store
from ktrade.engine.signals import SignalBook, load_conditions

LIST = {"trnm": "CNSRLST", "return_code": 0, "data": [["6", "01_오전스켈핑"], ["15", "스켈핑 전략-1"], ["41", "강매집종목"]]}


class FakeRealtimeWS:
    """CNSRLST/CNSRREQ에 응답하고, push()로 REAL(02 편입·이탈 / 0B 체결)을 흘려보낸다."""

    def __init__(self, members=None, bad_seq=None):
        self.members, self.bad_seq = members or {}, bad_seq
        self.sent, self.trnm_cb, self.type_cb = [], {}, {}
        self._subscriptions: list[dict] = []
        self._ws = object()
        self.closed = False
        self.loop = None

    def on_trnm(self, t, fn):
        self.trnm_cb.setdefault(t, []).append(fn)

    def on(self, t, fn):
        self.type_cb.setdefault(t, []).append(fn)

    async def connect(self):
        self.loop = asyncio.get_running_loop()

    async def listen(self):
        while not self.closed:
            await asyncio.sleep(0.01)

    async def disconnect(self):
        self.closed = True

    async def subscribe(self, types, items, grp_no="1", refresh="1"):
        self._subscriptions.append({"trnm": "REG", "grp_no": grp_no, "data": [{"item": list(items), "type": [types]}]})

    async def _deliver(self, msg):
        await asyncio.sleep(0)
        if msg.get("trnm") == "REAL":
            for it in msg["data"]:
                for fn in self.type_cb.get(it["type"], []):
                    await fn(it)
        else:
            for fn in self.trnm_cb.get(msg["trnm"], []):
                await fn(msg)

    async def send(self, p):
        self.sent.append(p)
        if p["trnm"] == "CNSRLST":
            msg = LIST
        elif p["trnm"] == "CNSRREQ":
            seq = p["seq"]
            msg = ({"trnm": "CNSRREQ", "seq": seq, "return_code": 900, "return_msg": "한도 초과"} if seq == self.bad_seq else
                   {"trnm": "CNSRREQ", "seq": seq + "  ", "return_code": 0, "data": [{"jmcode": "A" + m} for m in self.members.get(seq, [])]})
        else:
            return
        self.loop.create_task(self._deliver(msg))

    def push(self, *items):
        asyncio.run_coroutine_threadsafe(self._deliver({"trnm": "REAL", "data": list(items)}), self.loop).result(2)


def cond_item(seq, sym, flag, t="091502"):
    return {"type": "02", "name": "조건검색", "item": sym, "values": {"841": seq, "9001": sym, "843": flag, "20": t, "907": "2"}}


def trade_item(sym, price):
    return {"type": "0B", "name": "체결", "item": sym, "values": {"10": f"+{price}", "20": "091503"}}


def drain(st, until_kinds=(), timeout=3.0):
    out, end = [], time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            ev = st.events.get(timeout=0.05)
        except Exception:
            if until_kinds and any(e.kind in until_kinds for e in out):
                break
            continue
        out.append(ev)
    return out


def test_stream_registers_emits_and_restores_on_reconnect():
    ws = FakeRealtimeWS(members={"6": ["005930", "000660"]}, bad_seq="41")
    st = S.ConditionStream({}, ["오전스켈핑", "강매집", "없는조건"], ws_factory=lambda c: ws, reply_timeout=2).start()
    try:
        evs = drain(st, until_kinds=("status",), timeout=3)
        kinds = [(e.kind, e.cond, e.symbol) for e in evs]
        assert ("registered", "01_오전스켈핑", "") in kinds
        assert ("init", "01_오전스켈핑", "005930") in kinds and ("init", "01_오전스켈핑", "000660") in kinds
        assert any(e.kind == "error" and e.cond == "강매집종목" and "900" in e.detail for e in evs)   # 등록 실패는 그 조건식만
        assert any(e.kind == "error" and e.cond == "없는조건" for e in evs)
        assert st.registered == {"01_오전스켈핑": "6"}
        reqs = [p for p in ws.sent if p["trnm"] == "CNSRREQ"]
        assert ws.sent[0] == {"trnm": "CNSRLST"} and all(p["search_type"] == "1" for p in reqs)
        # 재연결 때 라이브러리가 다시 보낼 목록 맨 앞에 CNSRLST → 등록 성공한 CNSRREQ
        assert ws._subscriptions[0] == {"trnm": "CNSRLST"} and ws._subscriptions[1]["seq"] == "6"
        ws.push(cond_item("6", "035720", "I"), trade_item("035720", 51200))
        ws.push(cond_item("6", "035720", "D", "091530"))
        evs = drain(st, timeout=0.5)
        assert [(e.kind, e.symbol, e.time) for e in evs] == [("insert", "035720", "091502"), ("remove", "035720", "091530")]
        assert evs[0].cond == "01_오전스켈핑" and st.price("035720") == 51200
        st.watch_price("035720")
        time.sleep(0.2)
        assert any(s.get("trnm") == "REG" and "035720" in s["data"][0]["item"] for s in ws._subscriptions)
        ws._ws = object()                                  # 라이브러리가 다시 붙었다고 가정
        assert any("다시 붙음" in e.detail for e in drain(st, timeout=1.5))
    finally:
        st.stop()
    assert ws.closed


def test_stream_restarts_when_listener_dies():
    made = []

    class Dying(FakeRealtimeWS):
        async def listen(self):
            if len(made) == 1:
                raise RuntimeError("끊김")
            await super().listen()

    def factory(c):
        made.append(Dying(members={"6": []}))
        return made[-1]
    st = S.ConditionStream({}, ["6"], ws_factory=factory, reply_timeout=2, retry_delays=(0.1,)).start()
    try:
        time.sleep(1.0)
        assert len(made) >= 2 and st.restarts >= 1 and st.registered == {"01_오전스켈핑": "6"}
    finally:
        st.stop()


class Ev:
    def __init__(self, kind, symbol, cond="01_오전스켈핑", seq="6", time="091502", detail=""):
        self.kind, self.symbol, self.cond, self.seq, self.time, self.detail = kind, symbol, cond, seq, time, detail


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "m.db")


T0 = datetime(2026, 10, 26, 9, 15, 0)


def test_immediate_buy_and_skips(store):
    prices = {"A": 5000.0, "B": 500.0, "C": 7000.0}
    conds = load_conditions({"hts_conditions": [{"name": "오전스켈핑", "buy_window": ["09:00", "10:30"], "price_range": [1000, 50000],
                                                 "max_buys_per_day": 1}]})
    book = SignalBook(store, conds, prices.get)
    assert book.handle(Ev("init", "A"), T0)[0]["action"] == "INIT"
    assert book.handle(Ev("insert", "A"), T0)[0]["action"] == "WOULD_BUY"          # 이름 일부로 적은 설정이 실제 이름에 붙음
    assert "가격 범위" in book.handle(Ev("insert", "B"), T0)[0]["reason"]
    assert "하루 매수 한도" in book.handle(Ev("insert", "C"), T0)[0]["reason"]
    assert "재매수" in book.handle(Ev("insert", "A"), T0)[0]["reason"]
    assert "시간대 밖" in book.handle(Ev("insert", "C"), T0.replace(hour=11))[0]["reason"]
    df = store.signals("2026-10-26")
    assert list(df["action"]) == ["INIT", "WOULD_BUY", "SKIP", "SKIP", "SKIP", "SKIP"] and df.iloc[1]["price"] == 5000


def test_confirm_mode(store):
    prices = {"A": 5000.0, "B": 5000.0, "C": 5000.0}
    conds = load_conditions({}, ["스켈핑 전략-1"])
    conds[0].update(confirm_sec=30, chase_pct=1.0)
    book = SignalBook(store, conds, prices.get)
    c = "스켈핑 전략-1"
    for s in "ABC":
        assert book.handle(Ev("insert", s, cond=c), T0)[0]["action"] == "WAIT"
    book.handle(Ev("remove", "A", cond=c), T0 + timedelta(seconds=10))
    prices["B"] = 4990.0
    prices["C"] = 5040.0
    assert book.tick(T0 + timedelta(seconds=10))[0]["action"] == "CONFIRM_FAIL"     # A 헛편입
    out = {d["symbol"]: d for d in book.tick(T0 + timedelta(seconds=31))}
    assert out["B"]["action"] == "CONFIRM_FAIL" and "편입가" in out["B"]["reason"]
    assert out["C"]["action"] == "WOULD_BUY" and out["C"]["price"] == 5040
    prices["D"] = 5000.0
    book.handle(Ev("insert", "D", cond=c), T0)
    prices["D"] = 5100.0
    assert "추격 한도" in book.tick(T0 + timedelta(seconds=31))[0]["reason"]


def test_price_wait_and_exit_on_remove(store):
    prices: dict[str, float] = {}
    conds = load_conditions({"hts_conditions": [{"name": "01_오전스켈핑", "exit_on_remove": True}]})
    book = SignalBook(store, conds, prices.get, price_wait_sec=5)
    assert book.handle(Ev("insert", "A"), T0)[0]["reason"] == "현재가 받는 중"
    assert book.tick(T0 + timedelta(seconds=6))[0]["reason"] == "현재가를 받지 못함"
    prices["B"] = 3000.0
    book.handle(Ev("insert", "B"), T0)
    acts = [d["action"] for d in book.handle(Ev("remove", "B"), T0 + timedelta(minutes=5))]
    assert acts == ["REMOVE", "WOULD_SELL"]


def test_cli_watch_and_signals(cfg, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    cfg["_secrets"] = {"app_key": "k", "app_secret": "s"}
    cfg["hts_conditions"] = [{"name": "01_오전스켈핑", "buy_window": ["00:00", "23:59"]}]   # 시험 시각과 무관하게

    class FakeStream:
        def __init__(self, c, names):
            import queue
            self.events, self.restarts, self.names = queue.Queue(), 0, names
            for ev in [S.StreamEvent(kind="registered", cond="01_오전스켈핑", seq="6", detail="등록 시점 편입 1종목"),
                       S.StreamEvent(kind="init", cond="01_오전스켈핑", seq="6", symbol="000660"),
                       S.StreamEvent(kind="insert", cond="01_오전스켈핑", seq="6", symbol="005930", time="091502")]:
                self.events.put(ev)

        def start(self):
            return self

        def stop(self):
            pass

        def price(self, s):
            return 71200.0

        def watch_price(self, s):
            pass
    monkeypatch.setattr(S, "ConditionStream", FakeStream)
    calls = {"n": 0}

    def fake_sleep(sec):
        calls["n"] += 1
        raise KeyboardInterrupt
    monkeypatch.setattr(time, "sleep", fake_sleep)
    cli.main(["hts", "watch", "01_오전스켈핑", "--until", "23:59"])
    out = capsys.readouterr().out
    assert "주문 없음" in out and "(등록) [01_오전스켈핑]" in out and "005930 71,200원 → 매수 신호" in out
    assert "등록 시 편입 1" in out and "매수 신호 1" in out
    cli.main(["hts", "signals"])
    assert "매수 신호 1" in capsys.readouterr().out  # signals 요약


def test_stream_caps_realtime_registrations():
    ws = FakeRealtimeWS(members={})
    st = S.ConditionStream({}, ["6", "15", "41"], ws_factory=lambda c: ws, reply_timeout=2, max_realtime=2, register_gap=0).start()
    try:
        evs = drain(st, until_kinds=("status",), timeout=3)
    finally:
        st.stop()
    assert st.registered == {"01_오전스켈핑": "6", "스켈핑 전략-1": "15"}
    assert any(e.kind == "error" and "2개까지" in e.detail and e.cond == "강매집종목" for e in evs)
    assert [p["seq"] for p in ws.sent if p["trnm"] == "CNSRREQ"] == ["6", "15"]       # 한도 넘는 건 보내지도 않음


def test_stream_retries_silent_registration():
    class Flaky(FakeRealtimeWS):
        def __init__(self):
            super().__init__(members={"15": ["005930"]})
            self.dropped = 0

        async def send(self, p):
            if p["trnm"] == "CNSRREQ" and p["seq"] == "15" and self.dropped == 0:
                self.dropped += 1                      # 첫 등록 응답을 삼킴 (모의서버 무응답 재현)
                self.sent.append(p)
                return
            await super().send(p)
    ws = Flaky()
    st = S.ConditionStream({}, ["6", "15"], ws_factory=lambda c: ws, reply_timeout=0.3, register_gap=0).start()
    try:
        evs = drain(st, until_kinds=("status",), timeout=6)
        while not any(e.kind == "status" and "수신 중" in e.detail for e in evs):
            evs += drain(st, timeout=1)
    finally:
        st.stop()
    assert st.registered == {"01_오전스켈핑": "6", "스켈핑 전략-1": "15"}
    assert any("다시 시도" in e.detail for e in evs) and any("재시도 1번째에 됨" in e.detail for e in evs)
