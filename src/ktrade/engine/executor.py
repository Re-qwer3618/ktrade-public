"""계획 실행 — 매수는 매매조건이 정한 Entry대로, 매도는 엔진 청산 규칙(engine/exits.py)대로. plan_trades에 남긴다.

상태: submitted(매수 주문) → open(계좌에 보유 확인) → closing(매도 주문) → closed | unfilled | rejected.
매도 판단 (매 확인 주기, 실제 매수가 기준 — 백테스트와 같은 규칙):
  손절(stop_check: intraday면 닿는 즉시 / close면 stop_check_after 이후) · 추적 손절(최고가 기준, plan_trades.peak에 저장)
  · 목표가 · 사용자 장중 손절 %(risk.intraday_stop_pct, 켰을 때) · 보유기간 만료(15:15 이후)
"""
from __future__ import annotations

import logging
from datetime import datetime

import numpy as np

from ktrade.broker.base import Broker
from ktrade.data.store import Store
from ktrade.engine import exits as X
from ktrade.engine.risk import Risk

log = logging.getLogger("ktrade.executor")


def _hhmm(now: datetime) -> str:
    return now.strftime("%H:%M")


class Executor:
    def __init__(self, store: Store, broker: Broker, risk: Risk, quote_fn):
        self.store, self.broker, self.risk, self.quote_fn = store, broker, risk, quote_fn
        self.plans: dict[str, tuple[str, str, dict]] = {}   # 종목 → (조건, 버전, 계획)
        self._noted: set[str] = set()

    def set_plans(self, items: list[tuple[str, str, dict]]) -> None:
        self.plans = {p["symbol"]: (sid, ver, p) for sid, ver, p in items}
        self._noted.clear()

    def symbols(self) -> list[str]:
        return sorted(set(self.plans) | set(self.store.active_plan_symbols()))

    def _note_once(self, key: str, sid: str, symbol: str, signal: str, detail: str, now: datetime) -> None:
        if key not in self._noted:
            self._noted.add(key)
            self.store.log_decision(sid, symbol, signal, "NONE", detail, now.isoformat(timespec="seconds"))

    def check(self, symbol: str, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        trade = self.store.active_plan_trade(symbol)
        position = self.broker.position(symbol)
        q = self.quote_fn(symbol) or {}
        price = q.get("price")
        if not price:
            return
        if trade is not None:
            self._manage(trade, symbol, position, price, now, today)
            return
        if symbol not in self.plans or position > 0:
            return
        sid, ver, plan = self.plans[symbol]
        pid, e = plan["plan_id"], plan["entry"]
        if self.store.plan_trade_exists(pid, today) or _hhmm(now) < e["after"]:
            return
        if q.get("open") is None or q["open"] < e["min_open"]:
            self._note_once(pid + ":open", sid, symbol, "PLAN_SKIP",
                            f"{pid} 시가 {q.get('open') or 0:,.0f} < 조건 {e['min_open']:,.0f} — 오늘 매수 안 함", now)
            return
        if not (e["min_price"] < price <= e["max_price"]):
            self._note_once(pid + ":price", sid, symbol, "PLAN_WAIT", f"{pid} 현재가 {price:,.0f}가 매수 구간 밖 — 계속 확인", now)
            return
        why = self.risk.can_buy(self.store, self.broker, today)
        if why:
            self._note_once(pid + ":risk", sid, symbol, "PLAN_SKIP", f"{pid} {why}", now)
            return
        qty = self.risk.buy_qty(self.broker.cash(), price)
        if qty <= 0:
            self._note_once(pid + ":qty", sid, symbol, "PLAN_SKIP", f"{pid} 매수 수량 0 (현금 부족 또는 비중 한도)", now)
            return
        res = self.broker.place_order(symbol, "BUY", qty, price, self.risk.slippage_pct)
        ok = res.get("status") in ("SUBMITTED", "FILLED")
        tid = self.store.add_plan_trade(plan, sid, ver, qty, price, now.isoformat(timespec="seconds"))
        if not ok:
            self.store.update_plan_trade(tid, status="rejected", note=str(res)[:500])
        elif res.get("status") == "FILLED":
            self.store.update_plan_trade(tid, status="open", entry_date=today, peak=price)
        self.store.log_decision(sid, symbol, "PLAN_BUY", "BUY" if ok else "BUY_FAILED",
                                f"{pid} {qty}주 @현재가 {price:,.0f}: {res}", now.isoformat(timespec="seconds"))
        log.info(f"[{symbol}] {sid} 계획 매수 {qty}주 @{price:,.0f} → {res.get('status')}")

    def _manage(self, trade: dict, symbol: str, position: int, price: float, now: datetime, today: str) -> None:
        tid, st, plan, sid = trade["id"], trade["status"], trade["plan"], trade["strategy"]
        if st == "submitted":
            if position > 0:
                self.store.update_plan_trade(tid, status="open", entry_date=trade["buy_submitted_at"][:10],
                                             peak=trade["entry_price"])
            elif trade["buy_submitted_at"][:10] < today:
                self.store.update_plan_trade(tid, status="unfilled", note="매수 주문 후 다음 거래일까지 보유가 잡히지 않음")
            return
        if st == "closing":
            if position == 0:
                self.store.update_plan_trade(tid, status="closed", exit_date=trade["exit_submitted_at"][:10])
                return
            if trade["exit_submitted_at"][:10] >= today:
                return
            self.store.update_plan_trade(tid, status="open", note=(trade.get("note") or "") + " 매도 미체결 → 보유 유지;")
        if position == 0:
            self.store.update_plan_trade(tid, status="closed", exit_date=today, exit_price=price,
                                         exit_reason="계좌에 보유 없음 (수동 정리 등)")
            return
        reason, peak = exit_reason(trade, plan, price, now, today, self.risk.intraday_stop_pct)
        if peak != trade.get("peak"):
            self.store.update_plan_trade(tid, peak=peak)
        if not reason:
            return
        res = self.broker.place_order(symbol, "SELL", position, price, self.risk.slippage_pct)
        ok = res.get("status") in ("SUBMITTED", "FILLED")
        if ok:
            fields = dict(status="closing", exit_submitted_at=now.isoformat(timespec="seconds"), exit_price=price,
                          exit_reason=reason)
            if res.get("status") == "FILLED":
                fields.update(status="closed", exit_date=today)
            self.store.update_plan_trade(tid, **fields)
        self.store.log_decision(sid, symbol, "PLAN_EXIT", "SELL" if ok else "SELL_FAILED",
                                f"{trade['plan_id']} {reason} {position}주 @{price:,.0f}: {res}", now.isoformat(timespec="seconds"))
        log.info(f"[{symbol}] {sid} {reason} → {res.get('status')}")


def exit_reason(trade: dict, plan: dict, price: float, now: datetime, today: str,
                intraday_stop_pct: float | None = None) -> tuple[str, float]:
    """(매도 사유 — 없으면 "", 갱신된 최고가). 손절·추적 손절을 목표보다 먼저 본다 (백테스트와 같은 순서)."""
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
