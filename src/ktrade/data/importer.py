"""다른 SQLite DB에서 시세 가져오기 — 이미 모아 둔 데이터가 있으면 몇 시간 걸리는 첫 수집을 건너뛴다.

지원: trading-system 형식 (분리 모드 <data>/db/daily.db·minute.db, 또는 단일 market_data.db).
원본은 읽기 전용으로만 연다 — 원본 DB는 절대 바뀌지 않는다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from ktrade.data.store import Store


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=60)


def _find(src: Path, domain: str) -> Path | None:
    if src.is_file():
        return src
    for cand in (src / "db" / f"{domain}.db", src / f"{domain}.db", src / "market_data.db"):
        if cand.exists():
            return cand
    return None


def import_db(store: Store, src: str | Path, since: str | None = None, minute_days: int = 0,
              symbols: list[str] | None = None) -> dict:
    src = Path(src)
    daily = _find(src, "daily")
    if daily is None:
        raise FileNotFoundError(f"일봉 DB를 찾지 못했습니다: {src}")
    stats = {}
    with store.market.batch():
        _import(store, daily, src, since, minute_days, symbols, stats)
    return stats


def _import(store: Store, daily: Path, src: Path, since, minute_days, symbols, stats: dict) -> None:
    c = _ro(daily)
    try:
        u = pd.read_sql_query("SELECT code, name, market, market_name, state FROM universe", c)
        stats["universe"] = store.upsert_universe(u)
        cond, params = ("WHERE date >= ?", (since,)) if since else ("", ())
        idx = pd.read_sql_query(f"SELECT index_code, date, open, high, low, close, volume FROM index_ohlcv {cond}", c, params=params)
        stats["index"] = sum(store.upsert_index(code, g) for code, g in idx.groupby("index_code"))
        syms = symbols or list(pd.read_sql_query("SELECT DISTINCT symbol FROM ohlcv", c)["symbol"])
        n = 0
        for i in range(0, len(syms), 200):
            part = syms[i:i + 200]
            q = (f"SELECT symbol, date, open, high, low, close, volume FROM ohlcv WHERE symbol IN ({','.join('?' * len(part))})"
                 + (" AND date >= ?" if since else ""))
            df = pd.read_sql_query(q, c, params=(*part, *((since,) if since else ())))
            n += store.market.write_daily(df)
            store.market.flush()
        stats["daily_bars"] = n
    finally:
        c.close()
    if minute_days:
        mdb = _find(src, "minute")
        if mdb is not None:
            c = _ro(mdb)
            try:
                days = pd.read_sql_query("SELECT DISTINCT substr(timestamp, 1, 10) AS d FROM intraday_ohlcv WHERE interval = '1m' "
                                         "ORDER BY d DESC LIMIT ?", c, params=(minute_days,))["d"]
                if len(days):
                    m = pd.read_sql_query("SELECT symbol, timestamp AS ts, open, high, low, close, volume FROM intraday_ohlcv "
                                          "WHERE interval = '1m' AND timestamp >= ?", c, params=(days.min(),))
                    stats["minute_bars"] = store.market.write_minute(m)
            finally:
                c.close()
