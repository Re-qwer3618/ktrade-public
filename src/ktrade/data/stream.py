"""장중 실시간 — 영웅문 조건식 편입·이탈(CNSRREQ 실시간, REAL type 02) + 체결가(0B)를 웹소켓 하나로 받는다.

키움 웹소켓 실시간 세션은 계정당 하나 → 조건검색과 체결가를 **한 연결**에 같이 싣고, 같은 키로 실시간을 쓰는 다른 프로그램과
동시에 켜지 않는다. 연결은 별도 스레드(asyncio)가 소유하고, 이벤트는 스레드 안전 큐로 넘긴다(StreamEvent).

실시간 조건검색 동시 등록은 10개까지 (2026-10-26 모의서버 실측: 0.5초 간격이면 10개 모두 등록, 11·12번째는 재시도해도 오류 코드 없이
무응답). 간격 없이 몰아 보내면 한도 안에서도 응답이 빠질 때가 있어(6번째 무응답) 0.5초 간격 + 무응답 두 번 더 시도.
MAX_REALTIME 넘는 조건식은 보내지 않고 알린다.

순서(키움 문서): LOGIN → CNSRLST(목록, 반드시 먼저) → 조건식마다 CNSRREQ(search_type=1). 응답 data = 등록 시점에 이미 걸린 종목(INIT).
재연결: kiwoom_client는 끊기면 다시 붙고 '구독 목록'(_subscriptions)을 다시 보낸다. 조건검색 등록은 거기 없으므로 CNSRLST·CNSRREQ도
그 목록에 넣어 함께 복원되게 한다. 리스너가 아예 끝나면(재연결 포기 등) 이 모듈이 처음부터 다시 연결한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ktrade.data import hts

log = logging.getLogger("ktrade.stream")
MAX_REALTIME = 10


@dataclass
class StreamEvent:
    kind: str                 # insert | remove | init | registered | error | status
    cond: str = ""            # 조건식 이름
    seq: str = ""
    symbol: str = ""
    time: str = ""            # 키움이 준 체결시간 HHMMSS (편입·이탈)
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def _code(raw: Any) -> str:
    s = str(raw or "").strip()
    return s[1:] if len(s) == 7 and s[0].isalpha() else s


class ConditionStream:
    """start() → 백그라운드 스레드. events: queue.Queue[StreamEvent]. price(symbol)은 0B 체결가(받은 뒤 갱신 시각과 함께)."""

    def __init__(self, cfg: dict, names: list[str], ws_factory: Callable[[dict], Any] | None = None,
                 reply_timeout: float = 15.0, price_group: str = "5", max_price_items: int = 100,
                 retry_delays: tuple[float, ...] = (5, 15, 30, 60), max_realtime: int = MAX_REALTIME,
                 register_gap: float = 0.5, register_attempts: int = 3):
        self.cfg, self.names = cfg, list(dict.fromkeys(names))
        self.max_realtime, self.register_gap, self.register_attempts = max_realtime, register_gap, register_attempts
        self.ws_factory = ws_factory or hts._default_factory
        self.reply_timeout, self.price_group, self.max_price_items = reply_timeout, price_group, max_price_items
        self.retry_delays = retry_delays
        self.events: queue.Queue[StreamEvent] = queue.Queue()
        self.registered: dict[str, str] = {}          # 이름 → seq
        self.restarts = 0
        self._prices: dict[str, tuple[float, float]] = {}
        self._price_symbols: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._waiting: dict[str, asyncio.Future] = {}
        self._seq_name: dict[str, str] = {}

    # ---- 바깥(메인 스레드)에서 쓰는 것 ----
    def start(self) -> "ConditionStream":
        self._thread = threading.Thread(target=self._run, name="ktrade-condition-stream", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._loop and self._ws:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._ws.disconnect(), self._loop).result(timeout)
        if self._thread:
            self._thread.join(timeout)

    def price(self, symbol: str, max_age_sec: float = 30.0) -> float | None:
        with self._lock:
            p = self._prices.get(symbol)
        return p[0] if p and time.monotonic() - p[1] <= max_age_sec else None

    def watch_price(self, symbol: str) -> None:
        """이 종목 체결가(0B)를 받기 시작 (같은 연결에 추가 등록). 한도를 넘으면 건너뜀 — 그때는 REST 시세로 대신한다."""
        with self._lock:
            if symbol in self._price_symbols or len(self._price_symbols) >= self.max_price_items:
                return
            self._price_symbols.append(symbol)
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._subscribe_prices([symbol]), self._loop)

    # ---- 스레드 안 ----
    def _emit(self, **kw) -> None:
        self.events.put(StreamEvent(**kw))

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                asyncio.run(self._session())
                attempt = 0
            except Exception as exc:  # 연결·등록 실패 — 잠시 뒤 처음부터
                self._emit(kind="error", detail=f"실시간 연결 오류: {exc}")
                log.warning(f"조건검색 실시간 연결 오류: {exc}")
            if self._stop.is_set():
                break
            delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
            attempt += 1
            self.restarts += 1
            self._emit(kind="status", detail=f"{delay:.0f}초 뒤 다시 연결 (재시작 {self.restarts}번째)")
            self._stop.wait(delay)

    async def _session(self) -> None:
        self._loop = asyncio.get_running_loop()
        ws = self.ws_factory(self.cfg)
        self._ws, self._waiting = ws, {}
        ws.on_trnm("CNSRLST", self._on_reply("CNSRLST"))
        ws.on_trnm("CNSRREQ", self._on_cnsrreq)
        ws.on("02", self._on_condition)
        ws.on("0B", self._on_trade)
        await ws.connect()
        listener = asyncio.create_task(ws.listen())
        try:
            conds = hts.parse_list(await self._ask("CNSRLST", {"trnm": "CNSRLST"}, "조건식 목록(CNSRLST)"))
            restore: list[dict] = [{"trnm": "CNSRLST"}]
            todo: list[hts.HtsCondition] = []
            for name in self.names:                     # 이름 → seq (seq는 영웅문에서 조건식을 고치면 바뀐다 — 2026-10-26 실측)
                try:
                    todo.append(hts.find(conds, name))
                except hts.HtsError as exc:
                    self._emit(kind="error", cond=name, detail=str(exc))
            # 등록 응답이 가끔 오지 않는다(02:36 실측: 6번째가 무응답, 다른 때는 등록됨) → 간격을 두고, 무응답은 두 번 더 시도
            for attempt in range(self.register_attempts):
                failed: list[hts.HtsCondition] = []
                for c in todo:
                    if len(self.registered) >= self.max_realtime:
                        self._emit(kind="error", cond=c.name, seq=c.seq,
                                   detail=f"실시간 조건검색은 {self.max_realtime}개까지 — 등록하지 않음")
                        continue
                    payload = {"trnm": "CNSRREQ", "seq": c.seq, "search_type": "1", "stex_tp": "K"}
                    self._seq_name[c.seq] = c.name
                    try:
                        msg = await self._ask(f"CNSRREQ:{c.seq}", payload, f"실시간 등록 '{c.name}'")
                    except hts.HtsError as exc:
                        if "응답 없음" in str(exc) and attempt + 1 < self.register_attempts:
                            failed.append(c)
                            self._emit(kind="status", cond=c.name, seq=c.seq, detail=f"등록 응답 없음 — 뒤에 다시 시도 ({attempt + 1}번째)")
                        else:
                            self._emit(kind="error", cond=c.name, seq=c.seq, detail=str(exc))
                        continue
                    finally:
                        await asyncio.sleep(self.register_gap)
                    self.registered[c.name] = c.seq
                    restore.append(payload)
                    members = [_code(d.get("jmcode") or d.get("9001")) for d in msg.get("data") or []]
                    self._emit(kind="registered", cond=c.name, seq=c.seq,
                               detail=f"등록 시점 편입 {len(members)}종목" + (f" (재시도 {attempt}번째에 됨)" if attempt else ""))
                    for sym in dict.fromkeys(m for m in members if m):
                        self._emit(kind="init", cond=c.name, seq=c.seq, symbol=sym)
                if not failed:
                    break
                todo = failed
                await asyncio.sleep(2)
            if not self.registered:
                raise hts.HtsError("실시간 등록된 조건식이 없습니다")
            subs = getattr(ws, "_subscriptions", None)
            if isinstance(subs, list):            # 라이브러리 재연결 때 조건검색도 다시 보내도록 (목록 → 등록 순서)
                subs[:0] = restore
            if self._price_symbols:
                await self._subscribe_prices(list(self._price_symbols))
            self._emit(kind="status", detail="실시간 수신 중: " + ", ".join(f"{n}({s})" for n, s in self.registered.items()))
            last_conn = id(getattr(ws, "_ws", None))
            while not self._stop.is_set() and not listener.done():
                await asyncio.sleep(1)
                cur = id(getattr(ws, "_ws", None))
                if cur != last_conn and getattr(ws, "_ws", None) is not None:
                    self._emit(kind="status", detail="연결이 끊겼다가 다시 붙음 — 조건식·체결가 등록을 다시 보냈음")
                    last_conn = cur
            if listener.done() and not self._stop.is_set():
                exc = listener.exception() if not listener.cancelled() else None
                raise hts.HtsError(f"수신이 끝남{f': {exc}' if exc else ''}")
        finally:
            with contextlib.suppress(Exception):
                await ws.disconnect()
            listener.cancel()
            with contextlib.suppress(BaseException):
                await listener
            self._ws = None

    async def _subscribe_prices(self, symbols: list[str]) -> None:
        if self._ws and symbols:
            with contextlib.suppress(Exception):
                await self._ws.subscribe("0B", symbols, grp_no=self.price_group, refresh="1")

    async def _ask(self, key: str, payload: dict, what: str) -> dict:
        fut = asyncio.get_running_loop().create_future()
        self._waiting[key] = fut
        await self._ws.send(payload)
        try:
            msg = await asyncio.wait_for(fut, self.reply_timeout)
        except asyncio.TimeoutError:
            raise hts.HtsError(f"{what}: 응답 없음 ({self.reply_timeout:.0f}초) — 같은 키로 실시간을 쓰는 다른 프로그램이 있는지") from None
        hts._check(msg, what)
        return msg

    def _on_reply(self, key: str):
        async def handler(msg: dict) -> None:
            fut = self._waiting.pop(key, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        return handler

    async def _on_cnsrreq(self, msg: dict) -> None:
        seq = str(msg.get("seq") or "").strip()
        fut = self._waiting.pop(f"CNSRREQ:{seq}", None)
        if fut is not None and not fut.done():
            fut.set_result(msg)
        elif seq in self._seq_name:                 # 재연결 뒤 다시 등록된 응답 (기다리던 게 아님)
            name = self._seq_name[seq]
            if msg.get("return_code") not in (None, 0, "0"):
                self._emit(kind="error", cond=name, seq=seq, detail=f"재등록 실패: {msg.get('return_msg')}")
            else:
                n = len(msg.get("data") or [])
                self._emit(kind="registered", cond=name, seq=seq, detail=f"재등록 — 지금 편입 {n}종목")
                for d in msg.get("data") or []:
                    sym = _code(d.get("jmcode") or d.get("9001"))
                    if sym:
                        self._emit(kind="init", cond=name, seq=seq, symbol=sym, detail="재등록")

    async def _on_condition(self, item: dict) -> None:
        v = item.get("values") or {}
        seq = str(v.get("841") or "").strip()
        sym = _code(v.get("9001") or item.get("item"))
        flag = str(v.get("843") or "").strip().upper()
        if not sym or flag not in ("I", "D"):
            return
        self._emit(kind="insert" if flag == "I" else "remove", cond=self._seq_name.get(seq, seq), seq=seq, symbol=sym,
                   time=str(v.get("20") or ""))

    async def _on_trade(self, item: dict) -> None:
        v = item.get("values") or {}
        sym = _code(item.get("item") or v.get("9001"))
        try:
            price = abs(float(str(v.get("10") or "").replace(",", "")))
        except ValueError:
            return
        if sym and price:
            with self._lock:
                self._prices[sym] = (price, time.monotonic())
