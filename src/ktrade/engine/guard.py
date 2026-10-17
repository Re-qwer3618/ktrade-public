"""엔진 쪽 계획 검사 — 매매조건이 무엇을 내든 엔진은 이 검사를 통과한 계획만 실행한다.

가격이 말이 되는지, 기준일 종가에서 너무 멀지 않은지(가격제한폭 ±30%), 하루 계획 수 한도,
여러 조건이 같은 종목을 고르면 우선순위가 높은(config에서 위에 있는) 조건만.
"""
from __future__ import annotations

import re

from ktrade.sdk.models import Plan

_HHMM = re.compile(r"^(0\d|1\d|2[0-3]):[0-5]\d$")
MAX_GAP_FROM_CLOSE = 0.30


def check(plan: Plan, last_close: float | None) -> list[str]:
    e, errs = plan.entry, []
    for k, v in {"min_open": e.min_open, "min_price": e.min_price, "max_price": e.max_price}.items():
        if not isinstance(v, (int, float)) or v <= 0:
            errs.append(f"entry.{k}가 양수가 아님 ({v})")
    if errs:
        return errs
    if not re.fullmatch(r"[0-9A-Z]{6}", plan.symbol):
        errs.append(f"종목코드 형식 오류 ({plan.symbol})")
    if e.min_price >= e.max_price:
        errs.append(f"매수 구간이 비었음: min_price {e.min_price} >= max_price {e.max_price}")
    if plan.stop is not None and not plan.stop < e.min_price:
        errs.append(f"손절가 {plan.stop}가 매수 하한 {e.min_price}보다 낮지 않음")
    if plan.target is not None and not plan.target > e.min_price:
        errs.append(f"목표가 {plan.target}가 매수 하한 {e.min_price}보다 높지 않음")
    if not _HHMM.match(e.after):
        errs.append(f"after 시각 형식 오류 ({e.after}, HH:MM)")
    if not 1 <= int(plan.max_hold_days) <= 120:
        errs.append(f"max_hold_days 범위 밖 ({plan.max_hold_days}, 1~120)")
    if last_close:
        for k, v in (("min_price", e.min_price), ("max_price", e.max_price), ("stop", plan.stop), ("target", plan.target)):
            if v is not None and abs(v / last_close - 1) > MAX_GAP_FROM_CLOSE:
                errs.append(f"{k} {v:,.0f}가 기준일 종가 {last_close:,.0f}에서 ±{MAX_GAP_FROM_CLOSE:.0%} 밖")
    else:
        errs.append("기준일 종가 없음 (그 종목 일봉이 DB에 없음)")
    return errs


def screen(by_strategy: list[tuple[str, list[Plan]]], closes: dict[str, float], max_per_strategy: int
           ) -> tuple[dict[str, list[Plan]], list[tuple[str, str, str]]]:
    """(조건별 통과 계획, 거부 [(조건, 종목, 사유)]). by_strategy는 우선순위 순(앞이 높음)."""
    taken: set[str] = set()
    ok: dict[str, list[Plan]] = {}
    rejected = []
    for sid, plans in by_strategy:
        ranked = sorted(plans, key=lambda p: (p.rank is None, p.rank or 0))
        ok.setdefault(sid, [])
        for p in ranked:
            if len(ok[sid]) >= max_per_strategy:
                rejected.append((sid, p.symbol, f"조건당 하루 계획 한도 {max_per_strategy} 초과"))
                continue
            if p.symbol in taken:
                rejected.append((sid, p.symbol, "우선순위 높은 다른 조건이 이미 고른 종목"))
                continue
            errs = check(p, closes.get(p.symbol))
            if errs:
                rejected.append((sid, p.symbol, "; ".join(errs)))
                continue
            taken.add(p.symbol)
            ok[sid].append(p)
    return ok, rejected
