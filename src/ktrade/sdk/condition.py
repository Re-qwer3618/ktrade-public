"""매매조건 템플릿 — 사용자가 상속하는 것은 Condition 하나뿐이다.

    class MyCondition(Condition):
        default_params = {...}
        def select(self, ctx) -> list[Pick]:   # 장 마감 후 하루 한 번: 종목 선정 + 매수 타이밍
            ...

손절·이익실현·보유기간은 쓰지 않는다 — 엔진이 exits 설정(config.yaml 또는 strategy.yaml)으로 정하고,
실전과 백테스트에서 똑같이 적용한다. 그래서 매매조건만 바꿔 가며 백테스트로 비교할 수 있다.
조건이 할 수 없는 것(엔진이 막음): 주문, 계좌 조회, 데이터 쓰기. ctx.data는 기준일까지의 데이터만 준다.
"""
from __future__ import annotations

from typing import ClassVar

from ktrade.sdk.context import Context
from ktrade.sdk.models import Pick


class Condition:
    id: ClassVar[str] = ""
    version: ClassVar[str] = "v1"
    kind: ClassVar[str] = "condition"
    description: ClassVar[str] = ""
    default_params: ClassVar[dict] = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    def select(self, ctx: Context) -> list[Pick]:
        """기준일(ctx.asof) 장 마감 기준으로, 다음 거래일에 살 후보와 매수 조건(Entry)을 돌려준다."""
        raise NotImplementedError
