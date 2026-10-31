"""종목 찾기 — 정해진 형식의 검색 조건(spec)을 받아 둔 일봉에 적용한다.

자연어 검색은 AI가 사용자의 문장을 이 spec(JSON)으로 바꾸고, 계산은 여기서 한다 — AI가 만든 코드를 실행하지 않는다.

spec = {
  "markets": ["코스피", "코스닥"],          # 비우면 전부
  "exclude_kinds": true,                    # ETF·ETN·스팩·리츠·우선주·관리종목·거래정지 빼기
  "price": {"min": 1000, "max": 50000},     # 최근 종가(원)
  "cap": {"min": 500, "max": null},         # 시가총액(억 원)
  "rules": [
    {"type": "change", "days": 5, "min": 10, "max": null},        # N일 등락률 %
    {"type": "volume_ratio", "days": 20, "min": 2},              # 오늘 거래량 ÷ 직전 N일 평균
    {"type": "value", "days": 20, "min": 50},                    # N일 평균 거래대금(억 원)
    {"type": "ma", "days": 20, "position": "above", "within_pct": 3},   # N일 이동평균 위/아래 (within_pct = 이평과 거리 % 이내)
    {"type": "high", "days": 60, "within_pct": 3},               # N일 최고가 대비 x% 이내 (0 = 오늘 신고가)
    {"type": "low", "days": 60, "within_pct": 5},                # N일 최저가 대비 x% 이내
    {"type": "ma_cross", "fast": 5, "slow": 20, "direction": "golden", "within_days": 3}
  ],
  "sort": {"by": "change", "days": 5, "desc": true},           # change | volume_ratio | value | cap
  "limit": 30,
  "summary": "사람이 읽는 한 줄 설명"
}
"""
from __future__ import annotations

import pandas as pd

from ktrade.data.store import Store
from ktrade.sdk.context import DataView

RULES = ("change", "volume_ratio", "value", "ma", "high", "low", "ma_cross")
KIND_WORDS = ("ETF", "ETN", "스팩", "리츠")


def _num(v):
    return None if v is None or v == "" else float(v)


def need_bars(spec: dict) -> int:
    n = [int(r.get("days") or 0) for r in spec.get("rules") or []]
    n += [int(r.get("slow") or 0) + int(r.get("within_days") or 0) for r in spec.get("rules") or []]
    n.append(int((spec.get("sort") or {}).get("days") or 0))
    return max(n + [20]) + 2


def check(spec: dict) -> list[str]:
    errs = []
    for r in spec.get("rules") or []:
        if r.get("type") not in RULES:
            errs.append(f"모르는 조건 종류: {r.get('type')}")
        elif r["type"] != "ma_cross" and not 1 <= int(r.get("days") or 0) <= 500:
            errs.append(f"{r['type']}: days는 1~500")
    if need_bars(spec) > 520:
        errs.append("기간이 너무 깁니다 (최대 500일)")
    return errs


def _metrics(g: pd.DataFrame, rules: list[dict], sort: dict) -> dict | None:
    c, v = g["close"].to_numpy(), g["volume"].to_numpy()
    if len(c) < 2:
        return None
    out = {"close": c[-1]}
    for r in rules + ([sort] if sort.get("days") else []):
        t, n = r.get("type") or r.get("by"), int(r.get("days") or 0)
        if t == "change" and len(c) > n:
            out[f"change_{n}"] = (c[-1] / c[-1 - n] - 1) * 100
        elif t == "volume_ratio" and len(v) > n:
            avg = v[-1 - n:-1].mean()
            out[f"volume_ratio_{n}"] = v[-1] / avg if avg > 0 else None
        elif t == "value" and len(c) >= n:
            out[f"value_{n}"] = (c[-n:] * v[-n:]).mean() / 1e8
        elif t == "ma" and len(c) >= n:
            out[f"ma_{n}"] = c[-n:].mean()
        elif t == "high" and len(c) >= n:
            out[f"high_{n}"] = g["high"].to_numpy()[-n:].max()
        elif t == "low" and len(c) >= n:
            out[f"low_{n}"] = g["low"].to_numpy()[-n:].min()
        elif t == "ma_cross":
            f, s_, w = int(r["fast"]), int(r["slow"]), int(r.get("within_days") or 1)
            if len(c) >= s_ + w + 1:
                cs = pd.Series(c)
                diff = (cs.rolling(f).mean() - cs.rolling(s_).mean()).to_numpy()[-(w + 1):]
                sign = 1 if r.get("direction", "golden") == "golden" else -1
                out[f"cross_{f}_{s_}"] = any(sign * diff[i] <= 0 < sign * diff[i + 1] for i in range(w))
    return out


