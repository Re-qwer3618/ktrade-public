"""설치·환경 점검 (ktrade doctor) — 새 PC에 설치한 뒤 무엇이 빠졌는지 한 번에 본다. 아무것도 바꾸지 않는다(읽기만)."""
from __future__ import annotations

import platform
import sys
from datetime import date

from ktrade import __version__
from ktrade.config import paths


def checks(cfg: dict, online: bool = True) -> list[tuple[str, bool | None, str]]:
    """[(항목, 통과 True/실패 False/참고 None, 설명)]"""
    p = paths(cfg)
    out: list[tuple[str, bool | None, str]] = []
    out.append(("ktrade·Python", sys.version_info >= (3, 11), f"ktrade {__version__}, Python {platform.python_version()}, {platform.system()}"))
    out.append(("사용자 폴더", (p["home"] / "config.yaml").exists(), f"{p['home']} (없으면 ktrade init)"))
    has_key = bool(cfg["_secrets"]["app_key"] and cfg["_secrets"]["app_secret"])
    out.append(("키움 앱키", has_key, "~/.ktrade/.env 의 KIWOOM_APP_KEY·KIWOOM_APP_SECRET" + ("" if has_key else " — 없음")))
    mode = f"broker={cfg['broker']}, is_mock={cfg['kiwoom'].get('is_mock', True)}"
    out.append(("매매 모드", None if cfg["kiwoom"].get("is_mock", True) or cfg["broker"] == "paper" else False,
                mode + ("" if cfg["kiwoom"].get("is_mock", True) or cfg["broker"] == "paper" else " — 실전 계좌!")))
    if online and has_key:
        try:
            from ktrade.data.kiwoom import KiwoomData
            q = KiwoomData(cfg).quote("005930")
            out.append(("키움 API 연결", bool(q.get("price")), f"삼성전자 현재가 {q.get('price'):,.0f} (조회만)"))
        except Exception as exc:
            out.append(("키움 API 연결", False, f"실패: {str(exc)[:120]}"))
    if p["db"].exists():
        from ktrade.data.store import Store
        s = Store(p["db"], readonly=True)
        last = s.last_index_date_before("9999-12-31")
        n = int(s.query("SELECT COUNT(DISTINCT symbol) AS n FROM ohlcv")["n"].iloc[0])
        stale = last is None or (date.today() - date.fromisoformat(last)).days > 5
        out.append(("시세 데이터", None if n else False, f"{n}종목, 마지막 지수 일봉 {last}" + (" — 오래됨 (ktrade collect)" if stale else "")
                    + " · 받을 범위는 config collect, 받아 둔 범위는 ktrade data"))
    else:
        out.append(("시세 데이터", False, "DB 없음 (ktrade collect 또는 import-db)"))
    try:
        from ktrade.engine import loader
        en = loader.enabled(cfg)
        from ktrade.engine import experiments
        need = cfg["strategies"].get("require_backtest", True)
        desc = []
        for i, c, r in en:
            ok = experiments.has_run(cfg, i.id, experiments.setup_hash(i, c.params, r))
            desc.append(f"{i.id} {i.version}" + ("" if ok else " (백테스트 필요 — ktrade test " + i.id + ")"))
        missing = [d for d in desc if "백테스트 필요" in d]
        out.append(("켠 매매조건", (not (need and missing)) if en else None,
                    ", ".join(desc) or "아직 없음 — 내 조건을 만들고(ktrade new <id>) 백테스트(ktrade test <id>) 뒤 "
                                       "config strategies.enabled에 추가"))
    except Exception as exc:
        out.append(("켠 매매조건", False, str(exc)[:160]))
    plans = sorted(p["plans"].glob("*/")) if p["plans"].exists() else []
    out.append(("최근 계획", None, plans[-1].name if plans else "아직 없음 (장 마감 후 ktrade plan / daily)"))
    from ktrade.engine import schedule
    out.append(("자동 실행 등록", None, schedule.status()))
    return out


def render(rows) -> str:
    mark = {True: "✔", False: "✖", None: "·"}
    return "\n".join(f" {mark[ok]} {name:14} {desc}" for name, ok, desc in rows)
