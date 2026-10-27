"""SQLite 저장소 (파일 하나: <KTRADE_HOME>/data/market.db).

시장 데이터(ohlcv·index_ohlcv·universe·minute_ohlcv)는 수집기만 쓰고, 전략은 DataView(읽기 전용)로만 본다.
매매 기록(plan_trades·decisions)·설정(settings)은 엔진만 쓴다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
       close REAL, volume INTEGER, PRIMARY KEY (symbol, date))""",
    """CREATE TABLE IF NOT EXISTS index_ohlcv (index_code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL,
       low REAL, close REAL, volume INTEGER, PRIMARY KEY (index_code, date))""",
    """CREATE TABLE IF NOT EXISTS universe (code TEXT PRIMARY KEY, name TEXT, market TEXT, market_name TEXT,
       state TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS minute_ohlcv (symbol TEXT NOT NULL, ts TEXT NOT NULL, open REAL, high REAL,
       low REAL, close REAL, volume INTEGER, PRIMARY KEY (symbol, ts))""",
    """CREATE TABLE IF NOT EXISTS plan_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL,
       symbol TEXT NOT NULL, name TEXT, strategy TEXT, version TEXT, status TEXT NOT NULL, plan TEXT, qty INTEGER,
       buy_submitted_at TEXT, entry_date TEXT, entry_price REAL, exit_submitted_at TEXT, exit_date TEXT,
       exit_price REAL, exit_reason TEXT, note TEXT DEFAULT '', updated_at TEXT NOT NULL, peak REAL,
       target_qty INTEGER, fills TEXT, sells TEXT, pending TEXT)""",
    """CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
       strategy TEXT, symbol TEXT, signal TEXT, action TEXT, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv(date)",
    # 영웅문 조건식 신호 일지 — 편입(I)·이탈(D)·등록 시점 종목(INIT)과 그때 판단(action)·사유. 포워드 성적의 원자료
    """CREATE TABLE IF NOT EXISTS condition_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, date TEXT NOT NULL,
       cond_name TEXT NOT NULL, seq TEXT, symbol TEXT NOT NULL, event TEXT NOT NULL, event_time TEXT, price REAL,
       mode TEXT, action TEXT NOT NULL, reason TEXT DEFAULT '', plan_id TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_cond_signals_date ON condition_signals(date, cond_name)",
]

