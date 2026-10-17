"""손절·이익실현·보유기간 — 엔진 몫. 매매조건 작성자는 쓰지 않고, 설정으로 고른다.

    exits:                                   # config.yaml(전체 기본) → strategy.yaml(조건별 덮어쓰기)
      stop:        {type: pct, value: 7}     # pct(매수가 -%) | atr(매수가 - ATR×배수, period) | low(N일 최저가 -buffer%) | none
      take_profit: {type: pct, value: 10}    # pct(매수가 +%) | r(손절폭 × 배수) | none
      trailing:    {activate_pct: null, trail_pct: null}   # 매수가 대비 +activate% 이상 오른 뒤, 최고가에서 trail% 밀리면 매도
      max_hold_days: 10                      # 매수일 = 1일째, 이 거래일째 장 마감 무렵 매도
      stop_check: close                      # close = 장 후반(stop_check_after 이후)에만 손절 판단 / intraday = 닿는 즉시

손절·목표는 **실제 매수가 기준**으로 계산한다(levels). ATR·N일 최저가처럼 과거 데이터가 필요한 값은 계획을 만들 때
(기준일까지의 데이터로) 미리 구해 plan.exits에 넣는다(prepare). 실전(executor)과 백테스트가 같은 함수를 쓴다.
"""
from __future__ import annotations

import copy

import pandas as pd

DEFAULT = {
    "stop": {"type": "pct", "value": 7.0, "period": 14, "buffer_pct": 1.0},
    "take_profit": {"type": "pct", "value": 10.0},
    "trailing": {"activate_pct": None, "trail_pct": None},
    "max_hold_days": 10,
    "stop_check": "close",
    "stop_check_after": "15:10",
}
_STOP = ("pct", "atr", "low", "none")
_TP = ("pct", "r", "none")


def resolve(*layers: dict | None) -> dict:
    """기본값 ← config.yaml exits ← strategy.yaml exits 순서로 덮어쓰고 검사한다."""
    out = copy.deepcopy(DEFAULT)
    for layer in layers:
        for k, v in (layer or {}).items():
            out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    errs = []
    if out["stop"]["type"] not in _STOP:
        errs.append(f"stop.type은 {_STOP} 중 하나 ({out['stop']['type']})")
    if out["take_profit"]["type"] not in _TP:
        errs.append(f"take_profit.type은 {_TP} 중 하나 ({out['take_profit']['type']})")
    if out["take_profit"]["type"] == "r" and out["stop"]["type"] == "none":
        errs.append("take_profit.type r(손절폭 배수)은 손절이 있어야 쓸 수 있음")
    if out["stop_check"] not in ("close", "intraday"):
        errs.append(f"stop_check은 close 또는 intraday ({out['stop_check']})")
    if not 1 <= int(out["max_hold_days"]) <= 120:
        errs.append("max_hold_days는 1~120")
    t = out["trailing"]
    if t.get("activate_pct") is not None and t.get("trail_pct") is None:
        errs.append("trailing.activate_pct를 쓰려면 trail_pct도 정해야 함")
    if errs:
        raise ValueError("exits 설정 오류: " + "; ".join(errs))
    return out


def prepare(rules: dict, bars: pd.DataFrame) -> dict:
    """계획 시점 값(ATR, N일 최저가)을 채운 청산 설정. bars = 기준일까지의 그 종목 일봉."""
    ex = copy.deepcopy(rules)
    s = ex["stop"]
    if s["type"] == "atr" and len(bars) > 1:
        prev = bars["close"].shift(1)
        tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - prev).abs(), (bars["low"] - prev).abs()], axis=1).max(axis=1)
        s["atr"] = float(tr.tail(int(s.get("period", 14))).mean())
    if s["type"] == "low" and len(bars):
        n = int(s.get("value", 20))
        s["level"] = float(bars["low"].tail(n).min()) * (1 - float(s.get("buffer_pct", 1.0)) / 100)
    return ex


def levels(ex: dict, entry: float) -> tuple[float | None, float | None]:
    """(손절가, 목표가) — 실제 매수가 기준."""
    s, tp = ex["stop"], ex["take_profit"]
    stop = None
    if s["type"] == "pct":
        stop = entry * (1 - float(s["value"]) / 100)
    elif s["type"] == "atr" and s.get("atr"):
        stop = entry - float(s["value"]) * float(s["atr"])
    elif s["type"] == "low" and s.get("level"):
        stop = float(s["level"])
    if stop is not None and stop >= entry:   # N일 최저가가 매수가보다 높은 경우 등 — 손절이 의미 없으면 끈다
        stop = None
    target = None
    if tp["type"] == "pct":
        target = entry * (1 + float(tp["value"]) / 100)
    elif tp["type"] == "r" and stop is not None:
        target = entry + float(tp["value"]) * (entry - stop)
    return (round(stop, 2) if stop else None), (round(target, 2) if target else None)


def trail_level(ex: dict, entry: float, peak: float | None) -> float | None:
    """추적 손절가 — 아직 발동 전이면 None."""
    t = ex.get("trailing") or {}
    if not t.get("trail_pct") or not peak:
        return None
    if t.get("activate_pct") is not None and peak < entry * (1 + float(t["activate_pct"]) / 100):
        return None
    return peak * (1 - float(t["trail_pct"]) / 100)


def describe(ex: dict) -> str:
    s, tp, t = ex["stop"], ex["take_profit"], ex.get("trailing") or {}
    st = {"pct": f"매수가 -{s.get('value')}%", "atr": f"매수가 - ATR({s.get('period', 14)})×{s.get('value')}",
          "low": f"{s.get('value')}일 최저가 -{s.get('buffer_pct', 1)}%", "none": "없음"}[s["type"]]
    tg = {"pct": f"매수가 +{tp.get('value')}%", "r": f"손절폭 × {tp.get('value')}", "none": "없음"}[tp["type"]]
    tr = f", 추적 손절 최고가 -{t['trail_pct']}%" + (f"(+{t['activate_pct']}% 이후)" if t.get("activate_pct") is not None else "") \
        if t.get("trail_pct") else ""
    when = "장중 즉시" if ex["stop_check"] == "intraday" else f"{ex['stop_check_after']} 이후"
    return f"손절 {st}({when}), 익절 {tg}{tr}, 최대 {ex['max_hold_days']}일"
