"""ktrade 대시보드 — `ktrade dashboard`로 띄운다 (pip install "ktrade[dashboard]" 필요).

이 PC에서만 열린다(127.0.0.1). 주문은 하지 않는다.
  · 처음 화면: 키움 앱키 로그인(OS 자격 증명 저장소에 보관) 또는 체험 모드(가상 데이터, 키 없이 둘러보기)
  · 로그인 뒤 DB가 없으면: 몇 종목 시세 받기
  · 본 화면: 매매조건 실험(백테스트) · 계획 · 매매 기록 · 성적 · 종목 차트 · 사용법
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ktrade import __version__, credentials, demo
from ktrade import config as config_mod
from ktrade.config import paths
from ktrade.data.store import Store
from ktrade.engine import report

st.set_page_config(page_title="ktrade", page_icon="📈", layout="wide")
ss = st.session_state
ss.setdefault("mode", None)          # None = 내 데이터, "demo" = 체험 모드
ss.setdefault("page", None)          # None | "login"

base = config_mod.load()
has_key = bool(base["_secrets"]["app_key"] and base["_secrets"]["app_secret"])
is_mock = bool(base["kiwoom"].get("is_mock", True))
SAMPLE = "005930,000660,035420,005380,035720,051910,068270,105560"   # 삼성전자·SK하이닉스·NAVER·현대차·카카오·LG화학·셀트리온·KB금융
UP, DOWN = "#d62728", "#1f5bd6"


# ───────────────────────── 공통 ─────────────────────────
def check_key(app_key: str, app_secret: str, mock: bool) -> float | None:
    """키움에 조회 한 번 (삼성전자 현재가). 주문은 하지 않는다. 실패하면 예외."""
    from ktrade.data.kiwoom import KiwoomData
    cfg = {**base, "_secrets": {"app_key": app_key, "app_secret": app_secret}, "kiwoom": {**base["kiwoom"], "is_mock": mock}}
    return KiwoomData(cfg).quote("005930").get("price")


def sidebar() -> None:
    sb = st.sidebar
    sb.markdown(f"### 📈 ktrade `{__version__}`")
    if ss.mode == "demo":
        sb.info("🧪 체험 모드 — 가상 데이터")
        if sb.button("체험 모드 나가기", width="stretch"):
            ss.mode = None
            st.rerun()
    if has_key:
        sb.success(f"🔑 키움 연결 정보 저장됨 ({'모의투자' if is_mock else '실전'})\n\n앱키 {credentials.mask(base['_secrets']['app_key'])}")
        c1, c2 = sb.columns(2)
        if c1.button("키 변경", width="stretch"):
            ss.page = "login"
            st.rerun()
        if c2.button("로그아웃", width="stretch"):
            credentials.clear()
            st.rerun()
    elif ss.page != "login":
        if sb.button("🔑 키움 로그인", type="primary", width="stretch"):
            ss.page = "login"
            st.rerun()
    if ss.mode != "demo" and ss.page != "login":
        if sb.button("🧪 체험 모드로 둘러보기", width="stretch"):
            ss.mode = "demo"
            st.rerun()
    sb.divider()
    sb.caption("주문은 대시보드에서 하지 않습니다. 매매조건 실험·모의투자 도구이며 수익을 보장하지 않습니다.")
    sb.markdown("[설치·사용 가이드](https://github.com/Re-qwer3618/ktrade-public/blob/main/docs/INSTALL.md)")


def login_form(key_prefix: str = "login") -> None:
    st.markdown("키움 REST API **앱키·시크릿키**를 넣으면 키움에서 시세를 받아 내 데이터로 실험할 수 있습니다. "
                "키는 이 PC의 " + ("Windows 자격 증명 관리자" if credentials.where() == "Windows 자격 증명 관리자"
                                  else credentials.where()) + "에만 저장되고 어디에도 보내지 않습니다(키움 서버 제외).")
    with st.form(key_prefix):
        app_key = st.text_input("앱키 (App Key)")
        app_secret = st.text_input("시크릿키 (Secret Key)", type="password")
        kind = st.radio("키 종류", ["모의투자 키", "실전투자 키"], index=0 if is_mock else 1, horizontal=True,
                        help="키움 사이트에서 모의투자용으로 받은 키인지, 실전 계좌용 키인지. 서로 바꿔 쓰면 연결이 안 됩니다.")
        skip = st.checkbox("연결 확인 없이 저장", help="키움 서버 점검 시간 등 지금 연결 확인이 안 될 때")
        ok = st.form_submit_button("연결 확인 후 저장", type="primary")
    st.caption("실전투자 키여도 기본 설정(broker: paper)에서는 실제 주문이 나가지 않습니다. 대시보드는 조회만 합니다.")
    if not ok:
        return
    app_key, app_secret = app_key.strip(), app_secret.strip()
    if not app_key or not app_secret:
        st.error("앱키와 시크릿키를 모두 넣어 주세요.")
        return
    mock = kind.startswith("모의")
    if not skip:
        with st.spinner("키움에 연결해 보는 중… (삼성전자 현재가 조회만 합니다)"):
            try:
                price = check_key(app_key, app_secret, mock)
            except Exception as exc:
                st.error(f"연결 실패: {str(exc)[:300]}\n\n키 종류(모의/실전)와 키를 다시 확인해 보세요.")
                return
        st.success(f"연결 성공 — 삼성전자 현재가 {price:,.0f}원" if price else "연결 성공")
    where = credentials.save({"KIWOOM_APP_KEY": app_key, "KIWOOM_APP_SECRET": app_secret})
    config_mod.set_is_mock(mock)
    ss.page, ss.mode = None, None
    st.toast(f"키를 저장했습니다 ({where})")
    st.rerun()


def page_login() -> None:
    st.title("🔑 키움 로그인")
    login_form()
    if st.button("← 돌아가기"):
        ss.page = None
        st.rerun()
    key_help()


def key_help() -> None:
    with st.expander("앱키는 어디서 받나요?"):
        st.markdown(
            "1. 키움증권 계좌를 만듭니다 (모의투자로 시작하려면 키움 **모의투자 신청**).\n"
            "2. 키움 REST API 사이트(openapi.kiwoom.com)에 로그인 → **API 사용 신청** → **앱키·시크릿키 발급**.\n"
            "3. 모의투자용 키는 따로 발급되는 경우가 있으니 사이트 안내를 따르세요.\n"
            "4. 받은 키를 위 칸에 넣고 **연결 확인 후 저장**.\n\n"
            "키는 본인만 씁니다. 다른 사람에게 주거나 인터넷에 올리지 마세요.")


def page_welcome() -> None:
    st.title("📈 ktrade")
    st.markdown("#### 나만의 매매조건을 만들어 과거 데이터로 실험하고, 모의투자로 확인하는 국내주식 도구")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**1. 매매조건 작성**\n\n어떤 종목을, 어떤 가격에 살지만 정합니다. 직접 쓰거나 AI(Claude·Gemini)에게 맡겨도 됩니다.")
    c2.markdown("**2. 백테스트**\n\n과거 일봉으로 돌려 승률·평균 수익·시장 대비 성과를 봅니다. 바꿀 때마다 기록이 쌓입니다.")
    c3.markdown("**3. 계획 → 모의투자**\n\n장 마감 후 다음 날 매매 계획을 만들고, 가상 체결·키움 모의투자로 돌려 봅니다.")
    st.divider()
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("🔑 키움 앱키로 시작")
        login_form("welcome_login")
        key_help()
    with right:
        st.subheader("🧪 키 없이 둘러보기")
        st.markdown("가상 종목·가상 시세로 모든 화면을 미리 봅니다.\n\n"
                    "- 예제 매매조건 2개의 백테스트 결과\n- 다음 날 매매 계획\n- 가상 매매 기록과 성적\n- 종목 차트\n\n"
                    "실제 시세가 아니라 숫자에는 의미가 없습니다. 처음 한 번 준비하는 데 30초쯤 걸립니다.")
        if st.button("체험 모드로 둘러보기", type="primary"):
            ss.mode = "demo"
            st.rerun()
    st.caption("키움증권과 무관한 개인 프로젝트입니다. 투자 조언이 아니며 수익을 보장하지 않습니다.")


def ensure_demo() -> dict:
    if not demo.ready(base):
        with st.status("체험 모드 준비 중… (처음 한 번, 30초쯤)", expanded=True) as s:
            demo.build(base, progress=s.write)
            s.update(label="준비 완료", state="complete")
    return demo.config(base)


class _StatusLog(logging.Handler):
    def __init__(self, box):
        super().__init__(logging.INFO)
        self.box = box

    def emit(self, record):
        self.box.write(record.getMessage())


def page_collect() -> None:
    from ktrade.data.collect import collect, parse_codes, resolve_scope
    st.title("📥 시세 받기")
    st.markdown("키움 연결 정보는 저장됐습니다. 이제 실험에 쓸 **일봉 데이터**를 받습니다. 몇 종목이면 몇 초~몇 분이면 끝납니다.")
    syms = st.text_input("받을 종목 코드 (쉼표로 구분)", SAMPLE, help="기본값: 시가총액 상위 몇 종목")
    years = st.slider("기간 (년)", 1, 10, 3)
    st.caption("시장 전체(약 2,500종목)는 처음에 몇 시간 걸립니다 → 바탕화면 'ktrade 명령창'에서 `ktrade collect`.")
    if st.button("받기", type="primary"):
        try:
            codes = parse_codes(syms.split(","))
            scope = resolve_scope(base, symbols=codes, years=years)
        except ValueError as exc:
            st.error(str(exc))
            return
        with st.status(f"{len(codes)}종목 받는 중…", expanded=True) as box:
            h = _StatusLog(box)
            log = logging.getLogger("ktrade")
            log.addHandler(h)
            log.setLevel(logging.INFO)
            try:
                res = collect(Store(paths(base)["db"]), base, scope)
            except Exception as exc:
                box.update(label="실패", state="error")
                st.error(f"받기 실패: {str(exc)[:300]}")
                return
            finally:
                log.removeHandler(h)
            box.update(label=f"완료 — {res['symbols']}종목 · 일봉 {res['bars']:,}개", state="complete")
        if res["failed"]:
            st.warning(f"못 받은 종목: {', '.join(res['failed'])}")
        st.button("대시보드 열기", type="primary", on_click=lambda: None)
    with st.expander("다른 PC에서 받아 둔 DB가 있다면"):
        st.code("ktrade import-db <DB 파일 경로>", language="bash")


# ───────────────────────── 본 화면 ─────────────────────────
def run_backtest(cfg: dict, sid: str, years: float) -> None:
    from ktrade.engine import backtest, experiments, loader
    from ktrade.engine.planner import last_trading_day
    store = Store(paths(cfg)["db"], readonly=True)
    last, first = last_trading_day(store), store.index_range("001")[0]
    if not last:
        st.error("DB에 지수 일봉이 없습니다 — 먼저 시세를 받으세요.")
        return
    start = max(first, (date.fromisoformat(last) - timedelta(days=int(365 * years))).isoformat())
    info = loader.discover(cfg)[sid]
    item = next((e for e in cfg["strategies"]["enabled"] if isinstance(e, dict) and e.get("id") == sid), {})
    cond = loader.load(info, item.get("params") or {})
    rules = loader.exits_for(cfg, info, item.get("exits") or {})
    bar = st.progress(0.0, text=f"백테스트 {sid} {start} ~ {last}")
    res = backtest.run(store, info, cond, rules, start, last, float(cfg["backtest"]["cost_pct"]),
                       int(cfg["risk"]["max_plans_per_strategy"]), float(cfg["backtest"]["oos_ratio"]),
                       progress=lambda i, n: bar.progress(i / n, text=f"백테스트 {i}/{n}일"))
    experiments.save(cfg, info, cond.params, rules, res, note="대시보드에서 실행")
    bar.empty()
    st.toast(f"{sid}: 거래 {res.get('trades', 0)}건 — 기록에 추가했습니다")


def tab_lab(cfg: dict, store: Store) -> None:
    from ktrade.engine import experiments, loader
    found = loader.discover(cfg)
    if not found:
        st.info("매매조건이 없습니다 (ktrade new <이름>).")
        return
    enabled = [e.get("id") for e in cfg["strategies"]["enabled"] if isinstance(e, dict)]
    ids = sorted(found, key=lambda s: (s not in enabled, s))
    sid = st.selectbox("매매조건", ids, format_func=lambda s: f"{s} {found[s].version}" + (" · 켜짐" if s in enabled else ""))
    info = found[sid]
    st.caption(f"{info.description or ''}  ·  위치: {info.source}")
    hist = experiments.history(cfg, sid)
    c1, c2 = st.columns([1, 3])
    years = c1.number_input("기간(년)", 0.5, 10.0, float(cfg["backtest"].get("years") or 3), 0.5)
    if c2.button("▶ 지금 데이터로 백테스트", type="primary"):
        run_backtest(cfg, sid, years)
        st.rerun()
    if not hist:
        st.info("아직 실험 기록이 없습니다. 위 버튼으로 백테스트해 보세요.")
        return
    r = hist[-1]
    m = st.columns(5)
    pct = lambda v, f="{:.1f}%": "-" if v is None else f.format(v)  # noqa: E731
    m[0].metric("거래", f"{r.get('trades', 0)}건")
    m[1].metric("승률", pct(r.get("win_rate")))
    m[2].metric("평균 수익", pct(r.get("avg_ret"), "{:+.2f}%"))
    m[3].metric("손익비(PF)", "-" if not r.get("profit_factor") else f"{r['profit_factor']:.2f}")
    m[4].metric("시장 대비", pct(r.get("avg_excess"), "{:+.2f}%p"))
    st.caption(f"최근 실험 {r['run_at']} · {r.get('start')} ~ {r.get('end')} · {r.get('universe', '')} · 청산: {r.get('exits', '')}"
               + (f" · {r['note']}" if r.get("note") else ""))
    csv = paths(cfg)["experiments"] / sid / f"{r['run_at']}_{r['setup']}.csv"
    if csv.exists():
        t = pd.read_csv(csv, encoding="utf-8-sig", dtype={"symbol": str})
        if not t.empty:
            t = t.sort_values("exit_date")
            fig = go.Figure(go.Scatter(x=t["exit_date"], y=t["ret_pct"].cumsum(), mode="lines", line_color=UP))
            fig.update_layout(height=280, margin=dict(l=10, r=10, t=30, b=10), title="거래별 수익률 누적 (%p, 단순 합)")
            st.plotly_chart(fig, width="stretch")
            with st.expander(f"거래 목록 ({len(t)}건)"):
                st.dataframe(t[["asof", "name", "symbol", "entry_date", "entry", "exit_date", "exit", "reason", "days", "ret_pct"]]
                             .rename(columns={"asof": "선정일", "name": "종목", "symbol": "코드", "entry_date": "매수일",
                                              "entry": "매수가", "exit_date": "매도일", "exit": "매도가", "reason": "청산 사유",
                                              "days": "보유일", "ret_pct": "수익률%"}).round(2), hide_index=True, width="stretch")
    st.markdown("**실험 기록** (조건·파라미터·청산 설정을 바꿀 때마다 한 줄씩)")
    st.dataframe(experiments.table(hist).iloc[::-1], hide_index=True, width="stretch")
    st.caption("일봉 근사·생존편향(상장폐지 종목 없음). 과거 확률이며 미래 수익을 보장하지 않습니다.")


@st.cache_data(ttl=60)
def plan_docs(folder: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in sorted(pd.io.common.Path(folder).glob("*/*.json"), reverse=True):
        if not f.name.startswith("_"):
            out.setdefault(f.parent.name, []).append(json.loads(f.read_text(encoding="utf-8")))
    return out


def candle(store: Store, symbol: str, n: int = 120, mas: tuple[int, ...] = ()) -> go.Figure:
    df = store.query("SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = ? ORDER BY date DESC LIMIT ?",
                     (symbol, n + max(mas or (0,)))).iloc[::-1]
    fig = go.Figure()
    for k, color in zip(mas, ("#ff7f0e", "#2ca02c", "#9467bd")):
        fig.add_trace(go.Scatter(x=df["date"], y=df["close"].rolling(k).mean(), name=f"{k}일선", line=dict(width=1, color=color)))
    df = df.tail(n)
    fig.add_trace(go.Candlestick(x=df["date"], open=df["open"], high=df["high"], low=df["low"], close=df["close"], name="일봉",
                                 increasing_line_color=UP, decreasing_line_color=DOWN))
    fig.update_layout(height=460, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis=dict(type="category", nticks=10))
    return fig


def tab_plan(P: dict, store: Store) -> None:
    docs = plan_docs(str(P["plans"]))
    if not docs:
        st.info("계획이 없습니다. 켠 매매조건이 장 마감 후 다음 날 계획을 만듭니다 (`ktrade plan` 또는 `ktrade daily`).")
        return
    asof = st.selectbox("기준일 (이날 장 마감 기준 → 다음 거래일에 실행)", list(docs))
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
    if not rows:
        st.info(f"{asof}: 조건에 맞는 종목이 없어 계획이 0건입니다.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    pick = st.selectbox("종목 차트", [f"{r['종목']} ({r['코드']})" for r in rows])
    r = rows[[f"{x['종목']} ({x['코드']})" for x in rows].index(pick)]
    fig = candle(store, r["코드"])
    for y, name, color in ((r["손절"], "손절", DOWN), (r["목표"], "목표", UP)):
        if y is not None:
            fig.add_hline(y=y, line_dash="dot", line_color=color, annotation_text=f"{name} {y:,.0f}")
    st.plotly_chart(fig, width="stretch")


def tab_trades(store: Store) -> None:
    t = store.plan_trades(2000)
    if t.empty:
        st.info("아직 매매 기록이 없습니다. 켠 매매조건의 계획을 `ktrade run`(가상 체결·모의투자)이 실행하면 여기 쌓입니다.")
    else:
        t["수익률%"] = ((t["exit_price"] / t["entry_price"] - 1) * 100).round(2)
        st.dataframe(t[["strategy", "version", "name", "symbol", "status", "qty", "entry_date", "entry_price",
                        "exit_date", "exit_price", "exit_reason", "수익률%"]], hide_index=True, width="stretch")
    st.caption("최근 판단 기록")
    st.dataframe(store.query("SELECT timestamp, strategy, symbol, signal, action, detail FROM decisions ORDER BY id DESC LIMIT 200"),
                 hide_index=True, width="stretch")


def tab_perf(P: dict, store: Store) -> None:
    st.subheader("A. 실행된 매매")
    ex = report.executed(store)
    st.dataframe(ex.reset_index() if not ex.empty else pd.DataFrame(), hide_index=True, width="stretch")
    st.subheader("B. 계획 결과 (체결 여부 무관, 일봉 근사)")
    po = report.plan_outcomes(store, P["plans"])
    if not po.empty and po["closed"].any():
        c = po[po["closed"]]
        st.dataframe(c.groupby(["strategy", "version"])["ret_pct"].agg(["count", "mean", lambda s: (s > 0).mean() * 100])
                     .rename(columns={"count": "청산", "mean": "평균%", "<lambda_0>": "승률%"}).round(2).reset_index(),
                     hide_index=True, width="stretch")
    else:
        st.info("아직 청산까지 간 계획이 없습니다.")
    st.caption("일봉 근사·생존편향(상장폐지 종목 없음). 투자 조언이 아닙니다.")


def tab_chart(store: Store) -> None:
    syms = store.symbol_ranges()
    if syms.empty:
        st.info("받아 둔 일봉이 없습니다.")
        return
    labels = [f"{r.name or ''} ({r.symbol})" for r in syms.itertuples()]
    i = st.selectbox("종목", range(len(labels)), format_func=lambda k: labels[k])
    row = syms.iloc[i]
    st.caption(f"{row['start']} ~ {row['end']} · 일봉 {row['bars']}개")
    st.plotly_chart(candle(store, row["symbol"], 250, (20, 60)), width="stretch")


def tab_help(demo_mode: bool) -> None:
    st.markdown(f"""
