"""가상 체결 브로커 — 키 없이 연습·테스트용. 주문 가격 그대로 즉시 체결된다고 보고 현금·보유를 JSON 파일에 남긴다."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ktrade.broker.base import Broker


class PaperBroker(Broker):
    def __init__(self, state_path: str | Path, starting_cash: float):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"cash": float(starting_cash), "starting_cash": float(starting_cash), "positions": {}, "history": []}
            self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def cash(self) -> float:
        return self.state["cash"]

    def total_deposit(self) -> float:
        return self.state["starting_cash"]

    def positions(self) -> dict[str, dict]:
        return {s: p for s, p in self.state["positions"].items() if p["qty"] > 0}

    def place_order(self, symbol: str, side: str, qty: int, price: float, slippage_pct: float = 0.5) -> dict:
        cost = price * qty
        pos = self.state["positions"].setdefault(symbol, {"qty": 0, "avg_price": 0.0})
        if side == "BUY":
            if cost > self.state["cash"]:
                return {"status": "REJECTED", "reason": "현금 부족"}
            pos["avg_price"] = (pos["avg_price"] * pos["qty"] + cost) / (pos["qty"] + qty)
            pos["qty"] += qty
            self.state["cash"] -= cost
        elif side == "SELL":
            if qty > pos["qty"]:
                return {"status": "REJECTED", "reason": "보유 수량 부족"}
            pos["qty"] -= qty
            self.state["cash"] += cost
        else:
            return {"status": "REJECTED", "reason": f"알 수 없는 side: {side}"}
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "symbol": symbol, "side": side, "qty": qty, "price": price}
        self.state["history"].append(rec)
        self._save()
        return {"status": "FILLED", **rec}
