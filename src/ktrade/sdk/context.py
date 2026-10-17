"""전략이 받는 실행 맥락(Context)과 읽기 전용 데이터 창(DataView).

DataView는 만들 때 정한 기준일(asof)까지의 데이터만 돌려준다 — 백테스트에서 전략이 미래를 볼 수 없게 하는 장치다.
실전(장 마감 후 계획)과 백테스트가 같은 DataView를 쓰므로, 전략 코드는 두 경우에 똑같이 동작한다.
가격은 수정주가(액면분할·무상증자 보정) 일봉이고, 열 이름은 open/high/low/close/volume, 인덱스는 날짜(DatetimeIndex).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from ktrade.data.store import Store

INDEX_CODE = {"코스피": "001", "코스닥": "101"}
MARKET_OF = {"0": "코스피", "10": "코스닥"}


def _frame(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = df.copy()
    df.index = pd.to_datetime(df.pop(col))
    df.index.name = "date"
    return df


class DataView:
    """symbols를 주면 그 종목만 보인다(백테스트 대상 종목을 좁힐 때). 지수는 항상 보인다."""

    def __init__(self, store: Store, asof: str, symbols: set[str] | frozenset[str] | None = None):
        self._s = store
        self.asof = asof
        self.symbols = frozenset(symbols) if symbols else None

    def _allowed(self, df: pd.DataFrame, col: str) -> pd.DataFrame:
        return df if self.symbols is None or df.empty else df[df[col].isin(self.symbols)].reset_index(drop=True)

    def trading_days(self, n: int) -> list[str]:
        """asof까지 최근 n 거래일 (코스피 지수 일봉이 있는 날)."""
        r = self._s.query("SELECT date FROM index_ohlcv WHERE index_code = '001' AND date <= ? ORDER BY date DESC LIMIT ?",
                          (self.asof, n))
        return sorted(r["date"])

    def daily(self, symbol: str, bars: int = 300) -> pd.DataFrame:
        if self.symbols is not None and symbol not in self.symbols:
            return _frame(pd.DataFrame(), "date")
        r = self._s.query("SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = ? AND date <= ? "
                          "ORDER BY date DESC LIMIT ?", (symbol, self.asof, bars))
        return _frame(r.iloc[::-1], "date")

    def daily_all(self, bars: int = 300) -> pd.DataFrame:
        """전 종목 최근 bars 거래일 일봉 (긴 표: symbol, date, open…). 장 마감 후 전 종목을 훑는 전략용."""
        days = self.trading_days(bars)
        if not days:
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
        return self._allowed(self._s.query("SELECT symbol, date, open, high, low, close, volume FROM ohlcv "
                                           "WHERE date BETWEEN ? AND ? ORDER BY symbol, date", (days[0], days[-1])), "symbol")

    def index(self, market: str = "코스피", bars: int = 300) -> pd.DataFrame:
        code = INDEX_CODE.get(market, market)
        r = self._s.query("SELECT date, open, high, low, close, volume FROM index_ohlcv WHERE index_code = ? AND date <= ? "
                          "ORDER BY date DESC LIMIT ?", (code, self.asof, bars))
        return _frame(r.iloc[::-1], "date")

    def universe(self) -> pd.DataFrame:
        """code, name, market('코스피'/'코스닥'), market_name(거래소/코스닥/리츠…), state(관리종목·거래정지 등)."""
        u = self._s.query("SELECT code, name, market, market_name, state FROM universe")
        u["market"] = u["market"].map(lambda m: MARKET_OF.get(str(m), str(m)))
        return self._allowed(u, "code")

    def minute(self, symbol: str, day: str) -> pd.DataFrame:
        if day > self.asof or (self.symbols is not None and symbol not in self.symbols):
            return _frame(pd.DataFrame(), "ts")
        r = self._s.query("SELECT ts, open, high, low, close, volume FROM minute_ohlcv WHERE symbol = ? AND ts >= ? "
                          "AND ts <= ? ORDER BY ts", (symbol, f"{day} 09:00", f"{day} 15:30:59"))
        return _frame(r, "ts")


@dataclass
class Context:
    asof: str                      # 기준일 = 판단에 쓰는 마지막 일봉 날짜 (YYYY-MM-DD)
    data: DataView
    params: dict = field(default_factory=dict)
    strategy_id: str = ""
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("ktrade.strategy"))
