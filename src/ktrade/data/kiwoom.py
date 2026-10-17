"""키움 REST API 시세 조회 (kiwoom-client) — 수집기와 장중 시세판이 쓴다.

일봉(ka10081)은 연속조회(cont-yn/next-key)로 과거까지 받고 수정주가(upd_stkpc_tp=1)로 받는다 — 비수정주가는
액면분할·무상증자 날에 가짜 급등락 봉이 생겨 전략이 오작동한다. 지수(ka20006)는 100배 정수로 와서 100으로 나눈다.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from kiwoom_client import KiwoomAPI, extract_records, normalize, to_dataframe


def _paginate(fn, kwargs: dict, stop_before: str | None, max_pages: int, date_field: str = "dt") -> list[dict]:
    out, cont, nxt = [], "N", ""
    for _ in range(max_pages):
        resp = fn(cont_yn=cont, next_key=nxt, **kwargs)
        _, recs = extract_records(resp)
        if not recs:
            break
        out.extend(recs)
        if stop_before and any(str(r.get(date_field, ""))[:8] <= stop_before for r in recs):
            break
        if resp.get("cont-yn", "N") != "Y" or not resp.get("next-key"):
            break
        cont, nxt = "Y", resp["next-key"]
    return out


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce").abs()


def _bars(records: list[dict], div: float = 1.0) -> pd.DataFrame:
    df = to_dataframe(records)
    if df.empty or "dt" not in df:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = df[df["dt"].astype(str).str.len() >= 8]  # 날짜가 빈 레코드는 버린다 (date='nan' 쓰레기 행 방지)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["dt"].astype(str).str[:8], format="%Y%m%d").dt.strftime("%Y-%m-%d"),
        "open": _num(df["open_pric"]) / div, "high": _num(df["high_pric"]) / div, "low": _num(df["low_pric"]) / div,
        "close": _num(df["cur_prc"]) / div, "volume": _num(df["trde_qty"]).fillna(0).astype("int64"),
    }).dropna(subset=["close"])
    return out[out["close"] > 0].drop_duplicates("date").sort_values("date").reset_index(drop=True)


class KiwoomData:
    def __init__(self, cfg: dict, max_pages_daily: int = 20):
        s = cfg["_secrets"]
        if not s["app_key"] or not s["app_secret"]:
            raise RuntimeError("KIWOOM_APP_KEY / KIWOOM_APP_SECRET가 없습니다 — 대시보드 로그인 화면 또는 ktrade login")
        self.api = KiwoomAPI(app_key=s["app_key"], app_secret=s["app_secret"], is_mock=cfg["kiwoom"].get("is_mock", True))
        self.max_pages = max_pages_daily

    def daily(self, symbol: str, since: str | None = None) -> pd.DataFrame:
        recs = _paginate(self.api.chart.stock_daily_chart,
                         {"stk_cd": symbol, "base_dt": datetime.now().strftime("%Y%m%d"), "upd_stkpc_tp": "1"},
                         since.replace("-", "") if since else None, self.max_pages)
        df = _bars(recs)
        return df[df["date"] >= since] if since else df

    def index(self, code: str, since: str | None = None) -> pd.DataFrame:
        recs = _paginate(self.api.chart.industry_daily_chart, {"inds_cd": code, "base_dt": datetime.now().strftime("%Y%m%d")},
                         since.replace("-", "") if since else None, self.max_pages)
        df = _bars(recs, div=100.0)
        return df[df["date"] >= since] if since else df

    def universe(self, markets: tuple[str, ...] = ("0", "10")) -> pd.DataFrame:
        frames = []
        for m in markets:
            df = to_dataframe(self.api.stock_info.stock_info_list(mrkt_tp=m))
            if df.empty:
                continue
            frames.append(pd.DataFrame({"code": df["code"].astype(str), "name": df["name"].astype(str), "market": m,
                                        "market_name": df.get("marketName", pd.Series([""] * len(df))).astype(str),
                                        "state": df.get("state", pd.Series([""] * len(df))).astype(str)}))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["code", "name", "market", "market_name", "state"])

    def minute(self, symbol: str, since: str | None = None, max_pages: int = 3) -> pd.DataFrame:
        """1분봉 (ka10080, 수정주가). since(YYYY-MM-DD)가 담긴 페이지에서 멈춘다. 반환 열: ts, open…volume."""
        recs = _paginate(self.api.chart.stock_minute_chart, {"stk_cd": symbol, "tic_scope": "1", "upd_stkpc_tp": "1"},
                         since.replace("-", "") if since else None, max_pages, date_field="cntr_tm")
        df = to_dataframe(recs)
        if df.empty or "cntr_tm" not in df:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        out = pd.DataFrame({
            "ts": pd.to_datetime(df["cntr_tm"].astype(str), format="%Y%m%d%H%M%S", errors="coerce"),
            "open": _num(df["open_pric"]), "high": _num(df["high_pric"]), "low": _num(df["low_pric"]),
            "close": _num(df["cur_prc"]), "volume": _num(df["trde_qty"]).fillna(0).astype("int64"),
        }).dropna(subset=["ts", "close"])  # 시각이 빈 레코드(거래정지 종목 등)는 버린다
        out["ts"] = out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
        out = out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        return out[out["ts"] >= since] if since else out

    def quote(self, symbol: str) -> dict:
        d = normalize(self.api.stock_info.basic_stock_info(stk_cd=symbol))

        def num(k):
            v = d.get(k)
            return abs(float(v)) if v not in (None, "") else None
        return {"symbol": symbol, "price": num("cur_prc"), "open": num("open_pric"), "high": num("high_pric"),
                "low": num("low_pric"), "volume": num("trde_qty"), "date": ""}

    def quotes(self, symbols: list[str], chunk: int = 50) -> dict[str, dict]:
        """ka10095 지정종목 정보요청 — 여러 종목을 |로 묶어 한 번에. dt(일자)로 휴장 판단."""
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), chunk):
            raw = self.api.stock_info.watchlist_stock_info(stk_cd="|".join(symbols[i:i + chunk]))
            for r in (raw or {}).get("atn_stk_infr", []) or []:
                def num(k):
                    try:
                        v = r.get(k)
                        return abs(float(v)) if v not in (None, "") else None
                    except ValueError:
                        return None
                sym = str(r.get("stk_cd", "")).strip()
                if sym:
                    out[sym] = {"symbol": sym, "price": num("cur_prc"), "open": num("open_pric"), "high": num("high_pric"),
                                "low": num("low_pric"), "volume": num("trde_qty"), "date": str(r.get("dt") or "")}
        return out
