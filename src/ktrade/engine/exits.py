"""손절·이익실현·보유기간 — 엔진 몫. 매매조건 작성자는 쓰지 않고, 설정으로 고른다.

    exits:                                   # config.yaml(전체 기본) → strategy.yaml(조건별 덮어쓰기)
      stop:        {type: pct, value: 7}     # pct(매수가 -%) | atr(매수가 - ATR×배수, period) | low(N일 최저가 -buffer%) | none
      take_profit: {type: pct, value: 10}    # pct(매수가 +%) | r(손절폭 × 배수) | none
      trailing:    {activate_pct: null, trail_pct: null}   # 매수가 대비 +activate% 이상 오른 뒤, 최고가에서 trail% 밀리면 매도
      max_hold_days: 10                      # 매수일 = 1일째, 이 거래일째 장 마감 무렵 매도
      stop_check: close                      # close = 장 후반(stop_check_after 이후)에만 손절 판단 / intraday = 닿는 즉시
      scale_in:  [{pct: 50}, {pct: 50, drop_pct: 3}]          # 분할매수 — 첫 매수가 대비 -3%에서 나머지 50% (평균가로 손절·목표 다시 계산)
      scale_out: [{pct: 50, gain_pct: 3}]                     # 분할익절 — 평균가 +3%에서 절반, 나머지는 목표가·추적 손절·보유기간

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
    "scale_in": [],                 # 분할매수 [{pct: 50}, {pct: 30, drop_pct: 3}, {pct: 20, drop_pct: 6}] — 비면 한 번에
    "scale_out": [],                # 분할익절 [{pct: 50, gain_pct: 3}, {pct: 50, gain_pct: 6}] — 비면 목표가·추적 손절로 한 번에
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
    sin, sout = out.get("scale_in") or [], out.get("scale_out") or []
    if sin:
        if abs(sum(float(s.get("pct", 0)) for s in sin) - 100) > 1e-6:
            errs.append("scale_in 비율(pct) 합은 100")
        drops = [float(s.get("drop_pct", 0)) for s in sin]
        if drops[0] != 0 or any(b <= a for a, b in zip(drops, drops[1:])):
            errs.append("scale_in drop_pct는 첫 차수 0, 이후 차수는 점점 커져야 함 (예: 0 → 3 → 6)")
        if out["stop"]["type"] == "pct" and len(drops) > 1 and float(out["stop"]["value"]) <= drops[-1]:
            errs.append(f"손절(-{out['stop']['value']}%)이 마지막 분할매수 간격(-{drops[-1]}%)보다 가까움 — 분할매수 전에 손절된다")
    if sout:
        if sum(float(s.get("pct", 0)) for s in sout) > 100 + 1e-6:
            errs.append("scale_out 비율(pct) 합은 100 이하")
        gains = [float(s.get("gain_pct", 0)) for s in sout]
        if any(g <= 0 for g in gains) or any(b <= a for a, b in zip(gains, gains[1:])):
            errs.append("scale_out gain_pct는 0보다 크고 점점 커져야 함 (예: 3 → 5 → 8)")
    if errs:
        raise ValueError("exits 설정 오류: " + "; ".join(errs))
    return out


class PositionSim:
    """한 종목 보유를 봉 단위로 따라간다 — 백테스트(일봉)·조건식 성적표(1분봉) 공용. 실전 Executor와 같은 규칙.

    수량 대신 '계획 수량 대비 비율'로 계산한다. 손절·목표·추적 손절은 **평균 매수가** 기준(분할매수로 평균이 바뀌면 같이 바뀜).
    한 봉 안에서의 순서(보수적): 분할매수 체결 → 손절·추적 손절(둘 다 닿으면 먼저 닿는 높은 쪽) → 분할익절 → 목표가 → 시간 청산.
    scale_in: [{pct: 50}, {pct: 30, drop_pct: 3}, …] — 첫 매수가 대비 drop_pct% 떨어지면 그 차수만큼 더 산다.
    scale_out: [{pct: 50, gain_pct: 3}, …] — 평균 매수가 대비 +gain_pct%에서 체결된 수량의 pct%를 판다. 나머지는 손절·목표·추적·보유기간.
    """

    def __init__(self, ex: dict, first_price: float):
        self.ex = ex
        sin = ex.get("scale_in") or [{"pct": 100, "drop_pct": 0}]
        self.first = float(first_price)
        self.fills: list[tuple[float, float]] = [(self.first, float(sin[0]["pct"]) / 100)]
        self.pending_in = [(self.first * (1 - float(s.get("drop_pct", 0)) / 100), float(s["pct"]) / 100) for s in sin[1:]]
        self.out_steps = [(float(s["gain_pct"]), float(s["pct"]) / 100) for s in (ex.get("scale_out") or [])]
        self.held = self.fills[0][1]
        self.sold: list[tuple[float, float, str]] = []    # (가격, 비율, 사유)
        self.peak = self.first
        self.reason = ""

    @property
    def filled(self) -> float:
        return sum(f for _, f in self.fills)

    @property
    def avg(self) -> float:
        return sum(p * f for p, f in self.fills) / self.filled

    @property
    def closed(self) -> bool:
        return self.held <= 1e-9

    def _sell(self, price: float, frac: float, reason: str) -> None:
        frac = min(frac, self.held)
        if frac > 0:
            self.sold.append((float(price), frac, reason))
            self.held -= frac
            self.reason = reason

    def step(self, o: float, h: float, low: float, c: float, *, late: bool = True, time_exit: bool = False,
             time_reason: str = "보유기간 만료", stop_only: bool = False) -> bool:
        """봉 하나. late = 장 후반 손절 판단 시각 이후인가(stop_check: close). stop_only = 매수 당일 장 시작 매수처럼 손절만 볼 때.
        다 팔렸으면 True."""
        if self.closed:
            return True
        ex = self.ex
        if not stop_only:
            for trig, frac in list(self.pending_in):
                if low <= trig:
                    self.fills.append((min(o, trig), frac))
                    self.held += frac
                    self.pending_in.remove((trig, frac))
        stop, target = levels(ex, self.avg)
        intraday = ex["stop_check"] == "intraday"
        hits = []                                    # (기준가, 체결가, 사유)
        if stop is not None and (low <= stop if intraday else (late and c < stop)):
            hits.append((stop, min(o, stop) if intraday else c, "손절"))
        if not stop_only:
            trail = trail_level(ex, self.avg, self.peak)
            if trail is not None and low <= trail:
                hits.append((trail, min(o, trail), "추적 손절"))
        if hits:
            # 값이 내려오며 먼저 닿는 쪽 = 기준가가 높은 쪽 (같으면 손절)
            _, px, why = max(hits, key=lambda x: (x[0], x[2] == "손절"))
            self._sell(px, self.held, why)
            return True
        if stop_only:
            self.peak = max(self.peak, h)
            return False
        for g, frac in list(self.out_steps):
            level = self.avg * (1 + g / 100)
            if h >= level:
                self._sell(max(o, level), self.filled * frac, f"분할익절 +{g:g}%")
                self.out_steps.remove((g, frac))
        if self.closed:
            return True
        if target is not None and h >= target:
            self._sell(max(o, target), self.held, "목표가")
            return True
        if time_exit:
            self._sell(c, self.held, time_reason)
            return True
        self.peak = max(self.peak, h)
        return False

    def close_all(self, price: float, reason: str) -> None:
        self._sell(price, self.held, reason)

    def result(self, cost_pct: float = 0.0) -> dict:
        """판 만큼의 결과 — 평균 매도가·평균 매수가, 수익률(비용 차감), 차수 기록."""
        sold = sum(f for _, f, _ in self.sold)
        proceeds = sum(p * f for p, f, _ in self.sold)
        exit_px = proceeds / sold if sold else None
        ret = ((proceeds / (self.avg * sold)) - 1) * 100 - cost_pct if sold else None
        why = " → ".join(dict.fromkeys(r for _, _, r in self.sold)) or self.reason
        return {"entry": self.avg, "exit": exit_px, "ret_pct": ret, "reason": why, "fills": len(self.fills),
                "parts": [(round(p, 2), round(f, 4), r) for p, f, r in self.sold]}


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