**ktrade로 하는 일** — 매매조건(종목 선정 + 매수 타이밍)은 내가, 손절·익절·실행·백테스트는 엔진이 합니다.

1. **데이터 받기** — {'키움 로그인 후 대시보드에서 몇 종목, 또는 명령창에서 `ktrade collect`' if demo_mode else '명령창에서 `ktrade collect` (범위는 `~/.ktrade/config.yaml`의 collect)'}
2. **매매조건 만들기** — `ktrade new 내조건` → `~/.ktrade/strategies/내조건/strategy.py` 작성.
   직접 쓰기 어렵다면 `~/.ktrade/docs/LLM_CONTEXT.md`를 Claude·Gemini에 붙여 넣고 원하는 매매기법을 설명하면 코드를 써 줍니다.
3. **실험** — `ktrade test 내조건` 또는 이 화면의 **매매조건 실험** 탭에서 백테스트. 바꿀 때마다 기록이 쌓여 비교됩니다.
4. **켜기** — 마음에 들면 `config.yaml`의 `strategies.enabled`에 추가 → 장 마감 후 다음 날 계획이 생깁니다 (**계획** 탭).
5. **모의 운영** — `ktrade run`: 기본은 가상 체결(paper). 키움 모의투자는 `broker: kiwoom`, `is_mock: true`.
6. **자동 실행** — `ktrade schedule install` (평일 장 마감 후 계획, 장중 실행).