def _passes(m: dict, r: dict) -> bool:
    t, n = r["type"], int(r.get("days") or 0)
    lo, hi = _num(r.get("min")), _num(r.get("max"))

    def between(x):
        return x is not None and (lo is None or x >= lo) and (hi is None or x <= hi)
    if t in ("change", "volume_ratio", "value"):
        return between(m.get(f"{t}_{n}"))
    if t == "ma":
        ma = m.get(f"ma_{n}")
        if ma is None:
            return False
        gap = (m["close"] / ma - 1) * 100
        ok = gap >= 0 if r.get("position", "above") == "above" else gap <= 0
        w = _num(r.get("within_pct"))
        return ok and (w is None or abs(gap) <= w)
    if t == "high":
        h = m.get(f"high_{n}")
        return h is not None and (1 - m["close"] / h) * 100 <= (_num(r.get("within_pct")) or 0)
    if t == "low":
        lw = m.get(f"low_{n}")
        return lw is not None and (m["close"] / lw - 1) * 100 <= (_num(r.get("within_pct")) or 0)
    if t == "ma_cross":
        return bool(m.get(f"cross_{int(r['fast'])}_{int(r['slow'])}"))
    return False


def run(store: Store, spec: dict, asof: str | None = None) -> pd.DataFrame:
    """조건에 맞는 종목 표 (종목코드, 종목명, 시장, 현재가, 계산값들). 받아 둔 일봉 기준."""
    errs = check(spec)
    if errs:
        raise ValueError("; ".join(errs))
    asof = asof or store.last_index_date_before("9999-12-31")
    if not asof:
        raise ValueError("받아 둔 일봉이 없습니다 — 📥 데이터 탭에서 먼저 받으세요")
    view = DataView(store, asof)
    uni = view.universe().set_index("code")
    if spec.get("markets"):
        uni = uni[uni["market"].isin(spec["markets"])]
    if spec.get("exclude_kinds", True):
        name, mname, state = uni["name"].fillna(""), uni["market_name"].fillna(""), uni["state"].fillna("")
        bad = mname.isin(KIND_WORDS) | name.str.contains("스팩|ETN") | name.str.endswith(("우", "우B", "우C")) \
            | state.str.contains("관리종목|거래정지")
        uni = uni[~bad]
    cap = (pd.to_numeric(uni["last_price"], errors="coerce") * pd.to_numeric(uni["list_count"], errors="coerce") / 1e8)
    lo, hi = _num((spec.get("cap") or {}).get("min")), _num((spec.get("cap") or {}).get("max"))
    keep = cap.notna() if lo is not None or hi is not None else pd.Series(True, index=uni.index)
    if lo is not None:
        keep &= cap >= lo
    if hi is not None:
        keep &= cap <= hi
    uni = uni[keep]
    bars = view.daily_all(bars=need_bars(spec))
    bars = bars[bars["symbol"].isin(uni.index)]
    rules, sort = list(spec.get("rules") or []), dict(spec.get("sort") or {})
    plo, phi = _num((spec.get("price") or {}).get("min")), _num((spec.get("price") or {}).get("max"))
    rows = []
    for sym, g in bars.groupby("symbol"):
        if g["date"].iloc[-1] != asof:
            continue
        m = _metrics(g, rules, sort)
        if not m or (plo is not None and m["close"] < plo) or (phi is not None and m["close"] > phi):
            continue
        if all(_passes(m, r) for r in rules):
            rows.append({"종목코드": sym, "종목명": uni.at[sym, "name"], "시장": uni.at[sym, "market"], "현재가": m["close"],
                         "시가총액(억)": round(cap.get(sym), 0) if pd.notna(cap.get(sym)) else None,
                         **{k: round(v, 2) for k, v in m.items() if k != "close" and isinstance(v, float)}})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    by = sort.get("by")
    col = "시가총액(억)" if by == "cap" else f"{by}_{int(sort.get('days') or 0)}" if by else None
    if col in out:
        out = out.sort_values(col, ascending=not sort.get("desc", True))
    return out.head(int(spec.get("limit") or 30)).reset_index(drop=True).rename(columns=_ko)


def _ko(col: str) -> str:
    t, _, n = col.rpartition("_")
    return {"change": f"{n}일 등락률%", "volume_ratio": f"거래량/{n}일평균", "value": f"{n}일 평균거래대금(억)",
            "ma": f"{n}일 이평", "high": f"{n}일 최고가", "low": f"{n}일 최저가"}.get(t, col)
