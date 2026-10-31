"""데이터 수집 — 종목 목록, 코스피/코스닥 지수 일봉, 종목 일봉, (선택) N분봉·N틱봉.

무엇을 얼마나 받을지는 사용자가 정한다 (config.yaml `collect`, `ktrade collect` 옵션은 그 실행에서만 덮어씀):
  - 종목: `symbols`(종목코드 목록) / `symbols_file`(한 줄에 하나) — 둘 다 비면 `market_names` 시장 전체에서 아래로 고름
      `exclude`(종목코드) `exclude_states`(관리종목·거래정지 등) `exclude_kinds`(ETF·ETN·스팩·리츠·우선주)
      `exclude_warning`(투자유의) `price`(전일 종가 원) `market_cap`(시가총액 억 원)
  - 기간: `start`(YYYY-MM-DD, 없으면 오늘부터 `years`년 전) ~ `end`(없으면 오늘)
  - 봉: `daily`(일봉) · `minute.intervals`(분) · `tick.units`(틱) — 분봉·틱은 `symbols` 목록 또는 all(위 범위 전체)
DB에 이미 있는 구간은 다시 받지 않는다(이어받기). `start`를 예전보다 앞당기면 모자란 앞부분을 채운다.
분봉·틱은 키움이 최근 것만 준다(분봉 몇 달, 틱 며칠) — 그 앞은 매일 받아 쌓아야 생긴다. 호가는 지난 기록을 받을 수 없다.
처음 전 종목(약 2,500개)을 몇 년치 받으면 몇 시간 걸리니, 처음엔 종목을 좁히거나 --limit로 시작하세요.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ktrade.data.market import MINUTES, TICKS
from ktrade.data.store import Store

log = logging.getLogger("ktrade.collect")
INDEX_CODES = ("001", "101")
_CODE = re.compile(r"^[0-9A-Z]{6}$")
_ETF_BRAND = re.compile(r"^(KODEX|TIGER|KBSTAR|RISE|ACE|ARIRANG|HANARO|SOL|PLUS|KOSEF|TIMEFOLIO|KIWOOM|WOORI|BNK|TREX|"
                        r"FOCUS|VITA|1Q|KCGI|ITF|TRUSTON|마이티|히어로즈|파워|에셋플러스|마이다스|UNICORN)\b")
_PREF_NAME = re.compile(r"(\d우|우[BC]|우\(전환\))$")


def _is_pref(code: str, name: str) -> bool:
    """우선주: 보통주 코드는 끝자리가 0 (005930의 우선주 005935, 새 영문 코드는 00088K처럼 끝이 글자)."""
    code = str(code)
    tail_alpha = code[-1:].isalpha() and code[:-1].isdigit()
    return (code.isdigit() and code[-1] != "0") or tail_alpha or bool(_PREF_NAME.search(name))


KINDS = {   # 시장 범위에서 뺄 종류 → 판별 (종목 목록 한 줄: code, name, market_name)
    "ETF": lambda r: "ETF" in r["market_name"] or bool(_ETF_BRAND.match(r["name"])),
    "ETN": lambda r: "ETN" in r["market_name"] or "ETN" in r["name"],
    "스팩": lambda r: "스팩" in r["name"] or "SPAC" in r["name"].upper(),
    "리츠": lambda r: "리츠" in r["market_name"] or "리츠" in r["name"] or "REIT" in r["name"].upper(),
    "우선주": lambda r: _is_pref(r["code"], r["name"]),
}
WARNINGS = {"2": "정리매매", "3": "단기과열", "4": "투자위험", "5": "투자경고"}   # ka10099 orderWarning


@dataclass
class Scope:
    symbols: list[str] | None          # None = market_names 시장 전체
    market_names: list[str]
    exclude: list[str]
    exclude_states: list[str]
    start: str
    end: str | None                    # None = 오늘까지
    limit: int | None = None
    minute_symbols: list[str] | str = field(default_factory=list)    # 목록 또는 "all"(위 범위 전체)
    minute_start: str | None = None
    exclude_kinds: list[str] = field(default_factory=list)
    exclude_warning: bool = False
    price: tuple[float | None, float | None] = (None, None)           # 전일 종가(원)
    market_cap: tuple[float | None, float | None] = (None, None)      # 억 원
    daily: bool = True
    minute_intervals: list[int] = field(default_factory=lambda: [1])
    tick_symbols: list[str] | str = field(default_factory=list)
    tick_units: list[int] = field(default_factory=lambda: [1])
    tick_start: str | None = None
    tick_max_pages: int = 50

    def filters(self) -> list[str]:
        f = []
        if self.exclude_kinds:
            f.append(f"{'·'.join(self.exclude_kinds)} 제외")
        if self.exclude_warning:
            f.append("투자유의 제외")
        for label, (lo, hi), unit in (("종가", self.price, "원"), ("시총", self.market_cap, "억")):
            if lo is not None or hi is not None:
                f.append(f"{label} {'' if lo is None else f'{lo:,.0f}'}~{'' if hi is None else f'{hi:,.0f}'}{unit}")
        return f

    def describe(self) -> str:
        who = f"종목 {len(self.symbols)}개 직접 지정" if self.symbols is not None else f"시장 {', '.join(self.market_names)} 전체"
        extra = ([f"제외 {len(self.exclude)}개"] if self.exclude else []) + (self.filters() if self.symbols is None else [])
        if extra:
            who += f" ({', '.join(extra)})"
        if self.limit:
            who += f" 중 앞 {self.limit}개"
        s = f"{who} · " + (f"일봉 {self.start} ~ {self.end or '오늘'}" if self.daily else "일봉 안 받음")

        def n(x):
            return "범위 전체" if x == "all" else f"{len(x)}종목"
        if self.minute_symbols:
            s += f" · {'·'.join(map(str, self.minute_intervals))}분봉 {n(self.minute_symbols)} {self.minute_start or '받을 수 있는 만큼'}~"
        if self.tick_symbols:
            s += f" · {'·'.join(map(str, self.tick_units))}틱 {n(self.tick_symbols)} {self.tick_start or '받을 수 있는 만큼'}~"
        return s


def parse_codes(items) -> list[str]:
    """'005930', 'A005930', '005930,삼성전자', '5930' 같은 표기를 6자리 코드로. '#'로 시작하는 줄은 주석."""
    out = []
    for x in items or []:
        c = str(x).strip().upper()
        if not c or c.startswith("#"):
            continue
        c = c.replace(",", " ").split()[0]
        c = c[1:] if len(c) == 7 and c.startswith("A") else c
        c = c.zfill(6) if c.isdigit() else c
        if not _CODE.match(c):
            raise ValueError(f"종목코드 형식이 아닙니다: {x!r} (6자리, 예: 005930)")
        out.append(c)
    return list(dict.fromkeys(out))


def _file_codes(path: str | Path, home: Path) -> list[str]:
    p = Path(path).expanduser()
    p = p if p.is_absolute() else home / p
    if not p.exists():
        raise FileNotFoundError(f"종목 파일이 없습니다: {p}")
    return parse_codes(p.read_text(encoding="utf-8-sig").splitlines())


def _day(v, name: str) -> str | None:
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise ValueError(f"{name} 날짜 형식은 YYYY-MM-DD 입니다 (받은 값 {v!r})") from None


def _range(v, name: str) -> tuple[float | None, float | None]:
    v = v or {}
    if isinstance(v, (list, tuple)):
        v = dict(zip(("min", "max"), v))
    lo, hi = (None if v.get(k) in (None, "") else float(v[k]) for k in ("min", "max"))
    if lo is not None and hi is not None and hi < lo:
        raise ValueError(f"{name} 범위가 거꾸로입니다: {lo:g} ~ {hi:g}")
    return lo, hi


def _who(v) -> list[str] | str:
    """분봉·틱 받을 종목: 목록, 또는 all(위 범위 전체)."""
    if isinstance(v, str) and v.strip().lower() in ("all", "전체", "범위"):
        return "all"
    return parse_codes(v.split(",") if isinstance(v, str) else v)


def _units(v, allowed: tuple[int, ...], name: str) -> list[int]:
    out = [int(x) for x in (str(v).split(",") if isinstance(v, (str, int)) else (v or [1]))]
    bad = [x for x in out if x not in allowed]
    if bad:
        raise ValueError(f"{name}는 {', '.join(map(str, allowed))} 중에서 고르세요 (받은 값 {bad})")
    return list(dict.fromkeys(out))


def resolve_scope(cfg: dict, symbols=None, symbols_file=None, markets=None, start=None, end=None, years=None,
                  limit=None, minute_symbols=None, minute_start=None, **over) -> Scope:
    """config.yaml collect 설정 + 명령 옵션(준 것만 우선) → 실제로 받을 범위.
    over(준 것만): exclude_kinds, exclude_warning, price, market_cap, daily, minute_intervals, tick_symbols, tick_units, tick_start."""
    c = cfg.get("collect", {}) or {}
    over = {k: v for k, v in over.items() if v is not None}
    home = Path(cfg.get("_home", "."))
    if symbols is not None or symbols_file is not None:
        syms = parse_codes(symbols) + (_file_codes(symbols_file, home) if symbols_file else [])
    else:
        syms = parse_codes(c.get("symbols")) + (_file_codes(c["symbols_file"], home) if c.get("symbols_file") else [])
    years = float(years if years is not None else c.get("years", 3))
    st = (_day(start, "--start") or _day(c.get("start"), "collect.start")
          or (date.today() - timedelta(days=int(365 * years))).isoformat())
    en = _day(end, "--end") or _day(c.get("end"), "collect.end")
    if en and en < st:
        raise ValueError(f"기간이 거꾸로입니다: {st} ~ {en}")
    m = c.get("minute", {}) or {}
    t = c.get("tick", {}) or {}
    kinds = list(over.get("exclude_kinds", c.get("exclude_kinds", [])) or [])
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValueError(f"exclude_kinds는 {', '.join(KINDS)} 중에서 고르세요 (받은 값 {unknown})")
    return Scope(symbols=list(dict.fromkeys(syms)) or None,
                 market_names=list(markets or c.get("market_names") or ["거래소", "코스닥"]),
                 exclude=parse_codes(c.get("exclude")),
                 exclude_states=list(c.get("exclude_states", ["관리종목", "거래정지"]) or []),
                 start=st, end=en, limit=limit,
                 minute_symbols=_who(minute_symbols if minute_symbols is not None else m.get("symbols")),
                 minute_start=_day(minute_start, "--minute-start") or _day(m.get("start"), "collect.minute.start"),
                 exclude_kinds=kinds,
                 exclude_warning=bool(over.get("exclude_warning", c.get("exclude_warning", False))),
                 price=_range(over.get("price", c.get("price")), "price"),
                 market_cap=_range(over.get("market_cap", c.get("market_cap")), "market_cap"),
                 daily=bool(over.get("daily", c.get("daily", True))),
                 minute_intervals=_units(over.get("minute_intervals", m.get("intervals")), MINUTES, "minute.intervals"),
                 tick_symbols=_who(over.get("tick_symbols", t.get("symbols"))),
                 tick_units=_units(over.get("tick_units", t.get("units")), TICKS, "tick.units"),
                 tick_start=_day(over.get("tick_start"), "--tick-start") or _day(t.get("start"), "collect.tick.start"),
                 tick_max_pages=int(t.get("max_pages", 50) or 50))


def pick(universe: pd.DataFrame, scope: Scope) -> pd.DataFrame:
    """종목 목록에서 시장 범위 조건(시장·상태·종류·투자유의·가격·시가총액)에 맞는 줄만.
    가격·시가총액을 모르는 종목(예전 종목 목록)은 그 조건이 있으면 빠진다 — ktrade collect가 목록을 새로 받으면 채워짐."""
    u = universe.copy()
    for col in ("name", "market_name", "state", "order_warning"):
        u[col] = u[col].fillna("").astype(str) if col in u else ""
    u = u[u["market_name"].isin(scope.market_names)]
    for bad in scope.exclude_states:
        u = u[~u["state"].str.contains(bad, regex=False)]
    for k in scope.exclude_kinds:
        if len(u):
            u = u[~u.apply(KINDS[k], axis=1).astype(bool)]
    if scope.exclude_warning:
        u = u[~u["order_warning"].isin(list(WARNINGS))]
    price = pd.to_numeric(u["last_price"], errors="coerce") if "last_price" in u else pd.Series(float("nan"), index=u.index)
    count = pd.to_numeric(u["list_count"], errors="coerce") if "list_count" in u else pd.Series(float("nan"), index=u.index)
    cap = price * count / 1e8
    keep = pd.Series(True, index=u.index)
    for (lo, hi), v in ((scope.price, price), (scope.market_cap, cap)):
        if lo is not None:
            keep &= v >= lo
        if hi is not None:
            keep &= v <= hi
    return u[keep]


def target_symbols(store: Store, scope: Scope) -> list[str]:
    if scope.symbols is not None:
        syms = list(scope.symbols)
    else:
        syms = sorted(pick(store.universe(), scope)["code"])
    ex = set(scope.exclude)
    syms = [s for s in syms if s not in ex]
    return syms[:scope.limit] if scope.limit else syms


def fetch_from(first: str | None, last: str | None, start: str, end: str | None) -> str | None:
    """이 종목을 어느 날짜부터 받을지. None = 이미 범위를 다 갖고 있음."""
    if not last:
        return start
    if first and first > start:
        return start                       # 앞부분이 모자람 → start부터 (겹치는 날은 덮어씀)
    if last >= (end or date.today().isoformat()):
        return None
    return last                            # 이어받기


def collect(store: Store, cfg: dict, scope: Scope | None = None, universe: bool = True, limit: int | None = None,
            years: float | None = None) -> dict:
    from ktrade.data.kiwoom import KiwoomData
    scope = scope or resolve_scope(cfg, limit=limit, years=years)
    api = KiwoomData(cfg)
    stats = {"scope": scope.describe(), "universe": 0, "index": 0, "symbols": 0, "bars": 0, "up_to_date": 0, "failed": []}
    m = store.market

    def clip(df, col="date"):
        return df[df[col].astype(str).str[:10] <= scope.end] if scope.end and not df.empty else df

    if universe:
        stats["universe"] = store.upsert_universe(api.universe())
        log.info(f"종목 목록 {stats['universe']}개")
    with m.batch():
        for code in INDEX_CODES:            # 지수는 백테스트의 거래일 달력·시장 대비 계산에 필요해 항상 같은 기간을 받는다
            since = fetch_from(*store.index_range(code), scope.start, scope.end)
            if since:
                stats["index"] += store.upsert_index(code, clip(api.index(code, since)))
    symbols = target_symbols(store, scope)
    log.info(f"수집 범위: {scope.describe()} → {len(symbols)}종목")
    if scope.daily:
        t0 = time.time()
        with m.batch():
            for i, sym in enumerate(symbols, 1):
                since = fetch_from(*store.daily_range(sym), scope.start, scope.end)
                if since is None:
                    stats["up_to_date"] += 1
                    continue
                try:
                    stats["bars"] += store.upsert_daily(sym, clip(api.daily(sym, since)))
                    stats["symbols"] += 1
                except Exception as exc:  # 한 종목 실패해도 계속
                    stats["failed"].append(sym)
                    log.warning(f"[{sym}] 일봉 수집 실패: {exc}")
                if i % 200 == 0:
                    m.flush()                # 중간에 꺼져도 받은 만큼은 남게
                if i % 50 == 0:
                    el = time.time() - t0
                    log.info(f"일봉 {i}/{len(symbols)} (남은 예상 {el / i * (len(symbols) - i) / 60:.0f}분)")
    if scope.minute_symbols:
        who = symbols if scope.minute_symbols == "all" else scope.minute_symbols
        stats["minute"] = {f"{iv}m": collect_minute(store, cfg, who, scope.minute_start, api=api, interval=iv)
                           for iv in scope.minute_intervals}
    if scope.tick_symbols:
        who = symbols if scope.tick_symbols == "all" else scope.tick_symbols
        stats["tick"] = {f"{t}t": collect_tick(store, cfg, who, scope.tick_start, api=api, ticks=t,
                                               max_pages=scope.tick_max_pages) for t in scope.tick_units}
    return stats


def collect_minute(store: Store, cfg: dict, symbols: list[str], since: str | None, api=None, interval: int = 1) -> dict:
    """지정 종목의 N분봉을 since(YYYY-MM-DD)부터(이미 있으면 마지막 날부터 — 그날은 다시 받아 채움)."""
    if api is None:
        from ktrade.data.kiwoom import KiwoomData
        api = KiwoomData(cfg)
    stats = {"symbols": 0, "bars": 0, "failed": []}
    with store.market.batch():
        for i, sym in enumerate(symbols, 1):
            try:
                last = store.last_minute_ts(sym, interval)
                start = max(since or "", last[:10]) if last else since
                bars = api.minute(sym, start or None, max_pages=30, interval=interval)
                stats["bars"] += store.upsert_minute(sym, bars, interval)
                stats["symbols"] += 1
            except Exception as exc:
                stats["failed"].append(sym)
                log.warning(f"[{sym}] {interval}분봉 수집 실패: {exc}")
            if i % 50 == 0:
                store.market.flush()
                log.info(f"{interval}분봉 {i}/{len(symbols)}")
    return stats


def collect_tick(store: Store, cfg: dict, symbols: list[str], since: str | None, api=None, ticks: int = 1,
                 max_pages: int = 50) -> dict:
    """지정 종목의 N틱봉. 받은 날은 종목·날짜 단위로 통째 교체(이어받으면 마지막 날을 다시 받아 채움)."""
    if api is None:
        from ktrade.data.kiwoom import KiwoomData
        api = KiwoomData(cfg)
    stats = {"symbols": 0, "bars": 0, "failed": []}
    with store.market.batch():
        for i, sym in enumerate(symbols, 1):
            try:
                days = store.market.tick_days(sym, ticks)
                start = max(since or "", days[-1]) if days else since
                df = api.tick(sym, start or None, max_pages=max_pages, ticks=ticks)
                stats["bars"] += store.market.write_tick(df.assign(symbol=sym), ticks)
                stats["symbols"] += 1
            except Exception as exc:
                stats["failed"].append(sym)
                log.warning(f"[{sym}] {ticks}틱 수집 실패: {exc}")
            if i % 20 == 0:
                store.market.flush()
                log.info(f"{ticks}틱 {i}/{len(symbols)}")
    return stats
