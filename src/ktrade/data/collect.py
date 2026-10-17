"""데이터 수집 — 종목 목록, 코스피/코스닥 지수 일봉, 종목 일봉, (선택) 1분봉.

무엇을 얼마나 받을지는 사용자가 정한다 (config.yaml `collect`, `ktrade collect` 옵션은 그 실행에서만 덮어씀):
  - 종목: `symbols`(종목코드 목록) / `symbols_file`(한 줄에 하나) — 둘 다 비면 `market_names` 시장 전체
  - 빼기: `exclude`(종목코드), `exclude_states`(관리종목·거래정지 등 상태 글자)
  - 기간: `start`(YYYY-MM-DD, 없으면 오늘부터 `years`년 전) ~ `end`(없으면 오늘)
  - 분봉: `minute.symbols` 종목을 `minute.start`부터 (키움이 주는 범위까지만 — 보통 최근 몇 달)
DB에 이미 있는 구간은 다시 받지 않는다(이어받기). `start`를 예전보다 앞당기면 모자란 앞부분을 채운다.
처음 전 종목(약 2,500개)을 몇 년치 받으면 몇 시간 걸리니, 처음엔 종목을 좁히거나 --limit로 시작하세요.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ktrade.data.store import Store

log = logging.getLogger("ktrade.collect")
INDEX_CODES = ("001", "101")
_CODE = re.compile(r"^[0-9A-Z]{6}$")


@dataclass
class Scope:
    symbols: list[str] | None          # None = market_names 시장 전체
    market_names: list[str]
    exclude: list[str]
    exclude_states: list[str]
    start: str
    end: str | None                    # None = 오늘까지
    limit: int | None = None
    minute_symbols: list[str] = field(default_factory=list)
    minute_start: str | None = None

    def describe(self) -> str:
        who = f"종목 {len(self.symbols)}개 직접 지정" if self.symbols is not None else f"시장 {', '.join(self.market_names)} 전체"
        if self.exclude:
            who += f" (제외 {len(self.exclude)}개)"
        if self.limit:
            who += f" 중 앞 {self.limit}개"
        s = f"{who} · 일봉 {self.start} ~ {self.end or '오늘'}"
        if self.minute_symbols:
            s += f" · 분봉 {len(self.minute_symbols)}종목 {self.minute_start or '받을 수 있는 만큼'}~"
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


def resolve_scope(cfg: dict, symbols=None, symbols_file=None, markets=None, start=None, end=None, years=None,
                  limit=None, minute_symbols=None, minute_start=None) -> Scope:
    """config.yaml collect 설정 + 명령 옵션(준 것만 우선) → 실제로 받을 범위."""
    c = cfg.get("collect", {}) or {}
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
    return Scope(symbols=list(dict.fromkeys(syms)) or None,
                 market_names=list(markets or c.get("market_names") or ["거래소", "코스닥"]),
                 exclude=parse_codes(c.get("exclude")),
                 exclude_states=list(c.get("exclude_states", ["관리종목", "거래정지"]) or []),
                 start=st, end=en, limit=limit,
                 minute_symbols=parse_codes(minute_symbols if minute_symbols is not None else m.get("symbols")),
                 minute_start=_day(minute_start, "--minute-start") or _day(m.get("start"), "collect.minute.start"))


def target_symbols(store: Store, scope: Scope) -> list[str]:
    if scope.symbols is not None:
        syms = list(scope.symbols)
    else:
        names = scope.market_names
        u = store.query(f"SELECT code, state FROM universe WHERE market_name IN ({','.join('?' * len(names))})", tuple(names))
        for bad in scope.exclude_states:
            u = u[~u["state"].fillna("").str.contains(bad, regex=False)]
        syms = sorted(u["code"])
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

    def clip(df, col="date"):
        return df[df[col].astype(str).str[:10] <= scope.end] if scope.end and not df.empty else df

    if universe:
        stats["universe"] = store.upsert_universe(api.universe())
        log.info(f"종목 목록 {stats['universe']}개")
    for code in INDEX_CODES:                # 지수는 백테스트의 거래일 달력·시장 대비 계산에 필요해 항상 같은 기간을 받는다
        since = fetch_from(*store.index_range(code), scope.start, scope.end)
        if since:
            stats["index"] += store.upsert_index(code, clip(api.index(code, since)))
    symbols = target_symbols(store, scope)
    log.info(f"수집 범위: {scope.describe()} → {len(symbols)}종목")
    t0 = time.time()
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
        if i % 50 == 0:
            el = time.time() - t0
            log.info(f"일봉 {i}/{len(symbols)} (남은 예상 {el / i * (len(symbols) - i) / 60:.0f}분)")
    if scope.minute_symbols:
        stats["minute"] = collect_minute(store, cfg, scope.minute_symbols, scope.minute_start, api=api)
    return stats


def collect_minute(store: Store, cfg: dict, symbols: list[str], since: str | None, api=None) -> dict:
    """지정 종목의 1분봉을 since(YYYY-MM-DD)부터(이미 있으면 마지막 날부터). 전 종목 분봉은 너무 오래 걸려 지정 종목만."""
    if api is None:
        from ktrade.data.kiwoom import KiwoomData
        api = KiwoomData(cfg)
    stats = {"symbols": 0, "bars": 0, "failed": []}
    for sym in symbols:
        try:
            last = store.last_minute_ts(sym)
            start = max(since or "", last[:10]) if last else since
            stats["bars"] += store.upsert_minute(sym, api.minute(sym, start or None, max_pages=30))
            stats["symbols"] += 1
        except Exception as exc:
            stats["failed"].append(sym)
            log.warning(f"[{sym}] 분봉 수집 실패: {exc}")
    return stats
