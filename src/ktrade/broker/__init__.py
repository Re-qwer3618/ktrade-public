"""주문을 넣는 곳. 전략은 브로커를 받지 못한다 — 엔진(executor·live)만 쓴다."""
from __future__ import annotations

from ktrade.broker.base import Broker
from ktrade.broker.paper import PaperBroker


def build(cfg: dict, store) -> Broker:
    kind = cfg.get("broker", "paper")
    if kind == "paper":
        from ktrade.config import paths
        return PaperBroker(paths(cfg)["home"] / "data" / "paper_broker.json", cfg["paper"]["starting_cash"])
    if kind == "kiwoom":
        from ktrade.broker.kiwoom import KiwoomBroker
        return KiwoomBroker(cfg)
    raise ValueError(f"알 수 없는 broker: {kind} (paper | kiwoom)")
