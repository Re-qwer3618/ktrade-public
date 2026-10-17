"""사용자 리스크 한도 (config risk) — 전략과 무관하게 엔진이 지킨다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Risk:
    starting_cash: float
    max_position_pct: float = 2.0
    max_concurrent_positions: int = 5
    intraday_stop_pct: float | None = None
    slippage_pct: float = 0.5
    max_new_orders_per_day: int = 10

    @staticmethod
    def from_config(cfg: dict, starting_cash: float) -> "Risk":
        r = cfg["risk"]
        return Risk(starting_cash=starting_cash, max_position_pct=float(r["max_position_pct"]),
                    max_concurrent_positions=int(r["max_concurrent_positions"]),
                    intraday_stop_pct=r.get("intraday_stop_pct"), slippage_pct=float(r["order_slippage_pct"]),
                    max_new_orders_per_day=int(r["max_new_orders_per_day"]))

    def buy_qty(self, cash: float, price: float) -> int:
        """목표 금액 = 시작 총자본 × max_position_pct% (살수록 줄어드는 현금 기준이 아님), 지금 현금 한도 안에서."""
        if price <= 0:
            return 0
        return int(min(self.starting_cash * self.max_position_pct / 100, cash) // price)

    def can_buy(self, store, broker, today: str) -> str:
        """막아야 하면 사유, 괜찮으면 빈 문자열."""
        held = len(broker.positions())
        if held >= self.max_concurrent_positions:
            return f"동시 보유 한도 {held}/{self.max_concurrent_positions}"
        n = store.query("SELECT COUNT(*) AS n FROM decisions WHERE action = 'BUY' AND substr(timestamp, 1, 10) = ?",
                        (today,))["n"].iloc[0]
        if n >= self.max_new_orders_per_day:
            return f"하루 신규 매수 한도 {n}/{self.max_new_orders_per_day}"
        return ""
