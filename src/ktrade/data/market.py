"""시세 저장소 — 종류별 폴더 + 기간별 Parquet 파일 (<KTRADE_HOME>/data/market/).

  daily/2026.parquet             일봉 (그해 전 종목)                 symbol, date, open, high, low, close, volume
  index/index.parquet            지수 일봉 (코스피 001 · 코스닥 101)  index_code, date, open … volume
  minute/1m/2026-10.parquet      N분봉 (그달 전 종목, 폴더 = 분 단위) symbol, ts, open … volume
  tick/1t/2026-10-30.parquet     N틱봉 (그날 전 종목, 폴더 = 틱 단위) symbol, ts, seq, open … volume
  orderbook/2026-10-30.parquet   호가 (그날 전 종목 — 키움이 지난 호가를 주지 않아 실시간으로 쌓은 것만)

종목별이 아니라 기간별로 나눈 까닭: 백테스트는 '그날 전 종목'을 읽고, 매일 이어받기는 새 기간 파일 하나만 쓰면 된다.
지난 기간 파일은 다시 바뀌지 않으니 새 파일만 복사하면 백업 끝, 오래된 해는 폴더째 옮겨도 된다.
한 파일 안은 종목·시각 순으로 정렬돼 있어 한 종목만 읽어도 그 부분(행 그룹)만 읽는다. 압축(zstd)으로 SQLite보다 몇 배 작다.

쓰기는 batch() 안에서 모아 두었다가 파일마다 한 번에 합쳐 쓴다(같은 키는 새 값이 이김, 틱은 종목·날짜 단위로 통째 교체).
이동평균 같은 지표는 저장하지 않는다 — 종가로 언제든 다시 계산한다(dashboard 차트, 전략 코드).
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pandas.api.types import union_categoricals

BAR = ["open", "high", "low", "close", "volume"]
MINUTES = (1, 3, 5, 10, 15, 30, 45, 60)      # 키움 ka10080 tic_scope
TICKS = (1, 3, 5, 10, 30)                    # 키움 ka10079 tic_scope
_ROW_GROUP = 50_000
_cache: dict[str, tuple] = {}                # 파일 경로 → (서명, 표)


def _sig(path: Path):
    try:
        s = path.stat()
        return s.st_mtime_ns, s.st_size
    except OSError:
        return None


def _read(path: Path, columns=None, filters=None) -> pd.DataFrame:
    """filters가 없으면 통째로 읽고 파일이 바뀔 때까지 기억한다(일봉·지수처럼 자주 읽는 것)."""
    if filters is not None:
        try:
            return pq.read_table(path, columns=columns, filters=filters).to_pandas()
        except (OSError, pa.ArrowInvalid):
            return pd.DataFrame(columns=columns or [])
    sig = _sig(path)
    if sig is None:
        return pd.DataFrame(columns=columns or [])
    hit = _cache.get(str(path))
    if hit and hit[0] == sig:
        df = hit[1]
    else:
        df = pq.read_table(path).to_pandas()
        _cache[str(path)] = (sig, df)
    return df[columns] if columns else df


def _write(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp, compression="zstd", row_group_size=_ROW_GROUP)
    for i in range(20):                      # 윈도우: 다른 프로세스(대시보드)가 잠깐 열고 있으면 바꿔치기가 막힌다
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.25)
    _cache.pop(str(path), None)


def _norm(df: pd.DataFrame, key: str, ts: str, extra: tuple[str, ...] = ()) -> pd.DataFrame:
    out = pd.DataFrame({key: df[key].astype(str), ts: df[ts].astype(str)})
    for c in extra:
        out[c] = df[c].astype("int64")
    for c in BAR[:-1]:
        out[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    out["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    return out


def _ymd(dates: np.ndarray) -> np.ndarray:
    """정수 날짜(20261030) → 'YYYY-MM-DD' 문자열 배열 (다른 날짜 수만큼만 실제로 만든다)."""
    if not len(dates):
        return np.array([], dtype=object)
    u, inv = np.unique(dates, return_inverse=True)
    s = u.astype(str)
    return np.array([f"{x[:4]}-{x[4:6]}-{x[6:]}" for x in s], dtype=object)[inv]


def _int_day(day: str) -> int:
    return int(str(day)[:10].replace("-", ""))


class _Daily:
    """일봉 전체를 메모리에 — 문자열 대신 숫자 배열(종목 번호·정수 날짜·float32 가격)로 들고 있어
    40년치 2,500종목(1,170만 행)도 수백 MB 안쪽. 종목순·날짜순 위치를 미리 계산해 둔다."""

    def __init__(self, files: list[Path]):
        cats, dates, cols = [], [], {c: [] for c in BAR}
        for f in files:
            t = pq.read_table(f)
            if not t.num_rows:
                continue
            cats.append(pd.Categorical(t.column("symbol").to_numpy(zero_copy_only=False)))
            dates.append(pc.cast(pc.replace_substring(t.column("date"), "-", ""), pa.int32()).to_numpy())
            for c in BAR:
                cols[c].append(t.column(c).to_numpy().astype("int64" if c == "volume" else "float32"))
        if cats:
            u = union_categoricals(cats, sort_categories=True)
            self.names, code = np.asarray(u.categories, dtype=object), u.codes.astype("int32")
            date = np.concatenate(dates)
        else:
            self.names, code, date = np.array([], dtype=object), np.array([], "int32"), np.array([], "int32")
        order = np.lexsort((date, code))
        self.code, self.date = code[order], date[order]
        self.cols = {c: np.concatenate(v)[order] if v else np.array([]) for c, v in cols.items()}
        n = len(self.names)
        self.starts = np.searchsorted(self.code, np.arange(n), "left")
        self.ends = np.searchsorted(self.code, np.arange(n), "right")
        self.pos = {s: i for i, s in enumerate(self.names)}
        self.by_date = np.argsort(self.date, kind="stable")            # 날짜순 위치 (같은 날 안에서는 종목순)
        self.dates = self.date[self.by_date]

    def __len__(self) -> int:
        return len(self.date)

    def span(self, symbol: str) -> tuple[int, int]:
        i = self.pos.get(symbol)
        return (0, 0) if i is None else (int(self.starts[i]), int(self.ends[i]))

    def frame(self, idx, symbol: bool = False) -> pd.DataFrame:
        out = {"symbol": self.names[self.code[idx]]} if symbol else {}
        out["date"] = _ymd(self.date[idx])
        for c in BAR:
            out[c] = self.cols[c][idx].astype("int64" if c == "volume" else "float64")
        return pd.DataFrame(out)


_DAILY: dict[str, tuple] = {}                 # 폴더 → (파일 서명, _Daily) — 대시보드가 새로 그릴 때마다 다시 읽지 않게


class Market:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._buf: dict[tuple, list[pd.DataFrame]] = defaultdict(list)
        self._depth = 0

    # ───────────── 경로 ─────────────
    def _p(self, kind: str, part: str, sub: str = "") -> Path:
        return self.root / kind / sub / f"{part}.parquet" if sub else self.root / kind / f"{part}.parquet"

    def files(self, kind: str, sub: str = "") -> list[Path]:
        d = self.root / kind / sub if sub else self.root / kind
        return sorted(d.glob("*.parquet")) if d.is_dir() else []

    # ───────────── 쓰기 ─────────────
    @contextmanager
    def batch(self):
        """안에서 쓴 것을 모아 두었다가 끝날 때(또는 flush()) 파일마다 한 번에 쓴다. 수천 종목을 받을 때 쓴다."""
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            if self._depth == 0:
                self.flush()

    def _put(self, kind: str, sub: str, df: pd.DataFrame, part: pd.Series) -> int:
        if df.empty:
            return 0
        for p, g in df.groupby(part.to_numpy()):
            self._buf[(kind, sub, str(p))].append(g)
        if not self._depth:
            self.flush()
        return len(df)

    def pending(self) -> int:
        return sum(len(f) for fs in self._buf.values() for f in fs)

    def flush(self) -> None:
        buf, self._buf = self._buf, defaultdict(list)
        for (kind, sub, part), frames in buf.items():
            new = pd.concat(frames, ignore_index=True)
            key, order = _KEYS[kind]
            path = self._p(kind, part, sub)
            old = _read(path) if path.exists() else None
            if old is not None and len(old):
                if kind == "tick":           # 틱은 같은 초에 여러 건이라 키가 없다 → 받은 종목·날짜를 통째 교체
                    gone = (old["symbol"] + old["ts"].str[:10]).isin(set(new["symbol"] + new["ts"].str[:10]))
                    old = old[~gone]
                new = pd.concat([old, new], ignore_index=True)
            new = new.drop_duplicates(key, keep="last").sort_values(order, kind="stable").reset_index(drop=True)
            _write(path, new)

    def write_daily(self, df: pd.DataFrame) -> int:
        """df: symbol, date(YYYY-MM-DD), open … volume (여러 종목 가능)."""
        if df.empty:
            return 0
        d = _norm(df, "symbol", "date")
        return self._put("daily", "", d, d["date"].str[:4])

    def write_index(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        d = _norm(df, "index_code", "date")
        return self._put("index", "", d, pd.Series("index", index=d.index))

    def write_minute(self, df: pd.DataFrame, interval: int = 1) -> int:
        """df: symbol, ts(YYYY-MM-DD HH:MM:SS), open … volume."""
        if df.empty:
            return 0
        d = _norm(df, "symbol", "ts")
        return self._put("minute", f"{int(interval)}m", d, d["ts"].str[:7])

    def write_tick(self, df: pd.DataFrame, ticks: int = 1) -> int:
        """df: symbol, ts, open … volume — 시간 순서대로. 같은 초 안의 순서는 seq로 지킨다."""
        if df.empty:
            return 0
        d = df.copy()
        d["seq"] = d.groupby([d["symbol"], d["ts"].astype(str).str[:10]]).cumcount()
        d = _norm(d, "symbol", "ts", ("seq",))
        return self._put("tick", f"{int(ticks)}t", d, d["ts"].str[:10])

    def write_orderbook(self, df: pd.DataFrame) -> int:
        """df: symbol, ts, 그 밖의 열은 자유(호가·잔량). 실시간 수신기가 쓴다."""
        if df.empty:
            return 0
        d = df.copy()
        d["symbol"], d["ts"] = d["symbol"].astype(str), d["ts"].astype(str)
        return self._put("orderbook", "", d, d["ts"].str[:10])

    # ───────────── 읽기: 일봉 ─────────────
    def _d(self) -> _Daily:
        fs = self.files("daily")
        sig = tuple((f.name, _sig(f)) for f in fs)
        hit = _DAILY.get(str(self.root))
        if hit is None or hit[0] != sig:
            hit = (sig, _Daily(fs))
            _DAILY[str(self.root)] = hit
        return hit[1]

    def has_daily(self) -> bool:
        return bool(self.files("daily"))

    def daily(self, symbol: str, start: str | None = None, end: str | None = None, after: str | None = None,
              last: int | None = None, first: int | None = None) -> pd.DataFrame:
        """한 종목 일봉 (열: date, open … volume, 날짜순). last=끝에서 n개, first=앞에서 n개."""
        d = self._d()
        a, b = d.span(symbol)
        dt = d.date[a:b]
        lo = a + (np.searchsorted(dt, _int_day(start), "left") if start else 0)
        if after:
            lo = max(lo, a + int(np.searchsorted(dt, _int_day(after), "right")))
        hi = a + np.searchsorted(dt, _int_day(end), "right") if end else b
        hi = max(lo, hi)
        if last:
            lo = max(lo, hi - last)
        if first:
            hi = min(hi, lo + first)
        return d.frame(slice(lo, hi))

    def _day_slice(self, start: str, end: str) -> np.ndarray:
        d = self._d()
        a, b = np.searchsorted(d.dates, _int_day(start), "left"), np.searchsorted(d.dates, _int_day(end), "right")
        return d.by_date[a:b]

    def daily_between(self, start: str, end: str, symbols=None) -> pd.DataFrame:
        """전 종목 긴 표 (symbol, date, open … volume), 종목·날짜순."""
        d = self._d()
        idx = np.sort(self._day_slice(start, end))                    # 원래 배열이 종목·날짜순
        if symbols is not None:
            want = np.array([d.pos[s] for s in set(symbols) if s in d.pos], dtype="int32")
            idx = idx[np.isin(d.code[idx], want)]
        return d.frame(idx, symbol=True)

    def closes(self, day: str, symbols) -> dict[str, float]:
        d = self._d()
        idx = self._day_slice(day, day)
        got = dict(zip(d.names[d.code[idx]], d.cols["close"][idx].astype(float).tolist()))
        return {s: got[s] for s in symbols if s in got}

    def daily_range(self, symbol: str) -> tuple[str | None, str | None]:
        d = self._d()
        a, b = d.span(symbol)
        return (str(_ymd(d.date[[a]])[0]), str(_ymd(d.date[[b - 1]])[0])) if b > a else (None, None)

    def daily_ranges(self, symbols=None) -> pd.DataFrame:
        """symbol, bars, start, end."""
        d = self._d()
        keep = np.arange(len(d.names)) if symbols is None else np.array([d.pos[s] for s in symbols if s in d.pos], "int64")
        keep = keep[d.ends[keep] > d.starts[keep]]
        return pd.DataFrame({"symbol": d.names[keep], "bars": d.ends[keep] - d.starts[keep],
                             "start": _ymd(d.date[d.starts[keep]]), "end": _ymd(d.date[d.ends[keep] - 1])})

    def daily_stats(self) -> dict:
        d = self._d()
        ends = _ymd(d.dates[[0, -1]]) if len(d) else [None, None]
        return {"symbols": int((d.ends > d.starts).sum()), "rows": len(d), "from": ends[0], "to": ends[1]}

    # ───────────── 읽기: 지수 ─────────────
    def _idx(self, code: str) -> pd.DataFrame:
        df = _read(self._p("index", "index"))
        return df[df["index_code"] == code] if len(df) else pd.DataFrame(columns=["index_code", "date", *BAR])

    def index(self, code: str = "001", start: str | None = None, end: str | None = None, last: int | None = None,
              before: str | None = None) -> pd.DataFrame:
        df = self._idx(code)
        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        if before:
            df = df[df["date"] < before]
        if last:
            df = df.tail(last)
        return df[["date", *BAR]].reset_index(drop=True)

    def index_dates(self, start: str | None = None, end: str | None = None, last: int | None = None,
                    before: str | None = None, code: str = "001") -> list[str]:
        """거래일 달력 = 코스피 지수 일봉이 있는 날."""
        return list(self.index(code, start, end, last, before)["date"])

    def index_range(self, code: str = "001") -> tuple[str | None, str | None]:
        df = self._idx(code)
        return (df["date"].iloc[0], df["date"].iloc[-1]) if len(df) else (None, None)

    def index_stats(self) -> dict:
        df = _read(self._p("index", "index"))
        if not len(df):
            return {}
        return {c: {"rows": len(g), "from": g["date"].min(), "to": g["date"].max()} for c, g in df.groupby("index_code")}

    # ───────────── 읽기: 분봉·틱·호가 ─────────────
    def intervals(self, kind: str = "minute") -> list[int]:
        d = self.root / kind
        return sorted(int(p.name[:-1]) for p in d.iterdir() if p.is_dir() and p.name[:-1].isdigit()) if d.is_dir() else []

    def minute(self, symbol: str, start: str, end: str, interval: int = 1) -> pd.DataFrame:
        """ts(YYYY-MM-DD HH:MM:SS) 구간의 N분봉. 그 단위로 받아 둔 게 없으면 1분봉을 묶어 만든다."""
        sub = f"{int(interval)}m"
        if interval != 1 and not self.files("minute", sub):
            return resample(self.minute(symbol, start, end, 1), interval)
        frames = [_read(f, filters=[("symbol", "==", symbol), ("ts", ">=", start), ("ts", "<=", end)])
                  for f in self.files("minute", sub) if start[:7] <= f.stem <= end[:7]]
        frames = [f for f in frames if len(f)]
        if not frames:
            return pd.DataFrame(columns=["ts", *BAR])
        return pd.concat(frames, ignore_index=True).sort_values("ts")[["ts", *BAR]].reset_index(drop=True)

    def last_minute_ts(self, symbol: str, interval: int = 1) -> str | None:
        for f in reversed(self.files("minute", f"{int(interval)}m")):
            df = _read(f, columns=["ts"], filters=[("symbol", "==", symbol)])
            if len(df):
                return str(df["ts"].max())
        return None

    def tick(self, symbol: str, day: str, ticks: int = 1) -> pd.DataFrame:
        f = self._p("tick", day, f"{int(ticks)}t")
        if not f.exists():
            return pd.DataFrame(columns=["ts", "seq", *BAR])
        df = _read(f, filters=[("symbol", "==", symbol)])
        return df.sort_values(["ts", "seq"])[["ts", "seq", *BAR]].reset_index(drop=True)

    def tick_days(self, symbol: str | None = None, ticks: int = 1) -> list[str]:
        fs = self.files("tick", f"{int(ticks)}t")
        if symbol is None:
            return [f.stem for f in fs]
        return [f.stem for f in fs if len(_read(f, columns=["symbol"], filters=[("symbol", "==", symbol)]))]

    def orderbook(self, symbol: str, day: str) -> pd.DataFrame:
        f = self._p("orderbook", day)
        return _read(f, filters=[("symbol", "==", symbol)]).sort_values("ts").reset_index(drop=True) if f.exists() \
            else pd.DataFrame()

    # ───────────── 요약 ─────────────
    def disk(self) -> dict[str, dict]:
        """종류별 파일 수·용량(MB) — 무엇이 공간을 차지하는지."""
        out = {}
        for kind in ("daily", "index", "minute", "tick", "orderbook"):
            fs = list((self.root / kind).rglob("*.parquet")) if (self.root / kind).is_dir() else []
            if fs:
                out[kind] = {"files": len(fs), "mb": round(sum(f.stat().st_size for f in fs) / 1e6, 1)}
        return out

    def minute_stats(self, interval: int = 1) -> dict:
        fs = self.files("minute", f"{int(interval)}m")
        if not fs:
            return {"symbols": 0, "rows": 0, "from": None, "to": None}
        syms, rows, lo, hi = set(), 0, None, None
        for f in fs:
            df = pq.read_table(f, columns=["symbol", "ts"]).to_pandas()
            if len(df):
                syms |= set(df["symbol"])
                rows += len(df)
                lo = min(lo or df["ts"].min(), df["ts"].min())
                hi = max(hi or df["ts"].max(), df["ts"].max())
        return {"symbols": len(syms), "rows": rows, "from": lo, "to": hi}


_KEYS = {"daily": (["symbol", "date"], ["symbol", "date"]),
         "index": (["index_code", "date"], ["index_code", "date"]),
         "minute": (["symbol", "ts"], ["symbol", "ts"]),
         "tick": (["symbol", "ts", "seq"], ["symbol", "ts", "seq"]),
         "orderbook": (["symbol", "ts"], ["symbol", "ts"])}


def resample(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1분봉 → N분봉 (장 시작 09:00 기준으로 묶음, 시각은 묶음의 첫 분)."""
    if bars.empty or minutes == 1:
        return bars
    t = pd.to_datetime(bars["ts"])
    day = t.dt.normalize()
    start = day + pd.Timedelta(hours=9)
    k = ((t - start).dt.total_seconds() // 60 // minutes).astype(int)
    g = bars.assign(_k=start + pd.to_timedelta(k * minutes, unit="m")).groupby("_k", sort=True)
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum")).reset_index()
    out["ts"] = out.pop("_k").dt.strftime("%Y-%m-%d %H:%M:%S")
    return out[["ts", *BAR]]
