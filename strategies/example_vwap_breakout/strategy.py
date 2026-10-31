"""예제 장중 조건 — VWAP 위에서 거래량이 몰리며 직전 N분 고가를 넘으면 매수 신호 (교육용, 검증된 조건 아님).

쓰는 곳: ① 자동매매 탭 조건식의 '파이썬 확인' — 조건검색 편입 종목을 사기 전에 한 번 더
        ② 백테스트 정식판 '계획에 쓸 매매조건'의 '장중 조건' — 장 마감 후 고른 감시 목록을 장중에 지켜보다 신호에 매수
실전은 키움 실시간 데이터(현재가·오늘 1분봉), 백테스트는 받아 둔 1분봉으로 같은 코드가 돈다.
"""
from ktrade.sdk import Intraday, Signal


class VwapBreakout(Intraday):
    default_params = {"start": "09:05", "end": "14:30", "lookback": 5, "vol_mult": 2.0, "above_vwap": True, "chase_pct": 1.0}

    def check(self, ctx):
        p = self.params
        n = int(p["lookback"])
        m = ctx.minutes
        # ── 1) 시간대: 장 시작 직후의 흔들림과 장 막판은 뺀다 ──
        if not (p["start"] <= ctx.now.strftime("%H:%M") <= p["end"]) or len(m) < n + 1:
            return None
        # ── 2) 위치: 오늘 평균 매입 단가(VWAP) 위 ──
        vwap = ctx.vwap()
        if p["above_vwap"] and (vwap is None or ctx.price < vwap):
            return None
        # ── 3) 힘: 거래량이 몰리며 직전 N분 고가 돌파 ──
        prev = m.iloc[-(n + 1):-1]
        avg_vol = prev["volume"].mean()
        if avg_vol <= 0 or m["volume"].iloc[-1] < avg_vol * float(p["vol_mult"]):
            return None
        if ctx.price <= prev["high"].max():
            return None
        return Signal.yes(f"VWAP {vwap:,.0f} 위 · 거래량 {m['volume'].iloc[-1] / avg_vol:.1f}배 · {n}분 고가 돌파",
                          max_price=round(ctx.price * (1 + float(p["chase_pct"]) / 100), 2))
