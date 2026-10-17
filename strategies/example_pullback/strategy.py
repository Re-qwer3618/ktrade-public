"""예제 매매조건: 상승 추세 종목의 20일선 눌림 → 다음 날 20일선 부근에서 매수.

돌파 예제(example_breakout)와 '매수 타이밍'이 다른 예: 다음 날 장중에 가격이 정해 둔 구간으로 내려오면 산다(Entry.pullback).
손절·익절은 strategy.yaml의 exits(ATR 2배 손절, 손절폭 2배 익절)로 엔진이 한다. 작성법 예제 — ktrade test example_pullback.
"""
from __future__ import annotations

from ktrade.sdk import Condition, Context, Entry, Pick


class Pullback(Condition):
    default_params = {"min_price": 2000, "min_avg_value": 5e8, "near_pct": 2.0, "band_pct": 1.5, "max_plans": 5}

    def select(self, ctx: Context) -> list[Pick]:
        p = self.params
        bars = ctx.data.daily_all(bars=62)
        uni = ctx.data.universe()
        uni = uni[uni["market_name"].isin(["거래소", "코스닥"]) & ~uni["state"].fillna("").str.contains("관리종목|거래정지")]
        info = uni.set_index("code")
        out = []
        for sym, g in bars.groupby("symbol"):
            if sym not in info.index or len(g) < 61 or g["date"].iloc[-1] != ctx.asof:
                continue
            c = g["close"]
            close, ma20, ma60 = float(c.iloc[-1]), float(c.tail(20).mean()), float(c.tail(60).mean())
            if close < p["min_price"] or (g["close"] * g["volume"]).tail(20).mean() < p["min_avg_value"]:
                continue
            gap = (close / ma20 - 1) * 100
            # 종목 선정: 20일선이 60일선 위(상승 추세) + 20일선도 5일 전보다 올라감 + 종가가 20일선 위 0~near_pct%
            if not (ma20 > ma60 and ma20 > float(c.iloc[-25:-5].mean()) and 0 <= gap <= p["near_pct"]):
                continue
            b = p["band_pct"] / 100
            out.append(Pick(
                symbol=sym, name=str(info.at[sym, "name"]), market=str(info.at[sym, "market"]),
                # 매수 타이밍: 다음 날 장중 가격이 20일선 ±band% 구간으로 오면
                entry=Entry.pullback(low=round(ma20 * (1 - b)), high=round(ma20 * (1 + b))),
                reason=f"{ctx.asof} 상승 추세(20일선>60일선) 중 종가 {close:,.0f}원이 20일선 {ma20:,.0f}원 위 {gap:.1f}%",
                meta={"gap_pct": round(gap, 2)},
            ))
        out.sort(key=lambda x: x.meta["gap_pct"])
        for i, pk in enumerate(out, 1):
            pk.rank = i
        return out[: int(p["max_plans"])]
