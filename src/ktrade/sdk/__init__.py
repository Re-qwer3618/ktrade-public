"""매매조건 작성용 SDK — `from ktrade.sdk import Condition, Pick, Entry, Context` 만으로 만든다.

사용자는 종목 선정·매수 타이밍(Condition.select)만 쓰고, 손절·이익실현·실행·백테스트는 엔진이 한다.
설명과 예제: docs/CONDITION_GUIDE.md, docs/LLM_CONTEXT.md(AI에 붙여 넣는 규격), strategies/example_*
"""
from ktrade.sdk.condition import Condition
from ktrade.sdk.context import Context, DataView
from ktrade.sdk.models import PLAN_SCHEMA, Entry, Pick, Plan

__all__ = ["Condition", "Context", "DataView", "Entry", "PLAN_SCHEMA", "Pick", "Plan"]
