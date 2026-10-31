"""저장소 — 시세는 Parquet 폴더(<KTRADE_HOME>/data/market/, data/market.py), 나머지는 SQLite 파일 하나(data/market.db).

시세(일봉·지수·분봉·틱·호가)는 수집기만 쓰고, 전략은 DataView(읽기 전용)로만 본다 → Store.market
종목 목록(universe)·매매 기록(plan_trades·decisions)·조건식 신호·설정(settings)은 작고 자주 고쳐서 SQLite에 둔다.
0.7 이전 DB의 시세 표(ohlcv·index_ohlcv·minute_ohlcv)는 처음 열 때 Parquet으로 옮기고(행 수 확인 뒤) 지운다.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from ktrade.data.market import Market

log = logging.getLogger("ktrade.store")

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS universe (code TEXT PRIMARY KEY, name TEXT, market TEXT, market_name TEXT,
       state TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS plan_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL,
       symbol TEXT NOT NULL, name TEXT, strategy TEXT, version TEXT, status TEXT NOT NULL, plan TEXT, qty INTEGER,
       buy_submitted_at TEXT, entry_date TEXT, entry_price REAL, exit_submitted_at TEXT, exit_date TEXT,
       exit_price REAL, exit_reason TEXT, note TEXT DEFAULT '', updated_at TEXT NOT NULL, peak REAL,
       target_qty INTEGER, fills TEXT, sells TEXT, pending TEXT)""",
    """CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
       strategy TEXT, symbol TEXT, signal TEXT, action TEXT, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    # 영웅문 조건식 신호 일지 — 편입(I)·이탈(D)·등록 시점 종목(INIT)과 그때 판단(action)·사유. 포워드 성적의 원자료
    """CREATE TABLE IF NOT EXISTS condition_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, date TEXT NOT NULL,
       cond_name TEXT NOT NULL, seq TEXT, symbol TEXT NOT NULL, event TEXT NOT NULL, event_time TEXT, price REAL,
       mode TEXT, action TEXT NOT NULL, reason TEXT DEFAULT '', plan_id TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_cond_signals_date ON condition_signals(date, cond_name)",
]
# 종목 목록에 0.7에서 더한 열 (키움 ka10099) — 가격·시가총액·투자유의로 받을 범위를 고를 때 쓴다
UNIVERSE_EXTRA = {"last_price": "REAL", "list_count": "INTEGER", "order_warning": "TEXT", "audit_info": "TEXT",
                  "up_name": "TEXT", "up_size": "TEXT"}
UNIVERSE_COLS = ["code", "name", "market", "market_name", "state", *UNIVERSE_EXTRA]
_OLD_MARKET = (("ohlcv", "symbol", "date", "daily"), ("index_ohlcv", "index_code", "date", "index"),
               ("minute_ohlcv", "symbol", "ts", "minute"))

ACTIVE = ("submitted", "open", "closing")
MOVABLE = ("market.db", "market", "paper_broker.json")     # 데이터 폴더를 옮길 때 같이 가는 것


