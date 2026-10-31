"""장중 조건 실행 — 실전은 키움 실시간 데이터로, 백테스트는 받아 둔 데이터로 같은 Intraday.check()를 부른다.

원칙: **매매 판단·주문은 키움 실시간 데이터 기준**. 내가 받아 둔 데이터(Parquet·DB)는 종목 검색(장 마감 후 매매조건)에만 쓴다.
  · KiwoomSource — 오늘 1분봉(ka10080)·일봉(ka10081)을 키움에서 바로 받는다(저장 안 함, 짧게 캐시)
  · StoredSource — 백테스트용, 받아 둔 분봉·일봉
  · verify_plans — 장 시작 때 계획(내 데이터로 계산)과 키움 일봉이 맞는지 확인. 기준일 종가가 다르면(수정주가 반영 차이,
    받다 만 데이터 등) 그 계획은 사지 않는다 — 내 데이터로 정한 가격 구간이 키움 실제 가격과 어긋날 수 있어서
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from ktrade.engine import loader
from ktrade.sdk.intraday import IntradayContext, Signal

log = logging.getLogger("ktrade.intraday")
BAR = ["open", "high", "low", "close", "volume"]
MISMATCH_PCT = 0.5          # 기준일 종가가 이만큼(%) 넘게 다르면 데이터 불일치


def _ts_index(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=BAR)
    out = df.copy()
    out.index = pd.to_datetime(out.pop(col))
    out.index.name = col
    return out[BAR]


class KiwoomSource:
    """실전 — 키움에서 바로. 분봉은 min_gap초, 일봉은 하루 한 번만 다시 받는다."""

    def __init__(self, kiwoom, min_gap: float = 20.0, clock=time.monotonic):
        self.k, self.min_gap, self.clock = kiwoom, min_gap, clock
        self._min: dict[str, tuple[float, pd.DataFrame]] = {}
        self._day: dict[tuple[str, str], pd.DataFrame] = {}

    def minutes(self, symbol: str, now: datetime) -> pd.DataFrame:
        day = now.strftime("%Y-%m-%d")
        hit = self._min.get(symbol)
        if hit is None or self.clock() - hit[0] >= self.min_gap:
            df = self.k.minute(symbol, since=day, max_pages=2, interval=1)
            df = df[df["ts"].str[:10] == day] if len(df) else df
            hit = (self.clock(), _ts_index(df, "ts"))
            self._min[symbol] = hit
        m = hit[1]
        return m[m.index <= pd.Timestamp(now)]

    def daily(self, symbol: str, now: datetime) -> pd.DataFrame:
        day = now.strftime("%Y-%m-%d")
        if (symbol, day) not in self._day:
            df = self.k.daily(symbol, (now - timedelta(days=450)).strftime("%Y-%m-%d"))
            self._day[(symbol, day)] = _ts_index(df[df["date"] < day] if len(df) else df, "date").tail(300)
        return self._day[(symbol, day)]


class StoredSource:
    """백테스트 — 받아 둔 1분봉·일봉 (그 시각까지만)."""

    def __init__(self, store):
        self.store = store
        self._min: dict[tuple[str, str], pd.DataFrame] = {}

    def day_minutes(self, symbol: str, day: str) -> pd.DataFrame:
        if (symbol, day) not in self._min:
            self._min[(symbol, day)] = _ts_index(self.store.market.minute(symbol, f"{day} 09:00:00", f"{day} 15:30:59"), "ts")
        return self._min[(symbol, day)]

    def minutes(self, symbol: str, now: datetime) -> pd.DataFrame:
        m = self.day_minutes(symbol, now.strftime("%Y-%m-%d"))
        return m[m.index <= pd.Timestamp(now)]

    def daily(self, symbol: str, now: datetime) -> pd.DataFrame:
        df = self.store.market.daily(symbol, end=(now - timedelta(days=1)).strftime("%Y-%m-%d"), last=300)
        return _ts_index(df, "date")


class Runner:
    """장중 조건 인스턴스를 (id, params)마다 하나씩 두고 check를 부른다. 오류는 '사지 않음'으로."""

    def __init__(self, cfg: dict, source):
        self.cfg, self.src = cfg, source
        self._found = None
        self._inst: dict[str, object] = {}

    def _get(self, iid: str, params: dict | None):
        key = iid + json.dumps(params or {}, sort_keys=True)
        if key not in self._inst:
            if self._found is None:
                self._found = loader.of_kind(loader.discover(self.cfg), "intraday")
            if iid not in self._found:
                raise KeyError(f"장중 조건 '{iid}'를 찾지 못했습니다")
            self._inst[key] = loader.load(self._found[iid], params)
        return self._inst[key]

    def check(self, iid: str, params: dict | None, symbol: str, now: datetime, price: float, name: str = "",
              event: str = "watch", insert_price: float | None = None) -> tuple[bool, str, float | None]:
        """(살까, 이유, 추격 상한)."""
        try:
            cond = self._get(iid, params)
            ctx = IntradayContext(symbol=symbol, now=now, price=float(price), minutes=self.src.minutes(symbol, now),
                                  daily=self.src.daily(symbol, now), params=cond.params, name=name, event=event,
                                  insert_price=insert_price, log=logging.getLogger(f"ktrade.intraday.{iid}"))
            sig = cond.check(ctx)
        except Exception as exc:                      # 조건 하나가 죽어도 엔진은 계속 — 그 종목은 안 산다
            log.warning(f"[{iid}] {symbol} 장중 조건 오류: {exc}")
            return False, f"장중 조건 '{iid}' 오류: {str(exc)[:120]}", None
        if isinstance(sig, Signal):
            return bool(sig.buy), sig.reason or ("조건 충족" if sig.buy else "조건 미충족"), sig.max_price
        return bool(sig), "조건 충족" if sig else "조건 미충족", None


def verify_plans(items: list[tuple[str, str, dict]], local_close: dict[str, float], kiwoom_daily, asof: str,
                 tol_pct: float = MISMATCH_PCT) -> tuple[list[tuple[str, str, dict]], list[tuple[str, str, str]]]:
    """계획(내 데이터 기준) ↔ 키움 일봉 정합 확인. (쓸 계획, [(조건, 종목, 사유)] 뺀 것).
    kiwoom_daily(symbol, since) → 열 date·close. 키움 조회가 실패해도 그 계획은 뺀다(확인 못 한 계획으로 사지 않음)."""
    keep, dropped = [], []
    since = (datetime.fromisoformat(asof) - timedelta(days=10)).strftime("%Y-%m-%d")
    cache: dict[str, float | None] = {}
    for sid, ver, p in items:
        sym = p["symbol"]
        if sym not in cache:
            try:
                df = kiwoom_daily(sym, since)
                row = df[df["date"] == asof]
                cache[sym] = float(row["close"].iloc[0]) if len(row) else None
            except Exception as exc:
                log.warning(f"{sym} 키움 일봉 조회 실패: {exc}")
                cache[sym] = None
        kc, lc = cache[sym], local_close.get(sym)
        if kc is None:
            dropped.append((sid, sym, f"키움 일봉에서 기준일 {asof} 종가를 확인하지 못함 — 매수 안 함"))
        elif not lc:
            dropped.append((sid, sym, f"내 데이터에 기준일 {asof} 종가가 없음 — 매수 안 함"))
        elif abs(kc / lc - 1) * 100 > tol_pct:
            dropped.append((sid, sym, f"데이터 불일치 — 기준일 종가 내 데이터 {lc:,.0f} · 키움 {kc:,.0f} "
                                      f"({(kc / lc - 1) * 100:+.2f}%), 매수 안 함 (수정주가·받다 만 데이터 확인)"))
        else:
            keep.append((sid, ver, p))
    return keep, dropped
