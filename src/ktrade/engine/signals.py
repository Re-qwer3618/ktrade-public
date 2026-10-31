"""영웅문 조건식 신호 → 매수 판단 (SignalBook). mode: watch면 신호 일지(condition_signals)에만 남기고, trade면 장중 계획을 만들어
지금 Executor(engine/executor.py)에 넘긴다 — 매수·손절·익절·보유기간 처리는 파이썬 매매조건과 같은 코드.

판단 순서 (편입 I 하나마다, 거절하면 그 사유를 그대로 기록):
  매수 시간대(buy_window) → 현재가 확인 → 가격 범위(price_range) → 오늘 이 조건식 매수 한도(max_buys_per_day)
  → 같은 날 같은 종목 재매수(reentry) → [confirm_sec > 0이면 N초 대기: 그 사이 이탈하면 '헛편입', 편입가 아래면 탈락]
  → 추격 한도(편입가 대비 chase_pct% 넘게 오르면 안 삼) → WOULD_BUY (1c부터 trade 모드면 실제 매수)
action 값: INIT(등록 시점에 이미 편입 — 신호 아님) · WAIT(확인 대기) · WOULD_BUY(관찰) · BUY(매수 주문) · SKIP · CONFIRM_FAIL
  · REMOVE · WOULD_SELL / SELL(이탈 매도 설정)
trade는 allow_trade=True일 때만 실제로 주문한다(호출하는 쪽이 모의/paper인지, 실전 기준을 넘었는지 판단) — 아니면 관찰로 동작.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ktrade.data.store import Store
from ktrade.engine import exits as X

# 영웅문 조건식(장중 단타) 기본 청산 — 엔진 기본값(스윙: 10일·장 후반 손절)과 다르다. 조건식 설정 exits로 덮어쓴다.
HTS_EXITS = {
    "stop": {"type": "pct", "value": 2.0},
    "take_profit": {"type": "pct", "value": 3.0},
    "trailing": {"activate_pct": 2.0, "trail_pct": 1.0},
    "max_hold_days": 1,                 # 1 = 당일 15:15 이후 청산
    "stop_check": "intraday",
}

DEFAULT_COND = {
    "mode": "watch",                # watch(신호 일지만) | trade(1c부터 매수)
    "buy_window": ["09:00", "15:20"],
    "price_range": [1000, 1_000_000],
    "chase_pct": 1.0,
    "confirm_sec": 0,
    "max_buys_per_day": 3,
    "reentry": False,
    "exit_on_remove": False,
    "exits": {},
}
BUY_ACTIONS = ("WOULD_BUY", "BUY")


def cond_exits(c: dict) -> dict:
    ex = c.get("exits") or {}
    for k in ("atr", "low"):
        if (ex.get("stop") or {}).get("type") == k:
            raise ValueError(f"조건식 '{c['name']}': 장중 조건식은 손절 type pct만 (atr·low는 일봉 계획용)")
    return X.resolve(HTS_EXITS, ex)


def load_conditions(cfg: dict, names: list[str] | None = None) -> list[dict]:
    """config.yaml hts_conditions(+기본값). names를 주면 그 조건식만 — config에 없는 이름은 기본값(관찰)으로."""
    conf = {}
    for item in cfg.get("hts_conditions") or []:
        item = {"name": item} if isinstance(item, str) else dict(item)
        if not item.get("name"):
            continue
        merged = copy.deepcopy(DEFAULT_COND)
        merged.update(item)
        conf.setdefault(str(item["name"]), merged)  # 이름이 겹치면 앞(새로 저장한) 것 — 예전 저장 버그로 뒤에 묵은 항목이 남은 파일 대비
    if names:
        return [conf.get(n) or {**copy.deepcopy(DEFAULT_COND), "name": n} for n in names]
    return list(conf.values())


MAX_CUSTOM_SYMBOLS = 20         # 직접 작성만: 조건마다 감시 종목 상한 (종목마다 분봉 조회 — 키움 조회 한도)


def is_custom(c: dict) -> bool:
    """검색 방식 '직접 작성만' — 키움 조건검색 대신 정해 둔 감시 종목을 직접 작성 조건으로 훑는다."""
    return c.get("source") == "custom"


@dataclass
class _Pending:
    cond: dict
    symbol: str
    seq: str
    insert_price: float | None
    insert_at: datetime
    event_time: str
    removed: bool = False
    prechecked: tuple | None = None     # 직접 작성만: scan에서 이미 돌린 (이유, 추격 상한)


class SignalBook:
    """이벤트(insert/remove/init) → 판단·기록. price_fn(symbol) → 현재가 또는 None. 조건식 이름은 stream이 찾은 실제 이름."""

    def __init__(self, store: Store, conds: list[dict], price_fn: Callable[[str], float | None],
                 price_wait_sec: float = 5.0, executor=None, allow_trade: bool = False, no_trade_reason: str = "",
                 confirm=None):
        self.store, self.price_fn, self.price_wait = store, price_fn, timedelta(seconds=price_wait_sec)
        self.confirm = confirm              # engine.intraday.Runner — 조건식의 '직접 작성 조건'(confirm: 장중 조건 id)
        self.conds = {c["name"]: c for c in conds}
        self.pending: dict[tuple[str, str], _Pending] = {}
        self.executor, self.allow_trade, self.no_trade_reason = executor, allow_trade, no_trade_reason
        self.custom = [c for c in conds if is_custom(c)]   # 검색 방식 '직접 작성만' — 키움 조건검색 없이 감시 종목을 직접 훑음
        self.last_scan: dict[str, datetime] = {}
        self.scan_state: dict[tuple[str, str], tuple[str, bool, str]] = {}   # (조건, 종목) → (시각, 통과, 이유) — 불통과는 기록하지 않음
        for c in conds:
            if c.get("mode") == "trade":
                cond_exits(c)                  # 설정 오류는 시작할 때 바로

    def trading(self, c: dict) -> bool:
        return c.get("mode") == "trade" and self.allow_trade and self.executor is not None

    def cond_for(self, name: str) -> dict:
        """stream이 넘긴 실제 조건식 이름 → 설정. 설정은 이름 일부로 적었을 수 있어 포함 관계로도 찾는다."""
        if name in self.conds:
            return self.conds[name]
        for k, c in self.conds.items():
            if k in name or name in k:
                self.conds[name] = c
                return c
        c = {**copy.deepcopy(DEFAULT_COND), "name": name}
        self.conds[name] = c
        return c

    def _record(self, cond_name: str, seq: str, symbol: str, event: str, action: str, reason: str = "",
                price: float | None = None, event_time: str = "", now: datetime | None = None, plan_id: str | None = None) -> dict:
        c = self.cond_for(cond_name)
        mode = "trade" if self.trading(c) else ("watch" if c["mode"] != "trade" else "trade→watch")
        self.store.add_signal(cond_name, seq, symbol, event, action, reason, price, event_time, mode, plan_id,
                              ts=(now or datetime.now()).isoformat(timespec="seconds"))
        return {"cond": cond_name, "symbol": symbol, "event": event, "action": action, "reason": reason, "price": price,
                "plan_id": plan_id}

    def _bought_today(self, cond_name: str, today: str) -> set[str]:
        df = self.store.signals(today, cond_name)
        return set(df[df["action"].isin(BUY_ACTIONS)]["symbol"]) if not df.empty else set()

    # ---- 직접 작성만: 감시 종목을 장중 조건으로 훑기 ----
    def scan(self, now: datetime) -> list[dict]:
        """'직접 작성만' 조건마다 poll_sec(기본 30초)에 한 번, 감시 종목을 직접 작성 조건(키움 실시간 분봉·현재가)으로 확인.
        통과하면 편입과 똑같이 가격 범위·하루 한도·추격 상한을 거쳐 매수(또는 관찰 신호). 불통과는 신호 일지에 남기지 않는다."""
        out = []
        for c in self.custom:
            name = c["name"]
            last = self.last_scan.get(name)
            if last is not None and (now - last).total_seconds() < float(c.get("poll_sec") or 30):
                continue
            self.last_scan[name] = now
            lo, hi = c["buy_window"]
            if not (lo <= now.strftime("%H:%M") <= hi):
                continue
            cf = c.get("confirm")
            cf = cf if isinstance(cf, dict) else {"id": str(cf or "")}
            if self.confirm is None or not cf["id"]:
                continue
            bought = self._bought_today(name, now.strftime("%Y-%m-%d"))
            for sym in c.get("symbols") or []:
                if len(bought) >= int(c["max_buys_per_day"]):
                    break
                if sym in bought and not c.get("reentry"):
                    continue
                price = self.price_fn(sym)
                if not price:
                    continue
                try:
                    ok, why, cap = self.confirm.check(cf["id"], cf.get("params"), sym, now, price, event="watch")
                except Exception as exc:          # 한 종목 오류로 엔진이 서지 않게
                    ok, why, cap = False, f"오류: {exc}", None
                self.scan_state[(name, sym)] = (now.isoformat(timespec="seconds"), ok, why)
                if not ok:
                    continue
                p = _Pending(c, sym, "custom", price, now, now.strftime("%H%M%S"), prechecked=(why, cap))
                d = self._decide(p, price, now)
                out.append(d)
                if d["action"] in BUY_ACTIONS:
                    bought.add(sym)
        return out

    # ---- 이벤트 ----
    def handle(self, ev, now: datetime) -> list[dict]:
        if ev.kind == "init":
            return [self._record(ev.cond, ev.seq, ev.symbol, "INIT", "INIT", "등록 시점에 이미 편입 — 매수 신호 아님" +
                                 (f" ({ev.detail})" if ev.detail else ""), now=now)]
        if ev.kind == "remove":
            return self._on_remove(ev, now)
        if ev.kind == "insert":
            return self._on_insert(ev, now)
        return []

    def _on_insert(self, ev, now: datetime) -> list[dict]:
        c = self.cond_for(ev.cond)
        hhmm = now.strftime("%H:%M")
        lo, hi = c["buy_window"]
        if not (lo <= hhmm <= hi):
            return [self._record(ev.cond, ev.seq, ev.symbol, "I", "SKIP", f"매수 시간대 밖 ({lo}~{hi})", self.price_fn(ev.symbol),
                                 ev.time, now)]
        key = (ev.cond, ev.symbol)
        if key in self.pending:
            return []                                   # 이미 확인 대기 중
        p = _Pending(c, ev.symbol, ev.seq, self.price_fn(ev.symbol), now, ev.time)
        confirm = float(c.get("confirm_sec") or 0)
        if p.insert_price is None or confirm > 0:
            self.pending[key] = p
            why = f"편입 후 {confirm:.0f}초 확인 대기" if confirm > 0 else "현재가 받는 중"
            return [self._record(ev.cond, ev.seq, ev.symbol, "I", "WAIT", why, p.insert_price, ev.time, now)]
        return [self._decide(p, p.insert_price, now)]

    def _on_remove(self, ev, now: datetime) -> list[dict]:
        out = []
        p = self.pending.get((ev.cond, ev.symbol))
        if p is not None:
            p.removed = True
        c = self.cond_for(ev.cond)
        price = self.price_fn(ev.symbol)
        out.append(self._record(ev.cond, ev.seq, ev.symbol, "D", "REMOVE", "조건 이탈", price, ev.time, now))
        if c.get("exit_on_remove") and ev.symbol in self._bought_today(ev.cond, now.strftime("%Y-%m-%d")):
            if self.trading(c):
                trade = self.store.active_plan_trade(ev.symbol)
                if trade is not None and trade["strategy"] == f"hts:{ev.cond}":
                    ok = self.executor.force_exit(ev.symbol, f"조건 이탈 매도 ({ev.cond})", now)
                    out.append(self._record(ev.cond, ev.seq, ev.symbol, "D", "SELL" if ok else "SKIP",
                                            "조건 이탈 매도" if ok else "이탈 매도 못 함 (보유·시세 확인)", price, ev.time, now,
                                            trade["plan_id"]))
            else:
                out.append(self._record(ev.cond, ev.seq, ev.symbol, "D", "WOULD_SELL", "이탈 매도 설정 (관찰이라 주문 없음)",
                                        price, ev.time, now))
        return out

    def tick(self, now: datetime) -> list[dict]:
        """확인 대기 종목 처리 (매 루프)."""
        out = []
        for key, p in list(self.pending.items()):
            confirm = float(p.cond.get("confirm_sec") or 0)
            if p.removed:
                del self.pending[key]
                out.append(self._record(p.cond["name"], p.seq, p.symbol, "I", "CONFIRM_FAIL",
                                        f"확인 {confirm:.0f}초 안에 이탈 (헛편입)", self.price_fn(p.symbol), p.event_time, now))
                continue
            if p.insert_price is None:
                p.insert_price = self.price_fn(p.symbol)
                if p.insert_price is None:
                    if now - p.insert_at >= self.price_wait:
                        del self.pending[key]
                        out.append(self._record(p.cond["name"], p.seq, p.symbol, "I", "SKIP", "현재가를 받지 못함",
                                                None, p.event_time, now))
                    continue
            if (now - p.insert_at).total_seconds() < confirm:
                continue
            price = self.price_fn(p.symbol) or p.insert_price
            del self.pending[key]
            if confirm > 0 and price < p.insert_price:
                out.append(self._record(p.cond["name"], p.seq, p.symbol, "I", "CONFIRM_FAIL",
                                        f"확인 {confirm:.0f}초 뒤 편입가 {p.insert_price:,.0f} 아래 ({price:,.0f})", price, p.event_time, now))
                continue
            out.append(self._decide(p, price, now))
        return out

    def _decide(self, p: _Pending, price: float, now: datetime) -> dict:
        c, name = p.cond, p.cond["name"]
        lo, hi = c["price_range"]
        if not (lo <= price <= hi):
            return self._record(name, p.seq, p.symbol, "I", "SKIP", f"가격 범위 밖 ({lo:,}~{hi:,}원)", price, p.event_time, now)
        bought = self._bought_today(name, now.strftime("%Y-%m-%d"))
        if p.symbol in bought and not c.get("reentry"):
            return self._record(name, p.seq, p.symbol, "I", "SKIP", "오늘 이미 매수 신호 — 재매수 안 함(reentry)", price,
                                p.event_time, now)
        if len(bought) >= int(c["max_buys_per_day"]):
            return self._record(name, p.seq, p.symbol, "I", "SKIP", f"이 조건식 하루 매수 한도 {c['max_buys_per_day']}종목",
                                price, p.event_time, now)
        limit = p.insert_price * (1 + float(c["chase_pct"]) / 100)
        if price > limit:
            return self._record(name, p.seq, p.symbol, "I", "SKIP", f"추격 한도 — 편입가 {p.insert_price:,.0f} 대비 "
                                f"+{(price / p.insert_price - 1) * 100:.1f}% (> {c['chase_pct']}%)", price, p.event_time, now)
        how = "즉시" if not float(c.get("confirm_sec") or 0) else f"{float(c['confirm_sec']):.0f}초 확인 후"
        cf = c.get("confirm")
        if p.prechecked is not None:        # 직접 작성만 — scan에서 이미 통과
            why, cap = p.prechecked
            cf = cf if isinstance(cf, dict) else {"id": str(cf)}
            if cap:
                limit = min(limit, cap)
            how = f"감시 종목, 직접 작성 조건 '{cf['id']}' 통과({why})"
        elif cf:                            # 편입 뒤 내 파이썬 조건으로 한 번 더 (키움 실시간 분봉·일봉)
            cf = cf if isinstance(cf, dict) else {"id": str(cf)}
            if self.confirm is None:
                return self._record(name, p.seq, p.symbol, "I", "SKIP", f"직접 작성 조건 '{cf['id']}' 실행기 없음", price,
                                    p.event_time, now)
            ok, why, cap = self.confirm.check(cf["id"], cf.get("params"), p.symbol, now, price, event="insert",
                                              insert_price=p.insert_price)
            if not ok:
                return self._record(name, p.seq, p.symbol, "I", "SKIP", f"직접 작성 조건 '{cf['id']}' 불통과 — {why}", price,
                                    p.event_time, now)
            if cap:
                limit = min(limit, cap)
            how += f", 직접 작성 조건 '{cf['id']}' 통과({why})"
        if self.trading(c):
            return self._buy(p, price, limit, how, now)
        why = "관찰 모드라 주문 없음" if c["mode"] != "trade" else f"매매 설정이지만 관찰로 동작 — {self.no_trade_reason or '주문 허용 안 됨'}"
        return self._record(name, p.seq, p.symbol, "I", "WOULD_BUY", f"매수 신호 ({how}, {why})", price, p.event_time, now)

    def _buy(self, p: _Pending, price: float, limit: float, how: str, now: datetime) -> dict:
        """장중 계획 하나 → Executor. 매수 구간 = 지금가 -0.5% ~ 편입가 + chase_pct%. 엔진 리스크 한도(종목당 금액·동시 보유·하루 주문)도 거기서."""
        c, name = p.cond, p.cond["name"]
        ex = cond_exits(c)
        pid = f"hts-{name}-{now:%Y%m%d}-{p.symbol}-{now:%H%M%S}"
        plan = {"plan_id": pid, "symbol": p.symbol, "name": "", "rank": 1, "stop": None, "target": None,
                "entry": {"min_open": 0.01, "min_price": round(price * 0.995, 2), "max_price": round(limit, 2), "after": now.strftime("%H:%M")},
                "exits": ex, "max_hold_days": int(ex["max_hold_days"]),
                "basis": (f"직접 작성 조건 '{name}' 신호 {p.event_time} ({how})" if is_custom(c)
                          else f"영웅문 조건식 '{name}' 편입 {p.event_time} ({how})"), "meta": {"insert_price": p.insert_price}}
        self.executor.add_plan(f"hts:{name}", "hts", plan)
        self.executor.check(p.symbol, now)
        t = self.store.active_plan_trade(p.symbol)
        if t is not None and t["plan_id"] == pid:
            return self._record(name, p.seq, p.symbol, "I", "BUY", f"매수 {int(t['qty'])}주 ({how}) — {X.describe(ex)}",
                                price, p.event_time, now, pid)
        last = self.store.query("SELECT detail FROM decisions WHERE detail LIKE ? ORDER BY id DESC LIMIT 1", (pid + "%",))
        why = last["detail"].iloc[0].replace(pid, "").strip() if not last.empty else "매수 안 됨 (보유 중이거나 주문 실패)"
        return self._record(name, p.seq, p.symbol, "I", "SKIP", why, price, p.event_time, now, pid)