바탕화면의 **ktrade 명령창**에서 명령을 쓰고, 이 대시보드로 결과를 봅니다. 전체 명령: `ktrade --help`
""")


def main_view(cfg: dict, demo_mode: bool) -> None:
    P = paths(cfg)
    if not P["db"].exists():
        st.warning("DB가 없습니다. 먼저 시세를 받으세요.")
        return
    store = Store(P["db"], readonly=True)
    st.title("📈 ktrade" + (" · 체험 모드" if demo_mode else ""))
    if demo_mode:
        st.info("🧪 **체험 모드** — 가상 종목(DEMO01…)·가상 시세입니다. 실제 시세가 아니며 숫자에 의미가 없습니다. "
                "키움 로그인 후에는 내 데이터로 같은 화면을 씁니다.")
    hb = store.get_setting("live.heartbeat", {}) or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("매매 모드", f"{cfg['broker']} · {'모의' if cfg['kiwoom'].get('is_mock', True) else '실전'}")
    c2.metric("마지막 점검 (ktrade run)", hb.get("ts", "기록 없음"))
    c3.metric("마지막 일봉", store.last_index_date_before("9999-12-31") or "-")
    tabs = st.tabs(["🧪 매매조건 실험", "🗓️ 계획", "💹 매매 기록", "📊 성적", "📈 종목 차트", "❓ 사용법"])
    with tabs[0]:
        tab_lab(cfg, store)
    with tabs[1]:
        tab_plan(P, store)
    with tabs[2]:
        tab_trades(store)
    with tabs[3]:
        tab_perf(P, store)
    with tabs[4]:
        tab_chart(store)
    with tabs[5]:
        tab_help(demo_mode)


# ───────────────────────── 화면 고르기 ─────────────────────────
def has_data(cfg: dict) -> bool:
    db = paths(cfg)["db"]
    if not db.exists():
        return False
    try:
        return not Store(db, readonly=True).query("SELECT 1 FROM ohlcv LIMIT 1").empty
    except Exception:
        return False


sidebar()
if ss.page == "login":
    page_login()
elif ss.mode == "demo":
    main_view(ensure_demo(), True)
elif has_data(base):
    main_view(base, False)
elif has_key:
    page_collect()
else:
    page_welcome()