def move_data(src: str | Path, dst: str | Path) -> list[str]:
    """데이터 폴더 내용(market.db·시세 market/·가상 체결 장부)을 dst로 옮긴다. 옮긴 이름을 돌려준다.
    받는 폴더에 같은 이름이 이미 있거나, 다른 프로그램이 DB를 쓰는 중이면 아무것도 옮기지 않고 오류."""
    src, dst = Path(src).expanduser().resolve(), Path(dst).expanduser().resolve()
    if src == dst:
        raise ValueError("지금 폴더와 같습니다")
    if dst.is_relative_to(src):
        raise ValueError("지금 데이터 폴더 안으로는 옮길 수 없습니다")
    have = [n for n in MOVABLE if (src / n).exists()]
    clash = [n for n in have if (dst / n).exists()]
    if clash:
        raise ValueError(f"새 폴더에 이미 {', '.join(clash)}가 있습니다 — 비어 있는 폴더를 고르거나 그 파일을 먼저 치우세요")
    if (src / "market" / ".migrating").exists():
        raise RuntimeError("예전 시세를 옮기는 중입니다 — 끝난 뒤 다시 하세요")
    dst.mkdir(parents=True, exist_ok=True)
    db = src / "market.db"
    gc.collect()                                              # 이 프로세스(대시보드)에 남은 닫히지 않은 DB 연결 정리 — Windows는 열린 파일을 못 옮김
    if db.exists():
        c = sqlite3.connect(db, timeout=5)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # -wal에 남은 기록을 본 파일로
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"DB를 쓰는 중입니다 (ktrade run·hts watch·수집을 멈춘 뒤 다시): {exc}") from exc
        finally:
            c.close()
        try:
            shutil.move(db, dst / "market.db")                 # 가장 먼저 — 잠겨 있으면 여기서 멈춤
        except PermissionError as exc:
            if db.exists():
                (dst / "market.db").unlink(missing_ok=True)    # 다른 드라이브: 복사는 됐는데 원본을 못 지운 경우
            raise RuntimeError("DB를 쓰는 중입니다 — ktrade run·hts watch·수집을 멈춘 뒤 다시 하세요") from exc
        for side in ("market.db-wal", "market.db-shm"):
            (src / side).unlink(missing_ok=True)
    for n in have:
        if n != "market.db":
            shutil.move(src / n, dst / n)
    return have


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self.market = Market(self.path.parent / "market")
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
                ucols = {r[1] for r in c.execute("PRAGMA table_info(universe)")}
                for col, typ in UNIVERSE_EXTRA.items():
                    if col not in ucols:  # 0.7 이전 DB
                        c.execute(f"ALTER TABLE universe ADD COLUMN {col} {typ}")
        if self.path.exists():
            self._migrate_market()

    def _conn(self, write: bool = False) -> sqlite3.Connection:
        if self.readonly and not write:
            return sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=60)
        return sqlite3.connect(self.path, timeout=60)

    def _migrate_market(self) -> None:
        """0.7 이전 DB: SQLite 시세 표 → Parquet. 옮긴 행 수가 맞을 때만 표를 지운다. 다른 프로세스가 옮기는 중이면 건너뜀."""
        c = self._conn()
        try:
            have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        todo = [t for t in _OLD_MARKET if t[0] in have]
        if not todo:
            return
        lock = self.market.root / ".migrating"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if datetime.now().timestamp() - lock.stat().st_mtime < 7200:
                return
            lock.unlink(missing_ok=True)                  # 2시간 넘은 잠금 = 중간에 꺼진 것
            return self._migrate_market()
        os.close(fd)
        try:
            c = self._conn(write=True)
            try:
                for table, key, ts, kind in todo:
                    n = int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    if n:
                        log.info(f"시세 표 {table} {n:,}행을 Parquet으로 옮기는 중…")
                        moved = 0
                        for chunk in pd.read_sql_query(f"SELECT * FROM {table} ORDER BY {ts}", c, chunksize=500_000):
                            with self.market.batch():
                                moved += {"daily": self.market.write_daily, "index": self.market.write_index,
                                          "minute": self.market.write_minute}[kind](chunk)
                        if moved != n:
                            raise RuntimeError(f"{table}: {n}행 중 {moved}행만 옮김 — 원래 표를 지우지 않았습니다")
                    c.execute(f"DROP TABLE {table}")
                    c.commit()
                c.execute("VACUUM")
            finally:
                c.close()
        except Exception as exc:
            log.warning(f"시세 표 옮기기 실패 (SQLite 표는 그대로 둠): {exc}")
        finally:
            lock.unlink(missing_ok=True)

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

    # ---- 시세 쓰기 (수집기 전용) — 실제 저장은 self.market (Parquet) ----
    def upsert_daily(self, symbol: str, df: pd.DataFrame) -> int:
        return self.market.write_daily(df.assign(symbol=symbol))

    def upsert_index(self, code: str, df: pd.DataFrame) -> int:
        return self.market.write_index(df.assign(index_code=code))

    def upsert_minute(self, symbol: str, df: pd.DataFrame, interval: int = 1) -> int:
        return self.market.write_minute(df.assign(symbol=symbol), interval)

    def upsert_universe(self, df: pd.DataFrame) -> int:
        now = _now()
        df = df.reindex(columns=UNIVERSE_COLS)
        df = df.astype(object).where(df.notna(), None)
        rows = [(*r[1:], now) for r in df[UNIVERSE_COLS].itertuples()]
        cols = ", ".join([*UNIVERSE_COLS, "updated_at"])
        return self._many(f"INSERT OR REPLACE INTO universe ({cols}) VALUES ({','.join('?' * (len(UNIVERSE_COLS) + 1))})", rows)

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

    def universe(self) -> pd.DataFrame:
        u = self.query("SELECT * FROM universe")
        return u.reindex(columns=[*UNIVERSE_COLS, *(c for c in u.columns if c not in UNIVERSE_COLS)])

    def last_daily_date(self, symbol: str) -> str | None:
        return self.market.daily_range(symbol)[1]

    def last_index_date_before(self, date: str, code: str = "001") -> str | None:
        d = self.market.index_dates(before=date, last=1, code=code)
        return d[0] if d else None

    def daily_range(self, symbol: str) -> tuple[str | None, str | None]:
        return self.market.daily_range(symbol)

    def index_range(self, code: str = "001") -> tuple[str | None, str | None]:
        return self.market.index_range(code)

    def last_minute_ts(self, symbol: str, interval: int = 1) -> str | None:
        return self.market.last_minute_ts(symbol, interval)

    def summary(self) -> dict:
        """무엇이 얼마나 있는지 (ktrade data)."""
        u = self.query("SELECT market_name, COUNT(*) AS n FROM universe GROUP BY market_name ORDER BY n DESC")
        m = self.market
        return {"daily": m.daily_stats(), "index": m.index_stats(),
                "minute": {iv: m.minute_stats(iv) for iv in m.intervals("minute")},
                "tick": {t: len(m.files("tick", f"{t}t")) for t in m.intervals("tick")},
                "disk": m.disk(), "universe": {r.market_name: int(r.n) for r in u.itertuples()}}

    def symbol_ranges(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """symbol, name, market_name, bars, start, end — 일봉을 받아 둔 종목."""
        r = self.market.daily_ranges(symbols)
        u = self.query("SELECT code AS symbol, name, market_name FROM universe")
        return r.merge(u, on="symbol", how="left")[["symbol", "name", "market_name", "bars", "start", "end"]]

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
