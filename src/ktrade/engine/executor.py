"""계획 실행 — 매수는 매매조건이 정한 Entry대로, 매도는 엔진 청산 규칙(engine/exits.py)대로. plan_trades에 남긴다.

상태: submitted(첫 매수 주문) → open(보유) → closing(전량 매도 주문) → closed | unfilled | rejected.
한 종목에 주문은 한 번에 하나만 걸어 둔다(plan_trades.pending). 체결은 계좌 보유 수량의 변화로 확인한다
(키움 모의투자는 체결 조회가 들쭉날쭉해서). paper 브로커는 즉시 체결.
  fills = 매수 차수 기록 [{step, qty, price, at}], sells = 매도 기록 [{qty, price, at, reason, step}] (JSON)
  entry_price = 평균 매수가, qty = 지금 보유 수량, target_qty = 계획 수량(분할매수 전체)
매도 판단 (매 확인 주기, 평균 매수가 기준 — 백테스트 exits.PositionSim과 같은 규칙):
  계좌 정지(긴급 정지·하루 손익 한도) / 시간 일괄청산(risk.flatten_at) → 손절 · 추적 손절 · 사용자 장중 손절 % → 분할익절
  → 목표가 · 보유기간 만료(15:15 이후) → (안 팔았으면) 다음 분할매수 차수
미체결: risk.order_timeout_sec가 지나면 남은 수량을 취소하고 체결된 만큼만 기록한다(없으면 다음 거래일까지 기다림).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime

import numpy as np

from ktrade.broker.base import Broker
from ktrade.data.store import Store
from ktrade.engine import exits as X
from ktrade.engine.risk import Risk

log = logging.getLogger("ktrade.executor")
HALT_KEY = "trading.halt"        # {date, reason, flatten, at} — 그날만 유효
PNL_KEY = "trading.pnl"          # {date, realized, unrealized, total, at} — 화면 표시용


def _hhmm(now: datetime) -> str:
    return now.strftime("%H:%M")


def _ts(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _blank(v) -> bool:
    return v is None or v == "" or (isinstance(v, float) and v != v)


def _load(v, default):
    if _blank(v):
        return default
    return json.loads(v) if isinstance(v, str) else v


def _date(trade: dict) -> str | None:
    d = trade.get("entry_date")
    return None if _blank(d) else str(d)


def book(trade: dict) -> tuple[list, list, dict | None]:
    """(fills, sells, pending). 0.6.0 이전 기록(차수 없음)은 수량·매수가(·매도가)로 한 차수를 만든다."""
    fills, sells = _load(trade.get("fills"), None), _load(trade.get("sells"), None)
    old = fills is None
    if old:
        fills = []
        if trade.get("status") in ("open", "closing", "closed") and trade.get("qty") and not _blank(trade.get("entry_price")):
            at = trade.get("buy_submitted_at")
            fills = [{"step": 0, "qty": int(trade["qty"]), "price": float(trade["entry_price"]),
                      "at": str(at) if not _blank(at) else (_date(trade) or "")}]
    if sells is None:
        sells = []
        if old and fills and trade.get("status") == "closed" and not _blank(trade.get("exit_price")):
            at = trade.get("exit_submitted_at")
            sells = [{"qty": fills[0]["qty"], "price": float(trade["exit_price"]), "step": None,
                      "at": str(at) if not _blank(at) else str(trade.get("exit_date") or ""),
                      "reason": str(trade.get("exit_reason") or "")}]
    return fills, sells, _load(trade.get("pending"), None)


def held_qty(fills: list, sells: list) -> int:
    return int(sum(f["qty"] for f in fills) - sum(s["qty"] for s in sells))


def avg_price(rows: list) -> float | None:
    q = sum(r["qty"] for r in rows)
    return sum(r["qty"] * r["price"] for r in rows) / q if q else None


def tranche_qty(total: int, scale_in: list) -> list[int]:
    """계획 수량을 분할매수 차수로 나눈다 — 마지막 차수가 나머지. 첫 차수가 0주면 한 번에 다 산다."""
    if not scale_in:
        return [total]
    qs = [int(math.floor(total * float(s["pct"]) / 100)) for s in scale_in[:-1]]
    qs.append(total - sum(qs))
    return qs if qs[0] > 0 else [total]


def halt_state(store: Store, today: str) -> dict | None:
    h = store.get_setting(HALT_KEY)
    return h if h and h.get("date") == today else None


def set_halt(store: Store, today: str, reason: str, flatten: bool = True, now: datetime | None = None) -> None:
    """오늘 신규 매수를 멈춘다. flatten이면 보유도 모두 판다 (긴급 정지·하루 손익 한도). 다음 거래일엔 저절로 풀린다."""
    store.set_setting(HALT_KEY, {"date": today, "reason": reason, "flatten": flatten, "at": _ts(now or datetime.now())})


def clear_halt(store: Store) -> None:
    store.set_setting(HALT_KEY, {})


def daily_pnl(store: Store, quote_fn, today: str) -> dict:
    """오늘 손익(원, 비용 제외) — 오늘 매도한 실현 손익 + 보유 중 평가 손익. ktrade가 낸 주문(plan_trades)만 센다."""
    r = store.query("SELECT * FROM plan_trades WHERE status IN ('submitted', 'open', 'closing') "
                    "OR substr(updated_at, 1, 10) = ?", (today,))
    realized = unrealized = 0.0
    for t in r.to_dict("records"):
        fills, sells, _ = book(t)
        avg = avg_price(fills)
        if not avg:
            continue
        realized += sum((s["price"] - avg) * s["qty"] for s in sells if str(s.get("at", ""))[:10] == today)
        held = held_qty(fills, sells)
        if held > 0 and t["status"] in ("open", "closing"):
            px = (quote_fn(t["symbol"]) or {}).get("price")
            if px:
                unrealized += (float(px) - avg) * held
    return {"date": today, "realized": round(realized), "unrealized": round(unrealized),
            "total": round(realized + unrealized)}


class Executor:
    def __init__(self, store: Store, broker: Broker, risk: Risk, quote_fn):
        self.store, self.broker, self.risk, self.quote_fn = store, broker, risk, quote_fn
        self.plans: dict[str, tuple[str, str, dict]] = {}   # 종목 → (조건, 버전, 계획)
        self._noted: set[str] = set()

    def set_plans(self, items: list[tuple[str, str, dict]]) -> None:
        self.plans = {p["symbol"]: (sid, ver, p) for sid, ver, p in items}
        self._noted.clear()

    def add_plan(self, sid: str, ver: str, plan: dict) -> None:
        """장중에 생긴 계획 하나를 더한다 (영웅문 조건식 편입 → 장중 계획). 같은 종목의 앞 계획은 바뀐다."""
        self.plans[plan["symbol"]] = (sid, ver, plan)

    def symbols(self) -> list[str]:
        return sorted(set(self.plans) | set(self.store.active_plan_symbols()))

    def _note_once(self, key: str, sid: str, symbol: str, signal: str, detail: str, now: datetime) -> None:
        if key not in self._noted:
            self._noted.add(key)
            self.store.log_decision(sid, symbol, signal, "NONE", detail, _ts(now))

    # ---------------- 계좌 단위 ----------------
    def guard(self, now: datetime) -> dict:
        """매 주기 처음에 한 번 — 오늘 손익을 남기고, 하루 수익 목표·손실 한도에 닿으면 정지(+전량 매도)를 건다."""
        today = now.strftime("%Y-%m-%d")
        pnl = daily_pnl(self.store, self.quote_fn, today)
        self.store.set_setting(PNL_KEY, {**pnl, "at": _ts(now)})
        if halt_state(self.store, today):
            return pnl
        r = self.risk
        if r.daily_loss_limit and pnl["total"] <= -abs(float(r.daily_loss_limit)):
            why = f"하루 손실 한도 {-abs(float(r.daily_loss_limit)):,.0f}원 도달 (오늘 {pnl['total']:,.0f}원)"
        elif r.daily_profit_target and pnl["total"] >= float(r.daily_profit_target):
            why = f"하루 수익 목표 {float(r.daily_profit_target):,.0f}원 달성 (오늘 {pnl['total']:,.0f}원)"
        else:
            return pnl
        set_halt(self.store, today, why, flatten=True, now=now)
        self.store.log_decision("engine", "", "HALT", "HALT", why + " — 신규 매수 중지, 보유 전량 매도", _ts(now))
        log.warning(why + " — 신규 매수 중지, 보유 전량 매도")
        return pnl

    # ---------------- 종목 단위 ----------------
    def force_exit(self, symbol: str, reason: str, now: datetime) -> bool:
        """보유 중인 이 종목을 지금 판다 (조건식 이탈 매도 등). 주문을 냈으면 True."""
        trade = self.store.active_plan_trade(symbol)
        if trade is None or trade["status"] != "open":
            return False
        fills, sells, pend = book(trade)
        if pend:
            return False
        price = (self.quote_fn(symbol) or {}).get("price")
        qty = min(held_qty(fills, sells), self.broker.position(symbol))
        if not price or qty <= 0:
            return False
        return self._sell(trade, symbol, qty, float(price), reason, now, final=True)

    def check(self, symbol: str, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        trade = self.store.active_plan_trade(symbol)
        position = self.broker.position(symbol)
        q = self.quote_fn(symbol) or {}
        price = q.get("price")
        if not price:
            return
        if trade is not None:
            self._manage(trade, symbol, position, float(price), now, today)
            return
        if symbol not in self.plans or position > 0:
            return
        sid, ver, plan = self.plans[symbol]
        pid, e = plan["plan_id"], plan["entry"]
        if self.store.plan_trade_exists(pid, today) or _hhmm(now) < e["after"]:
            return
        halt = halt_state(self.store, today)
        if halt:
            self._note_once(pid + ":halt", sid, symbol, "PLAN_SKIP", f"{pid} 계좌 정지 중 — {halt.get('reason', '')}", now)
            return
        if q.get("open") is None or q["open"] < e["min_open"]:
            self._note_once(pid + ":open", sid, symbol, "PLAN_SKIP",
                            f"{pid} 시가 {q.get('open') or 0:,.0f} < 조건 {e['min_open']:,.0f} — 오늘 매수 안 함", now)
            return
        if not (e["min_price"] < price <= e["max_price"]):
            self._note_once(pid + ":price", sid, symbol, "PLAN_WAIT", f"{pid} 현재가 {price:,.0f}가 매수 구간 밖 — 계속 확인", now)
            return
        why = self.risk.can_buy(self.store, self.broker, today, _hhmm(now))
        if why:
            self._note_once(pid + ":risk", sid, symbol, "PLAN_SKIP", f"{pid} {why}", now)
            return
        total = self.risk.buy_qty(self.broker.cash(), price)
        if total <= 0:
            self._note_once(pid + ":qty", sid, symbol, "PLAN_SKIP", f"{pid} 매수 수량 0 (현금 부족 또는 금액 한도)", now)
            return
        ex = plan.get("exits") or X.resolve()
        qs = tranche_qty(total, ex.get("scale_in") or [])
        qty = qs[0]
        res = self.broker.place_order(symbol, "BUY", qty, price, self.risk.slippage_pct)
        ok = res.get("status") in ("SUBMITTED", "FILLED")
        tid = self.store.add_plan_trade(plan, sid, ver, qty, price, _ts(now))
        self.store.update_plan_trade(tid, target_qty=total, fills="[]", sells="[]")
        order = {"side": "BUY", "step": 0, "qty": qty, "price": float(price), "at": _ts(now), "base": 0,
                 "order_no": res.get("order_no")}
        if not ok:
            self.store.update_plan_trade(tid, status="rejected", note=str(res)[:500])
        elif res.get("status") == "FILLED":
            self._record(tid, [], [], order, qty, now, "submitted", None)
        else:
            self.store.update_plan_trade(tid, pending=json.dumps(order))
        steps = f" (분할매수 1/{len(qs)}차, 계획 {total}주)" if len(qs) > 1 else ""
        self.store.log_decision(sid, symbol, "PLAN_BUY", "BUY" if ok else "BUY_FAILED",
                                f"{pid} {qty}주{steps} @현재가 {price:,.0f}: {res}", _ts(now))
        log.info(f"[{symbol}] {sid} 계획 매수 {qty}주{steps} @{price:,.0f} → {res.get('status')}")

    # ---------------- 보유 관리 ----------------
    def _record(self, tid: int, fills: list, sells: list, order: dict, done: int, now: datetime, status: str,
                entry_date: str | None) -> None:
        """주문 하나가 done주 체결됐다고 기록하고 상태를 정리한다."""
        today = now.strftime("%Y-%m-%d")
        if order["side"] == "BUY":
            fills = fills + [{"step": order["step"], "qty": int(done), "price": float(order["price"]), "at": _ts(now)}]
        else:
            sells = sells + [{"qty": int(done), "price": float(order["price"]), "at": _ts(now),
                              "reason": order.get("reason", ""), "step": order.get("step")}]
        held = held_qty(fills, sells)
        avg = avg_price(fills)
        f = dict(fills=json.dumps(fills, ensure_ascii=False), sells=json.dumps(sells, ensure_ascii=False), pending=None,
                 qty=held)
        if avg:
            f["entry_price"] = round(avg, 2)
        if order["side"] == "BUY":
            if held <= 0:
                f.update(status="unfilled", note="매수 주문이 체결되지 않음")
            else:
                f.update(status="open", entry_date=entry_date or today)
                if status == "submitted":
                    f["peak"] = float(order["price"])
        elif held <= 0:
            sold = [s for s in sells if s["qty"] > 0]
            f.update(status="closed", exit_date=today, exit_price=round(avg_price(sold) or order["price"], 2),
                     exit_reason=" → ".join(dict.fromkeys(s["reason"] for s in sold)))
        else:
            f["status"] = "open"
        self.store.update_plan_trade(tid, **f)

    def _settle(self, trade: dict, symbol: str, position: int, now: datetime, today: str) -> bool:
        """걸어 둔 주문을 확인한다. 아직 기다려야 하면 True."""
        fills, sells, p = book(trade)
        done = max(0, (position - p["base"]) if p["side"] == "BUY" else (p["base"] - position))
        age = (now - datetime.fromisoformat(p["at"])).total_seconds()
        timeout = self.risk.order_timeout_sec
        expired = bool(timeout) and age >= float(timeout)
        if done < p["qty"] and not expired and p["at"][:10] >= today:
            return True
        if done < p["qty"] and expired:
            res = self.broker.cancel(symbol, p.get("order_no"))
            self.store.log_decision(trade["strategy"], symbol, "ORDER_CANCEL", "CANCEL",
                                    f"{trade['plan_id']} {p['side']} {p['qty']}주 중 {done}주 체결 — {age:.0f}초 지나 남은 수량 취소: {res}",
                                    _ts(now))
        self._record(trade["id"], fills, sells, p, min(done, p["qty"]), now, trade["status"], _date(trade))
        return False

    def _sell(self, trade: dict, symbol: str, qty: int, price: float, reason: str, now: datetime, final: bool,
              step: int | None = None) -> bool:
        fills, sells, _ = book(trade)
        base = self.broker.position(symbol)
        res = self.broker.place_order(symbol, "SELL", qty, price, self.risk.slippage_pct)
        ok = res.get("status") in ("SUBMITTED", "FILLED")
        order = {"side": "SELL", "qty": qty, "price": float(price), "at": _ts(now), "reason": reason, "step": step,
                 "base": base, "order_no": res.get("order_no")}
        if res.get("status") == "FILLED":
            self._record(trade["id"], fills, sells, order, qty, now, trade["status"], _date(trade))
        elif ok:
            self.store.update_plan_trade(trade["id"], pending=json.dumps(order, ensure_ascii=False),
                                         status="closing" if final else trade["status"], exit_submitted_at=_ts(now))
        self.store.log_decision(trade["strategy"], symbol, "PLAN_EXIT", "SELL" if ok else "SELL_FAILED",
                                f"{trade['plan_id']} {reason} {qty}주 @{price:,.0f}: {res}", _ts(now))
        log.info(f"[{symbol}] {trade['strategy']} {reason} {qty}주 → {res.get('status')}")
        return ok

    def _manage(self, trade: dict, symbol: str, position: int, price: float, now: datetime, today: str) -> None:
        if book(trade)[2]:
            if self._settle(trade, symbol, position, now, today):
                return
            trade = self.store.active_plan_trade(symbol)
            if trade is None:
                return
        tid, plan = trade["id"], trade["plan"]
        fills, sells, _ = book(trade)
        held = held_qty(fills, sells)
        if position == 0:
            self.store.update_plan_trade(tid, status="closed", exit_date=today, exit_price=price, qty=0,
                                         exit_reason="계좌에 보유 없음 (수동 정리 등)")
            return
        if position != held:            # 늦은 체결·수동 매매 — 계좌 수량을 따른다
            diff = position - held
            if diff > 0:
                fills = fills + [{"step": max([f["step"] for f in fills], default=0), "qty": diff, "price": price,
                                  "at": _ts(now)}]
            else:
                sells = sells + [{"qty": -diff, "price": price, "at": _ts(now), "reason": "계좌 수량 맞춤", "step": None}]
            self.store.update_plan_trade(tid, fills=json.dumps(fills, ensure_ascii=False),
                                         sells=json.dumps(sells, ensure_ascii=False), qty=position,
                                         entry_price=round(avg_price(fills), 2),
                                         note=(trade.get("note") or "") + f" 계좌 수량 {held}→{position} 맞춤;")
            held = position
            trade = {**trade, "entry_price": avg_price(fills), "fills": fills, "sells": sells}
        ex = plan.get("exits") or X.resolve()
        halt = halt_state(self.store, today)
        if halt and halt.get("flatten"):
            self._sell(trade, symbol, held, price, f"계좌 정지: {halt.get('reason', '')}", now, final=True)
            return
        if self.risk.flatten_at and _hhmm(now) >= self.risk.flatten_at:
            self._sell(trade, symbol, held, price, f"시간 일괄청산 ({self.risk.flatten_at})", now, final=True)
            return
        reason, peak = exit_reason(trade, plan, price, now, today, self.risk.intraday_stop_pct)
        if peak != trade.get("peak"):
            self.store.update_plan_trade(tid, peak=peak)
        if reason:
            self._sell(trade, symbol, held, price, reason, now, final=True)
            return
        avg = avg_price(fills)
        filled = sum(f["qty"] for f in fills)
        done_out = {s.get("step") for s in sells if str(s.get("reason", "")).startswith("분할익절")}
        for k, s in enumerate(ex.get("scale_out") or []):
            if k in done_out or price < avg * (1 + float(s["gain_pct"]) / 100):
                continue
            qty = min(int(math.floor(filled * float(s["pct"]) / 100)), held)
            if qty <= 0:
                continue
            self._sell(trade, symbol, qty, price, f"분할익절 +{float(s['gain_pct']):g}%", now, final=qty >= held, step=k)
            return
        self._scale_in({**trade, "peak": peak}, symbol, fills, sells, price, ex, now, today)

    def _scale_in(self, trade: dict, symbol: str, fills: list, sells: list, price: float, ex: dict, now: datetime,
                  today: str) -> None:
        sin = ex.get("scale_in") or []
        if len(sin) < 2 or not fills or halt_state(self.store, today) or not self.risk.in_buy_window(_hhmm(now)):
            return
        k = max(f["step"] for f in fills) + 1
        if k >= len(sin):
            return
        first = float(fills[0]["price"])
        if price > first * (1 - float(sin[k].get("drop_pct", 0)) / 100):
            return
        total = 0 if _blank(trade.get("target_qty")) else int(trade["target_qty"])
        qs = tranche_qty(total or sum(f["qty"] for f in fills), sin)
        qty = qs[k] if k < len(qs) else 0
        order = {"side": "BUY", "step": k, "qty": qty, "price": float(price), "at": _ts(now),
                 "base": self.broker.position(symbol), "order_no": None}
        if qty <= 0 or qty * price > self.broker.cash():
            self._record(trade["id"], fills, sells, order, 0, now, trade["status"], _date(trade))
            self.store.log_decision(trade["strategy"], symbol, "PLAN_ADD", "NONE",
                                    f"{trade['plan_id']} {k + 1}차 분할매수 건너뜀 (수량 0 또는 현금 부족)", _ts(now))
            return
        res = self.broker.place_order(symbol, "BUY", qty, price, self.risk.slippage_pct)
        ok = res.get("status") in ("SUBMITTED", "FILLED")
        order["order_no"] = res.get("order_no")
        if res.get("status") == "FILLED":
            self._record(trade["id"], fills, sells, order, qty, now, trade["status"], _date(trade))
        elif ok:
            self.store.update_plan_trade(trade["id"], pending=json.dumps(order))
        else:
            self._record(trade["id"], fills, sells, order, 0, now, trade["status"], _date(trade))
        self.store.log_decision(trade["strategy"], symbol, "PLAN_ADD", "ADD" if ok else "ADD_FAILED",
                                f"{trade['plan_id']} {k + 1}/{len(sin)}차 분할매수 {qty}주 @{price:,.0f} "
                                f"(첫 매수가 {first:,.0f} 대비 -{float(sin[k].get('drop_pct', 0)):g}%): {res}", _ts(now))
        log.info(f"[{symbol}] {trade['strategy']} {k + 1}차 분할매수 {qty}주 → {res.get('status')}")


def exit_reason(trade: dict, plan: dict, price: float, now: datetime, today: str,
                intraday_stop_pct: float | None = None) -> tuple[str, float]:
    """(전량 매도 사유 — 없으면 "", 갱신된 최고가). 손절·추적 손절을 목표보다 먼저 본다 (백테스트와 같은 순서).
    기준은 평균 매수가(entry_price) — 분할매수로 평균이 바뀌면 손절·목표도 같이 바뀐다."""
    entry = float(trade["entry_price"])
    ex = plan.get("exits") or X.resolve()
    prev_peak = trade.get("peak")
    prev_peak = entry if prev_peak is None or prev_peak != prev_peak else float(prev_peak)   # NaN 처리
    stop, target = X.levels(ex, entry)
    late = _hhmm(now) >= ex.get("stop_check_after", "15:10")
    if stop is not None and (price <= stop if ex["stop_check"] == "intraday" else (late and price < stop)):
        return f"손절 (매수 {entry:,.0f} → {price:,.0f}, 손절가 {stop:,.0f})", prev_peak
    trail = X.trail_level(ex, entry, prev_peak)
    if trail is not None and price <= trail:
        return f"추적 손절 (최고 {prev_peak:,.0f} → {price:,.0f})", prev_peak
    if intraday_stop_pct and price <= entry * (1 - intraday_stop_pct / 100):
        return f"장중 손절 {intraday_stop_pct}% (사용자 한도)", prev_peak
    if target is not None and price >= target:
        return f"목표가 {target:,.0f} 도달", max(prev_peak, price)
    held = int(np.busday_count(trade.get("entry_date") or today, today)) + 1
    if held >= int(plan.get("max_hold_days") or ex["max_hold_days"]) and _hhmm(now) >= "15:15":
        return f"보유기간 만료 ({held}거래일째)", max(prev_peak, price)
    return "", max(prev_peak, price)
