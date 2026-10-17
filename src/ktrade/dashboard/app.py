"""ktrade 대시보드 (보기 전용) — `ktrade dashboard`로 띄운다 (pip install "ktrade[dashboard]" 필요).

이 PC에서만 열린다(127.0.0.1). 데이터는 읽기 전용으로 연다 — 대시보드에서는 주문·설정 변경을 하지 않는다.
"""
from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ktrade import config as config_mod
from ktrade.config import paths
from ktrade.data.store import Store
from ktrade.engine import report

st.set_page_config(page_title="ktrade", page_icon="📈", layout="wide")
cfg = config_mod.load()
P = paths(cfg)
if not P["db"].exists():
    st.warning(f"DB가 없습니다 ({P['db']}). 먼저 `ktrade collect` 또는 `ktrade import-db`.")
    st.stop()
store = Store(P["db"], readonly=True)

st.title("📈 ktrade")
hb = store.get_setting("live.heartbeat", {}) or {}
c1, c2, c3 = st.columns(3)
c1.metric("매매 모드", f"{cfg['broker']} · {'모의' if cfg['kiwoom'].get('is_mock', True) else '실전'}")
c2.metric("마지막 점검 (ktrade run)", hb.get("ts", "기록 없음"))
c3.metric("마지막 일봉", store.last_index_date_before("9999-12-31") or "-")

tab_plan, tab_trade, tab_perf = st.tabs(["🗓️ 계획", "💹 매매 기록", "📊 성적"])


@st.cache_data(ttl=60)
def plan_docs(folder: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in sorted(pd.io.common.Path(folder).glob("*/*.json"), reverse=True):
        if not f.name.startswith("_"):
            out.setdefault(f.parent.name, []).append(json.loads(f.read_text(encoding="utf-8")))
    return out


with tab_plan:
    docs = plan_docs(str(P["plans"]))
    if not docs:
        st.info("계획이 없습니다 (장 마감 후 `ktrade plan` 또는 `ktrade daily`).")
    else:
        asof = st.selectbox("기준일", list(docs))
        trades = store.plan_trades(5000)
        rows = []
        for doc in docs[asof]:
            for p in doc["plans"]:
                t = trades[trades["plan_id"] == p["plan_id"]] if not trades.empty else trades
                status = t.iloc[0]["status"] if t is not None and not t.empty else "-"
                e = p["entry"]
                rows.append({"매매조건": f"{doc['strategy']} {doc['version']}", "순위": p.get("rank"), "종목": p.get("name"),
                             "코드": p["symbol"], "매수 조건": f"시가≥{e['min_open']:,.0f} · {e['after']} 이후 "
                             f"{e['min_price']:,.0f}~{e['max_price']:,.0f}", "손절": p["stop"], "목표": p["target"],
                             "실행": status, "사유": p.get("basis", "")})
        table = pd.DataFrame(rows)
        st.dataframe(table, hide_index=True, width="stretch")
        if not table.empty:
            pick = st.selectbox("종목 차트", [f"{r['종목']} ({r['코드']})" for r in rows])
            r = rows[[f"{x['종목']} ({x['코드']})" for x in rows].index(pick)]
            df = store.query("SELECT date, open, high, low, close FROM ohlcv WHERE symbol = ? ORDER BY date DESC LIMIT 120",
                             (r["코드"],)).iloc[::-1]
            fig = go.Figure(go.Candlestick(x=df["date"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                                           increasing_line_color="#d62728", decreasing_line_color="#1f5bd6"))
            for y, name, color in ((r["손절"], "손절", "#1f5bd6"), (r["목표"], "목표", "#d62728")):
                if y is None:
                    continue
                fig.add_hline(y=y, line_dash="dot", line_color=color, annotation_text=f"{name} {y:,.0f}")
            fig.add_vline(x=asof, line_dash="dot", line_color="gray")
            fig.update_layout(height=460, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10))
            st.plotly_chart(fig, width="stretch")

with tab_trade:
    t = store.plan_trades(2000)
    if t.empty:
        st.info("아직 매매 기록이 없습니다.")
    else:
        t["수익률%"] = (t["exit_price"] / t["entry_price"] - 1) * 100
        st.dataframe(t[["strategy", "version", "name", "symbol", "status", "qty", "entry_date", "entry_price",
                        "exit_date", "exit_price", "exit_reason", "수익률%"]], hide_index=True, width="stretch")
    st.caption("최근 판단 기록")
    st.dataframe(store.query("SELECT timestamp, strategy, symbol, signal, action, detail FROM decisions ORDER BY id DESC LIMIT 200"),
                 hide_index=True, width="stretch")

with tab_perf:
    st.subheader("A. 실행된 매매")
    ex = report.executed(store)
    st.dataframe(ex.reset_index() if not ex.empty else pd.DataFrame(), hide_index=True, width="stretch")
    st.subheader("B. 계획 결과 (체결 여부 무관, 일봉 근사)")
    po = report.plan_outcomes(store, P["plans"])
    if not po.empty:
        c = po[po["closed"]]
        st.dataframe(c.groupby(["strategy", "version"])["ret_pct"].agg(["count", "mean", lambda s: (s > 0).mean() * 100])
                     .rename(columns={"<lambda_0>": "승률%"}).round(2).reset_index(), hide_index=True, width="stretch")
    st.caption("일봉 근사·생존편향(상장폐지 종목 없음). 투자 조언이 아닙니다.")
