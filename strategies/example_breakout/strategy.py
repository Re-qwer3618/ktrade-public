"""예제 매매조건: 20일 최고 종가 돌파 → 다음 날 확인 매수.

이 파일이 정하는 것은 두 가지뿐이다: 어떤 종목을(종목 선정) / 어떤 조건에서 살지(매수 타이밍 = Entry).
손절·이익실현·보유기간은 엔진이 exits 설정으로 정한다. 검증된 전략이 아니라 작성법 예제다 — ktrade test example_breakout.
"""
from __future__ import annotations

from ktrade.sdk import Condition, Context, Entry, Pick


class Breakout(Condition):
    default_params = {"lookback": 20, "min_price": 2000, "min_avg_value": 5e8, "max_vol_ratio": 4.0,
                      "max_extension_pct": 7, "chase_pct": 5, "max_plans": 5}

    def select(self, ctx: Context) -> list[Pick]:
        p = self.params
        n = int(p["lookback"])
        bars = ctx.data.daily_all(bars=n + 2)
        uni = ctx.data.universe()
        uni = uni[uni["market_name"].isin(["거래소", "코스닥"]) & ~uni["state"].fillna("").str.contains("관리종목|거래정지")]
        info = uni.set_index("code")
        out = []
        for sym, g in bars.groupby("symbol"):
            if sym not in info.index or len(g) < n + 1 or g["date"].iloc[-1] != ctx.asof:
                continue
            close, vol = float(g["close"].iloc[-1]), float(g["volume"].iloc[-1])
            prev = g.iloc[-(n + 1):-1]
            prev_high, avg_vol = float(prev["close"].max()), float(prev["volume"].mean())
            if close < p["min_price"] or (prev["close"] * prev["volume"]).mean() < p["min_avg_value"] or avg_vol <= 0:
                continue
            ext, ratio = (close / prev_high - 1) * 100, vol / avg_vol
            # 종목 선정: 종가로 n일 신고가 돌파, 추격 과다·과열 거래량은 제외
            if not 0 < ext <= p["max_extension_pct"] or ratio >= p["max_vol_ratio"]:
                continue
            out.append(Pick(
                symbol=sym, name=str(info.at[sym, "name"]), market=str(info.at[sym, "market"]),
                # 매수 타이밍: 다음 날 시가가 돌파일 종가 이상이고 15:10 이후에도 그 위면 (돌파 유지 확인)
                entry=Entry.confirm(close, chase_pct=p["chase_pct"], after="15:10"),
                reason=f"{ctx.asof} 종가 {close:,.0f}원이 {n}일 최고 종가 {prev_high:,.0f}원을 {ext:+.1f}% 돌파, 거래량 {ratio:.1f}배",
                meta={"extension_pct": round(ext, 2), "vol_ratio": round(ratio, 2)},
            ))
        out.sort(key=lambda x: x.meta["extension_pct"])          # 덜 추격한 순
        for i, pk in enumerate(out, 1):
            pk.rank = i
        return out[: int(p["max_plans"])]