ACTIVE = ("submitted", "open", "closing")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._conn() as c:
                c.execute("PRAGMA journal_mode=WAL")
                for sql in _SCHEMA:
                    c.execute(sql)
                cols = {r[1] for r in c.execute("PRAGMA table_info(plan_trades)")}
                if "peak" not in cols:  # 0.3 이전 DB
                    c.execute("ALTER TABLE plan_trades ADD COLUMN peak REAL")
                for col, typ in (("target_qty", "INTEGER"), ("fills", "TEXT"), ("sells", "TEXT"), ("pending", "TEXT")):
                    if col not in cols:  # 0.6.0 이전 DB (분할매수·분할익절·미체결 기록)
                        c.execute(f"ALTER TABLE plan_trades ADD COLUMN {col} {typ}")

    def _conn(self) -> sqlite3.Connection:
        if self.readonly:
            return sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=60)
        return sqlite3.connect(self.path, timeout=60)

    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        c = self._conn()
        try:
            return pd.read_sql_query(sql, c, params=params)
        finally:
            c.close()

    def _exec(self, sql: str, params: tuple = ()) -> int:
        c = self._conn()
        try:
            with c:
                return c.execute(sql, params).lastrowid
        finally:
            c.close()

    # ---- 시장 데이터 쓰기 (수집기 전용) ----
    def upsert_daily(self, symbol: str, df: pd.DataFrame) -> int:
        rows = [(symbol, r.date, r.open, r.high, r.low, r.close, int(r.volume or 0)) for r in df.itertuples()]
        return self._many("INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?)", rows)

    def upsert_index(self, code: str, df: pd.DataFrame) -> int:
        rows = [(code, r.date, r.open, r.high, r.low, r.close, int(r.volume or 0)) for r in df.itertuples()]
        return self._many("INSERT OR REPLACE INTO index_ohlcv VALUES (?,?,?,?,?,?,?)", rows)

    def upsert_universe(self, df: pd.DataFrame) -> int:
        now = _now()
        rows = [(r.code, r.name, r.market, r.market_name, r.state, now) for r in df.itertuples()]
        return self._many("INSERT OR REPLACE INTO universe VALUES (?,?,?,?,?,?)", rows)

    def upsert_minute(self, symbol: str, df: pd.DataFrame) -> int:
        rows = [(symbol, r.ts, r.open, r.high, r.low, r.close, int(r.volume or 0)) for r in df.itertuples()]
        return self._many("INSERT OR REPLACE INTO minute_ohlcv VALUES (?,?,?,?,?,?,?)", rows)

    def _many(self, sql: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        c = self._conn()
        try:
            with c:
                c.executemany(sql, rows)
            return len(rows)
        finally:
            c.close()

    def last_daily_date(self, symbol: str) -> str | None:
        r = self.query("SELECT MAX(date) AS d FROM ohlcv WHERE symbol = ?", (symbol,))
        return r["d"].iloc[0] if not r.empty else None

    def last_index_date_before(self, date: str, code: str = "001") -> str | None:
        r = self.query("SELECT MAX(date) AS d FROM index_ohlcv WHERE index_code = ? AND date < ?", (code, date))
        return r["d"].iloc[0] if not r.empty else None

    def daily_range(self, symbol: str) -> tuple[str | None, str | None]:
        r = self.query("SELECT MIN(date) AS a, MAX(date) AS b FROM ohlcv WHERE symbol = ?", (symbol,))
        return (r["a"].iloc[0], r["b"].iloc[0]) if not r.empty else (None, None)

    def index_range(self, code: str = "001") -> tuple[str | None, str | None]:
        r = self.query("SELECT MIN(date) AS a, MAX(date) AS b FROM index_ohlcv WHERE index_code = ?", (code,))
        return (r["a"].iloc[0], r["b"].iloc[0]) if not r.empty else (None, None)

    def last_minute_ts(self, symbol: str) -> str | None:
        r = self.query("SELECT MAX(ts) AS t FROM minute_ohlcv WHERE symbol = ?", (symbol,))
        return r["t"].iloc[0] if not r.empty else None

    def summary(self) -> dict:
        """DB에 무엇이 얼마나 있는지 (ktrade data)."""
        d = self.query("SELECT COUNT(DISTINCT symbol) AS n, COUNT(*) AS rows, MIN(date) AS a, MAX(date) AS b FROM ohlcv").iloc[0]
        i = self.query("SELECT index_code, COUNT(*) AS rows, MIN(date) AS a, MAX(date) AS b FROM index_ohlcv GROUP BY index_code")
        m = self.query("SELECT COUNT(DISTINCT symbol) AS n, COUNT(*) AS rows, MIN(ts) AS a, MAX(ts) AS b FROM minute_ohlcv").iloc[0]
        u = self.query("SELECT market_name, COUNT(*) AS n FROM universe GROUP BY market_name ORDER BY n DESC")
        return {"daily": {"symbols": int(d["n"]), "rows": int(d["rows"]), "from": d["a"], "to": d["b"]},
                "index": {r.index_code: {"rows": int(r.rows), "from": r.a, "to": r.b} for r in i.itertuples()},
                "minute": {"symbols": int(m["n"]), "rows": int(m["rows"]), "from": m["a"], "to": m["b"]},
                "universe": {r.market_name: int(r.n) for r in u.itertuples()}}

    def symbol_ranges(self, symbols: list[str] | None = None) -> pd.DataFrame:
        sql = "SELECT o.symbol, u.name, u.market_name, COUNT(*) AS bars, MIN(o.date) AS start, MAX(o.date) AS end " \
              "FROM ohlcv o LEFT JOIN universe u ON u.code = o.symbol"
        params: tuple = ()
        if symbols:
            sql += f" WHERE o.symbol IN ({','.join('?' * len(symbols))})"
            params = tuple(symbols)
        return self.query(sql + " GROUP BY o.symbol ORDER BY o.symbol", params)

    # ---- 설정 ----
    def get_setting(self, key: str, default=None):
        r = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(r["value"].iloc[0]) if not r.empty else default

    def set_setting(self, key: str, value) -> None:
        self._exec("INSERT INTO settings VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                   "updated_at=excluded.updated_at", (key, json.dumps(value, ensure_ascii=False), _now()))

    # ---- 판단 기록 ----
    def log_decision(self, strategy: str, symbol: str, signal: str, action: str, detail: str = "",
                     timestamp: str | None = None) -> None:
        self._exec("INSERT INTO decisions (timestamp, strategy, symbol, signal, action, detail) VALUES (?,?,?,?,?,?)",
                   (timestamp or _now(), strategy, symbol, signal, action, detail))

    # ---- 조건식 신호 일지 ----
    def add_signal(self, cond_name: str, seq: str, symbol: str, event: str, action: str, reason: str = "",
                   price: float | None = None, event_time: str = "", mode: str = "watch", plan_id: str | None = None,
                   ts: str | None = None) -> int:
        ts = ts or _now()
        return self._exec("""INSERT INTO condition_signals (ts, date, cond_name, seq, symbol, event, event_time, price, mode, action,
                             reason, plan_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (ts, ts[:10], cond_name, seq, symbol, event, event_time, price, mode, action, reason, plan_id))

    def signals(self, date: str | None = None, cond_name: str | None = None) -> pd.DataFrame:
        sql, params = "SELECT * FROM condition_signals WHERE 1=1", []
        if date:
            sql += " AND date = ?"
            params.append(date)
        if cond_name:
            sql += " AND cond_name = ?"
            params.append(cond_name)
        return self.query(sql + " ORDER BY id", tuple(params))

    # ---- 계획 매매 기록 ----
    def add_plan_trade(self, plan: dict, strategy: str, version: str, qty: int, order_price: float,
                       submitted_at: str) -> int:
        return self._exec(
            """INSERT INTO plan_trades (plan_id, symbol, name, strategy, version, status, plan, qty, buy_submitted_at,
               entry_price, updated_at) VALUES (?,?,?,?,?,'submitted',?,?,?,?,?)""",
            (plan["plan_id"], plan["symbol"], plan.get("name"), strategy, version,
             json.dumps(plan, ensure_ascii=False), qty, submitted_at, order_price, _now()))

    def active_plan_trade(self, symbol: str) -> dict | None:
        r = self.query(f"SELECT * FROM plan_trades WHERE symbol = ? AND status IN ({','.join('?' * len(ACTIVE))}) "
                       "ORDER BY id DESC LIMIT 1", (symbol, *ACTIVE))
        if r.empty:
            return None
        rec = r.iloc[0].to_dict()
        rec["plan"] = json.loads(rec["plan"]) if rec.get("plan") else {}
        return rec

    def active_plan_symbols(self) -> list[str]:
        r = self.query(f"SELECT DISTINCT symbol FROM plan_trades WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE)
        return list(r["symbol"])

    def plan_trade_exists(self, plan_id: str, today: str) -> bool:
        """이 계획으로 이미 주문을 냈나. 주문 거부(rejected)는 그날만 막는다(휴장일 거부 뒤 다음 거래일 재시도)."""
        r = self.query("SELECT 1 FROM plan_trades WHERE plan_id = ? AND NOT (status = 'rejected' AND "
                       "substr(buy_submitted_at, 1, 10) < ?) LIMIT 1", (plan_id, today))
        return not r.empty

    def update_plan_trade(self, trade_id: int, **fields) -> None:
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE plan_trades SET {sets} WHERE id = ?", (*fields.values(), int(trade_id)))

    def plan_trades(self, limit: int = 500) -> pd.DataFrame:
        return self.query("SELECT * FROM plan_trades ORDER BY id DESC LIMIT ?", (limit,))
