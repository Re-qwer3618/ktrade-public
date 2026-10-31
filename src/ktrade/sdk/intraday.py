"""장중 조건 템플릿 — 장중에 '지금 사도 되나'를 판단한다. 사용자가 상속하는 것은 Intraday 하나뿐이다.

    class MyIntraday(Intraday):
        default_params = {...}
        def check(self, ctx) -> Signal | None:   # 살 때면 Signal.buy("이유"), 아니면 None
            ...

쓰이는 곳 두 가지 (같은 코드를 그대로 씀):
  ① 영웅문 조건검색 편입 확인 — 조건식이 종목을 편입하면, 사기 전에 이 조건으로 한 번 더 확인한다 (자동매매 탭 조건식의 '파이썬 확인')
  ② 감시 목록 장중 신호 — 장 마감 후 매매조건이 고른 종목(감시 목록)을 장중에 지켜보다가 이 조건이 맞으면 산다
     (🧪 백테스트 '계획에 쓸 매매조건'의 '장중 조건')

데이터 (ctx) — 실전은 **키움 실시간 데이터**(현재가·오늘 1분봉·일봉 조회), 백테스트는 받아 둔 분봉·일봉. 코드는 같다.
  ctx.price     지금 가격 (키움 실시간 현재가 / 백테스트는 그 분봉 종가)
  ctx.minutes   오늘 1분봉 (지금까지만, 오래된 → 최근, 열 open high low close volume, 인덱스 시각)
  ctx.daily     어제까지 일봉 (최근 300개, 수정주가)
  ctx.now       지금 시각 (datetime) · ctx.symbol · ctx.name · ctx.params
  ctx.event     "insert"(조건검색 편입) | "watch"(감시 목록) · ctx.insert_price 편입가(① 일 때)
  ctx.vwap()    오늘 거래량 가중 평균가 · ctx.day_open · ctx.day_high · ctx.prev_close
이익실현·손실제한·보유기간은 쓰지 않는다 — 엔진 설정(exits)으로. 미래 데이터는 ctx에 없다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

import pandas as pd


@dataclass
class Signal:
    """살 때 돌려주는 값. max_price를 주면 그보다 비싸면 사지 않는다(추격 제한)."""
    buy: bool = True
    reason: str = ""
    max_price: float | None = None

    @staticmethod
    def yes(reason: str = "", max_price: float | None = None) -> "Signal":
        return Signal(True, reason, max_price)

    buy_now = yes


@dataclass
class IntradayContext:
    symbol: str
    now: datetime
    price: float
    minutes: pd.DataFrame
    daily: pd.DataFrame
    params: dict = field(default_factory=dict)
    name: str = ""
    event: str = "watch"
    insert_price: float | None = None
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("ktrade.intraday"))

    def vwap(self) -> float | None:
        m = self.minutes
        if m.empty or m["volume"].sum() <= 0:
            return None
        typical = (m["high"] + m["low"] + m["close"]) / 3
        return float((typical * m["volume"]).sum() / m["volume"].sum())

    @property
    def day_open(self) -> float | None:
        return float(self.minutes["open"].iloc[0]) if len(self.minutes) else None

    @property
    def day_high(self) -> float | None:
        return float(self.minutes["high"].max()) if len(self.minutes) else None

    @property
    def prev_close(self) -> float | None:
        return float(self.daily["close"].iloc[-1]) if len(self.daily) else None


class Intraday:
    id: ClassVar[str] = ""
    version: ClassVar[str] = "v1"
    kind: ClassVar[str] = "intraday"
    description: ClassVar[str] = ""
    default_params: ClassVar[dict] = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    def check(self, ctx: IntradayContext) -> Signal | None:
        """지금 사도 되면 Signal.yes("이유"), 아니면 None."""
        raise NotImplementedError
