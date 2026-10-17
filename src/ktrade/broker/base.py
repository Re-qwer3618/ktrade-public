from __future__ import annotations

from abc import ABC, abstractmethod


class Broker(ABC):
    @abstractmethod
    def cash(self) -> float:
        """지금 새 주문에 쓸 수 있는 현금."""

    @abstractmethod
    def total_deposit(self) -> float:
        """비중 계산 기준 총자본 (세션 시작에 한 번 읽어 고정해서 쓴다)."""

    @abstractmethod
    def positions(self) -> dict[str, dict]:
        """{종목: {qty, avg_price}}"""

    def position(self, symbol: str) -> int:
        return int((self.positions().get(symbol) or {}).get("qty", 0))

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int, price: float, slippage_pct: float = 0.5) -> dict:
        """현재가(price) ± slippage_pct 지정가. 반환 {'status': SUBMITTED|FILLED|REJECTED, ...}"""
