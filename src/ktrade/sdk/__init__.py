"""매매조건 작성용 SDK — 장 마감 후 종목 선정은 `Condition`(Pick·Entry), 장중 매수 판단은 `Intraday`(Signal).

사용자는 종목 선정·매수 타이밍(Condition.select)만 쓰고, 손절·이익실현·실행·백테스트는 엔진이 한다.
설명과 예제: docs/CONDITION_GUIDE.md, docs/LLM_CONTEXT.md(AI에 붙여 넣는 규격), strategies/example_*
"""
from ktrade.sdk.condition import Condition
from ktrade.sdk.context import Context, DataView
from ktrade.sdk.intraday import Intraday, IntradayContext, Signal
from ktrade.sdk.models import PLAN_SCHEMA, Entry, Pick, Plan

__all__ = ["Condition", "Context", "DataView", "Entry", "Intraday", "IntradayContext", "PLAN_SCHEMA", "Pick", "Plan", "Signal"]
