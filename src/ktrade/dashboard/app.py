"""ktrade 대시보드 — `ktrade dashboard`로 띄운다 (pip install "ktrade[dashboard]" 필요).

이 PC에서만 열린다(127.0.0.1). 대시보드가 직접 주문하지는 않는다 — 운영 탭은 엔진(ktrade hts watch)을 띄우고·멈추고,
설정(config.yaml)·긴급 정지 표시를 남길 뿐이고 주문은 그 엔진이 낸다.
  · 처음 화면: 키움 앱키 로그인(OS 자격 증명 저장소에 보관) 또는 체험 모드(가상 데이터로 백테스트만)
  · 로그인 뒤 DB가 없으면: 몇 종목 시세 받기
  · 본 화면(기본 기능): 운영(조건식 자동매매 설정·시작·중지·실시간) · 계획 · 매매 기록 · 성적 · 영웅문 조건식 · 종목 차트 · 사용법
  · 백테스트(매매조건 실험): 이용 코드로 추가 로그인해야 열린다 (ktrade/access.py). 관리자는 관리자 탭에서 코드 발급
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ktrade import __version__, access, credentials
from ktrade import config as config_mod
from ktrade.config import paths
from ktrade.data.store import Store
from ktrade.engine import report

st.set_page_config(page_title="ktrade", page_icon="📈", layout="wide")
ss = st.session_state
ss.setdefault("page", None)          # None | "login" | "access"

base = config_mod.load()
has_key = bool(base["_secrets"]["app_key"] and base["_secrets"]["app_secret"])
is_mock = bool(base["kiwoom"].get("is_mock", True))
acc = access.current()               # 백테스트 이용 권한 (None = 로그인 안 함)
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
    if acc:
        sb.success(f"🔓 백테스트 정식판\n\n{access.describe(acc)}" + ("\n\n🤖 AI 기능 사용 가능" if access.can_ai(acc) else ""))
        if sb.button("백테스트 로그아웃", width="stretch"):
            access.logout()
            st.rerun()
    elif ss.page != "access":
        if sb.button("🔓 백테스트 정식판 로그인", width="stretch", help="관리자에게 받은 아이디·이용 코드로 로그인하면 내 매매조건 백테스트가 열립니다"):
            ss.page = "access"
            st.rerun()
    sb.divider()
    sb.caption("주문은 운영 탭에서 시작한 엔진이 냅니다(paper·키움 모의, 실전은 동의 뒤). 수익을 보장하지 않습니다.")
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
    st.caption("실전투자 키여도 운영 탭에서 주문 방식을 '키움 계좌'로 고르고 실전 주문에 동의하기 전에는 실제 주문이 나가지 않습니다.")
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
    config_mod.set_kiwoom(is_mock=mock, real_orders=False)          # 키를 바꾸면 실전 주문 동의는 다시 받는다
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


def access_form(key_prefix: str = "access") -> None:
    """백테스트 추가 로그인 — 관리자에게 받은 아이디·이용 코드."""
    with st.form(key_prefix):
        user_id = st.text_input("아이디")
        code = st.text_input("이용 코드", type="password", help="KT1. 으로 시작하는 코드. 관리자에게 받습니다.")
        ok = st.form_submit_button("로그인", type="primary")
    if not ok:
        return
    try:
        info = access.login(user_id, code)
    except ValueError as exc:
        st.error(f"로그인 실패: {exc}")
        return
    ss.page = None
    st.toast(f"백테스트 로그인 — {access.describe(info)}")
    st.rerun()


def page_access() -> None:
    st.title("🔓 백테스트 정식판 로그인")
    st.markdown("내 매매조건 백테스트와 '계획에 쓸 매매조건 켜기'는 **이용 코드**로 추가 로그인해야 씁니다. 키움 앱키 로그인과는 따로입니다.\n\n"
                "코드가 없으면 관리자에게 요청하세요. 코드 없이도 🧪 백테스트 탭의 **체험판**(예제 매매조건 · 조건식 테스트)은 씁니다.")
    access_form()
    if st.button("← 돌아가기"):
        ss.page = None
        st.rerun()


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
    st.markdown("#### 내 키움증권 앱키로 돌리는 국내주식 자동매매")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**1. 키움 앱키 로그인**\n\n내 키움 REST API 앱키를 넣습니다. 키는 이 PC에만 저장됩니다.")
    c2.markdown("**2. 자동매매 (기본 기능)**\n\n영웅문 조건식·손절·익절·분할매매·계좌 한도를 정하고 운영 탭에서 시작합니다. "
                "가상 체결 → 모의투자 → (동의 뒤) 실전.")
    c3.markdown("**3. 백테스트**\n\n**체험판**: 예제 매매조건을 내가 받은 키움 데이터로 실험, 영웅문 조건식은 실제 신호를 쌓아 평가. "
                "**정식판**(이용 코드): 내 매매조건 실험 · 계획에 켜기.")
    st.divider()
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("🔑 키움 앱키로 시작")
        login_form("welcome_login")
    with right:
        st.subheader("처음이라면")
        st.markdown("- **모의투자 키**로 시작하세요 — 실전 키여도 동의 전에는 주문하지 않습니다.\n"
                    "- 로그인하면 바로 운영 탭(조건식 자동매매)을 씁니다. **차트 데이터는 받지 않아도 됩니다** — "
                    "백테스트·계획에 쓸 때만 📥 데이터 탭에서 골라 받습니다.")
        key_help()
    st.caption("키움증권과 무관한 개인 프로젝트입니다. 투자 조언이 아니며 수익을 보장하지 않습니다.")


class _StatusLog(logging.Handler):
    def __init__(self, box):
        super().__init__(logging.INFO)
        self.box = box

    def emit(self, record):
        self.box.write(record.getMessage())


def data_location(cfg: dict) -> None:
    """데이터 폴더 보기·바꾸기 (config data_dir) — 기존 DB·시세를 같이 옮길 수 있다."""
    import shutil
    import string
    cur = paths(cfg)["data"]
    if ss.get("data_dir_msg"):
        st.success(ss.pop("data_dir_msg"))
    free = shutil.disk_usage(cur if cur.exists() else Path(cur.anchor)).free / 2**30
    st.markdown(f"**저장 위치** `{cur}`" + ("" if cfg.get("data_dir") else " (기본값 — 사용자 폴더 안)")
                + f" · 남은 공간 {free:,.1f} GB")
    drives = [f"{d}: {shutil.disk_usage(f'{d}:/').free / 2**30:,.0f} GB" for d in string.ascii_uppercase
              if Path(f"{d}:/").exists()] if Path("C:/").exists() else []
    with st.form("data_dir_form"):
        new = st.text_input("새 폴더 (없으면 만듦)", placeholder=r"예: D:\ktrade-data",
                            help="드라이브별 남은 공간: " + ", ".join(drives) if drives else None)
        move = st.checkbox("지금 있는 데이터도 옮기기 (종목 목록·매매 기록 DB, 시세 폴더, 가상 체결 장부)", value=True)
        go_ = st.form_submit_button("저장 위치 바꾸기")
    if not go_:
        return
    db = paths(cfg)["db"]
    if move and db.exists():
        from ktrade.dashboard import ops
        s = ops.status(Store(db, readonly=True), cfg)
        if s["hts_running"] or s["run_running"]:
            st.error("자동매매(hts watch·ktrade run)가 돌고 있습니다 — 운영 탭에서 멈춘 뒤 옮기세요.")
            return
    try:
        dst, moved = config_mod.set_data_dir(cfg, new, move=move)
    except (ValueError, RuntimeError, OSError) as exc:
        st.error(f"바꾸지 못했습니다: {exc}")
        return
    ss["data_dir_msg"] = (f"저장 위치를 `{dst}`로 바꿨습니다 (config.yaml data_dir)."
                          + (f" 옮긴 것: {', '.join(moved)}" if moved else " 옮긴 데이터 없음")
                          + " — 명령창에서 켜 둔 ktrade run·hts watch가 있으면 다시 시작하세요.")
    st.rerun()


def quick_collect(cfg: dict, key: str = "qc") -> None:
    """몇 종목 일봉 빨리 받기 — 백테스트·계획용 (운영·조건식 매매에는 필요 없음)."""
    from ktrade.data.collect import collect, parse_codes, resolve_scope
    if not has_key:
        st.info("키움 로그인 뒤 받을 수 있습니다.")
        return
    c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="bottom")
    syms = c1.text_input("받을 종목 코드 (쉼표로 구분)", SAMPLE, help="기본값: 시가총액 상위 몇 종목", key=f"{key}_syms")
    years = c2.number_input("기간 (년)", 1, 10, 3, key=f"{key}_years")
    go_ = c3.button("받기", type="primary", key=f"{key}_go", width="stretch")
    st.caption("시장 전체(약 2,500종목)는 처음에 몇 시간 걸립니다 → 아래 받을 범위를 정하고 'ktrade 명령창'에서 `ktrade collect`.")
    if not go_:
        return
    try:
        codes = parse_codes(syms.split(","))
        scope = resolve_scope(cfg, symbols=codes, years=int(years))
    except ValueError as exc:
        st.error(str(exc))
        return
    with st.status(f"{len(codes)}종목 받는 중…", expanded=True) as box:
        h = _StatusLog(box)
        log = logging.getLogger("ktrade")
        log.addHandler(h)
        log.setLevel(logging.INFO)
        try:
            res = collect(Store(paths(cfg)["db"]), cfg, scope)
        except Exception as exc:
            box.update(label="실패", state="error")
            st.error(f"받기 실패: {str(exc)[:300]}")
            return
        finally:
            log.removeHandler(h)
        box.update(label=f"완료 — {res['symbols']}종목 · 일봉 {res['bars']:,}개", state="complete")
    if res["failed"]:
        st.warning(f"못 받은 종목: {', '.join(res['failed'])}")


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


def is_example(info) -> bool:
    from ktrade.engine.loader import bundled_dir
    b = bundled_dir()
    return bool(b) and str(info.source).startswith(str(b))


def tab_trial(cfg: dict, store: Store) -> None:
    """체험판 — 예제 매매조건을 내가 받은 키움 데이터로 · 영웅문 조건식은 실제 신호를 쌓아 평가 (10개까지 골라서)."""
    st.info("**체험판** — 예제 매매조건을 **내가 키움에서 받은 데이터**로 백테스트하고, 영웅문 조건식은 실제 편입 신호를 쌓아 "
            "'그때 샀다면'으로 평가합니다. 내 매매조건 실험·계획에 켜기는 **정식판**(이용 코드).", icon=":material/science:")
    t1, t2, t3 = st.tabs(["예제 매매조건", "영웅문 조건식 테스트", "🔓 정식판 로그인"])
    with t1:
        tab_lab(cfg, store, examples_only=True)
    with t2:
        trial_forward(cfg, store)
    with t3:
        st.markdown("관리자에게 받은 **아이디·이용 코드**로 로그인하면 내 매매조건 백테스트와 '계획에 쓸 매매조건 켜기'가 열립니다.")
        access_form("lab_access")


def trial_forward(cfg: dict, store: Store) -> None:
    """테스트할 조건식 고르기(config forward_test, 운영 조건식과 합쳐 10개까지) → 엔진이 관찰 → 신호별 '그때 샀다면'."""
    from ktrade.dashboard import ops
    from ktrade.engine import forward
    from ktrade.engine.signals import MAX_REALTIME
    s = ops.status(Store(paths(cfg)["db"]), cfg)
    ops_names = [c["name"] for c in cfg.get("hts_conditions") or []]
    cur = list(cfg.get("forward_test") or [])
    st.markdown("키움 조건검색식은 과거 날짜에 어떤 종목이 걸렸는지 주지 않아 **과거 백테스트가 안 됩니다**. 대신 고른 조건식을 "
                "장중에 **주문 없이 관찰**해 편입 신호를 쌓고, 신호 종목의 1분봉으로 '그때 샀다면' 결과를 매깁니다. 쓸수록 정확해집니다.")
    if st.button("키움에서 내 조건식 목록 불러오기", icon=":material/download:", disabled=s["hts_running"] or not has_key,
                 key="trial_list", help="엔진 실행 중에는 못 씀(계정당 웹소켓 하나)"):
        from ktrade.data.hts import HtsClient
        try:
            with st.spinner("조건식 목록 받는 중…"):
                ss.trial_listed = [c.name for c in HtsClient(cfg).list()]
        except Exception as exc:
            st.error(f"목록을 못 받았습니다: {exc}")
    options = list(dict.fromkeys((ss.get("trial_listed") or []) + cur))
    room = MAX_REALTIME - len([n for n in ops_names if n not in options])
    with st.form("trial_form", border=False):
        pick = st.multiselect(f"테스트할 조건식 (최대 {room}개 — 운영 탭 조건식 {len(ops_names)}개와 합쳐 {MAX_REALTIME}개까지)",
                              options, default=[n for n in cur if n in options], max_selections=max(room, 0),
                              placeholder="먼저 위 버튼으로 목록을 불러오세요")
        save = st.form_submit_button("저장", type="primary")
    if save:
        if len(set(pick) | set(ops_names)) > MAX_REALTIME:
            st.error(f"운영 조건식과 합쳐 {MAX_REALTIME}개를 넘습니다.")
        else:
            config_mod.update_sections({"forward_test": pick}, Path(cfg["_home"]) / "config.yaml")
            st.toast("저장했습니다 — 엔진을 (다시) 시작하면 관찰합니다")
            st.rerun()
    c1, c2 = st.columns([2, 1], vertical_alignment="center")
    if s["hts_running"]:
        hb = s["hts"].get("conds") or []
        missing = [n for n in cur if n not in hb]
        c1.success("엔진이 관찰 중입니다" + (f" — 새로 고른 {', '.join(missing)}은(는) 엔진을 다시 시작해야 들어갑니다" if missing else ""))
        if c2.button("엔진 중지", key="trial_stop", disabled=s["stop_pending"]):
            ops.stop_watch(Store(paths(cfg)["db"]))
            st.rerun()
    else:
        c1.caption("엔진이 꺼져 있습니다. 장중(09:00~15:30)에 켜 두면 신호가 쌓입니다. 운영 탭의 조건식 엔진과 같은 엔진입니다.")
        if c2.button("관찰 시작", type="primary", key="trial_start", disabled=not cur):
            res = ops.start_watch(cfg, Store(paths(cfg)["db"]))
            (st.toast if res["ok"] else st.error)("시작했습니다" if res["ok"] else res["reason"])
    if not cur:
        return
    try:
        days = store.query("SELECT DISTINCT date FROM condition_signals ORDER BY date DESC")["date"].tolist()
    except Exception:
        days = []
    if not days:
        st.info("아직 쌓인 신호가 없습니다.")
        return
    n = st.slider("최근 거래일 수", 1, max(len(days), 1), min(len(days), 5), key="trial_days") if len(days) > 1 else 1
    use = sorted(days[:n])
    rep = forward.report(store, cfg, use)
    if rep["missing_bars"]:
        st.warning(f"1분봉이 없어 결과를 못 매긴 신호 {rep['missing_bars']}건")
        if st.button("신호 종목 1분봉 받고 다시 계산", key="trial_collect", disabled=not has_key):
            with st.spinner("1분봉 받는 중…"):
                forward.collect_minutes(Store(paths(cfg)["db"]), cfg, use)
            st.rerun()
    rows = []
    for name in cur:
        r = rep["conds"].get(name)
        if r:
            rows.append(_stat_row(f"{name} — 모든 편입", r["all"]))
            rows.append(_stat_row(f"{name} — 매수 신호", r["buy"]))
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(f"{use[0]} ~ {use[-1]} · '모든 편입' = 편입 묶음마다 첫 편입가에 샀다면, '매수 신호' = 운영 설정(가격·추격 한도 등)을 "
                   "통과했다면. 청산은 조건식 기본 청산 규칙, 비용 차감, 1분봉 근사. 신호별 내역은 🔎 영웅문 조건식 탭.")
    else:
        st.info("고른 조건식의 신호가 이 기간에 없습니다.")


def tab_lab(cfg: dict, store: Store, examples_only: bool = False) -> None:
    from ktrade.engine import loader
    found = loader.discover(cfg)
    if examples_only:
        found = {k: v for k, v in found.items() if is_example(v)}
    if not found:
        st.info("매매조건이 없습니다 (ktrade new <이름>).")
        return
    if not store.market.has_daily():
        st.info("백테스트에는 일봉 데이터가 필요합니다 — 몇 종목만 받아도 됩니다 (📥 데이터 탭에서 범위를 넓힐 수 있음).")
        quick_collect(cfg, "lab_qc")
        return
    enabled = [e.get("id") if isinstance(e, dict) else str(e) for e in cfg["strategies"]["enabled"]]
    if examples_only:
        lab_experiments(cfg, found, enabled, None)
        return
    ai_on = access.can_ai(acc)
    t1, t2, t3 = st.tabs(["실험", "계획에 쓸 매매조건", "🤖 AI 도우미" + ("" if ai_on else " 🔒")])
    with t1:
        lab_experiments(cfg, found, enabled, ai_on)
    with t2:
        lab_enabled(cfg, found)
    with t3:
        if ai_on:
            lab_ai_writer(cfg)
        else:
            st.info("AI 도우미(백테스트 결과 분석 · 매매조건 작성)는 **AI판 구독** 기능입니다 — 구독 결제는 출시 때 열립니다. "
                    "지금은 관리자만 씁니다.", icon=":material/lock:")


def lab_experiments(cfg: dict, found: dict, enabled: list, ai_on: bool | None) -> None:
    from ktrade.engine import experiments
    ids = sorted(found, key=lambda s: (s not in enabled, s))
    sid = st.selectbox("매매조건", ids, key="lab_sid" + ("_ex" if ai_on is None else ""), format_func=lambda s: f"{s} {found[s].version}" + (" · 켜짐" if s in enabled else ""))
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
    if ai_on:
        lab_ai_analysis(cfg, info, r)
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


def hts_names(cfg: dict) -> list[str]:
    """조건식 이름 후보 — 운영·테스트 조건식, 불러온 목록, 이미 조합에 쓴 이름."""
    used = [e.get("hts_filter") for e in cfg["strategies"]["enabled"] if isinstance(e, dict) and e.get("hts_filter")]
    return sorted(set([c["name"] for c in cfg.get("hts_conditions") or []] + list(cfg.get("forward_test") or [])
                      + (ss.get("ops_listed") or []) + (ss.get("trial_listed") or []) + used))


def lab_enabled(cfg: dict, found: dict) -> None:
    """정식판 — 계획에 쓸 매매조건(config strategies.enabled): 켜기 · 순서(우선순위) · 파라미터 · 영웅문 조건식 조합."""
    from ktrade.engine import experiments, loader
    st.markdown("켠 매매조건이 장 마감 후 다음 거래일 계획을 만듭니다 — **순서가 작을수록 우선**(같은 종목을 고르면 위 조건). "
                "지금 설정(코드·파라미터·청산)으로 **백테스트한 기록이 없으면 계획을 만들지 않습니다**. "
                "**조건식 조합**을 고르면 계획을 만들 때 그 영웅문 조건식을 조회해 둘 다에 걸린 종목만 남깁니다(백테스트에는 반영 안 됨).")
    items: dict[str, tuple[int, dict]] = {}
    for i, e in enumerate(cfg["strategies"]["enabled"]):
        e = e if isinstance(e, dict) else {"id": str(e)}
        items[e["id"]] = (i, e)
    rows = []
    for sid, info in found.items():
        i, e = items.get(sid, (None, {}))
        try:
            cond = loader.load(info, e.get("params") or {})
            tested = experiments.has_run(cfg, sid, experiments.setup_hash(info, cond.params, loader.exits_for(cfg, info, e.get("exits") or {})))
        except Exception:
            tested = False
        rows.append({"켜기": i is not None, "순서": (i + 1) if i is not None else None, "매매조건": sid, "버전": info.version,
                     "백테스트": "✅ 함" if tested else "⚠️ 안 함", "파라미터 (JSON)": json.dumps(e.get("params") or {}, ensure_ascii=False),
                     "조건식 조합": e.get("hts_filter") or "", "설명": info.description or ""})
    df = pd.DataFrame(rows).sort_values(["켜기", "순서", "매매조건"], ascending=[False, True, True], na_position="last")
    if st.button("키움에서 조건식 목록 불러오기 (조합용)", icon=":material/download:", key="lab_list", disabled=not has_key):
        from ktrade.data.hts import HtsClient
        try:
            ss.trial_listed = [c.name for c in HtsClient(cfg).list()]
            st.rerun()
        except Exception as exc:
            st.error(f"목록을 못 받았습니다: {exc}")
    edited = st.data_editor(df, key="lab_enabled", hide_index=True, width="stretch", disabled=["매매조건", "버전", "백테스트", "설명"],
                            column_config={
                                "켜기": st.column_config.CheckboxColumn(width="small"),
                                "순서": st.column_config.NumberColumn(min_value=1, step=1, width="small"),
                                "파라미터 (JSON)": st.column_config.TextColumn(help="비우면 {} = 기본값. 예: {\"lookback\": 30}"),
                                "조건식 조합": st.column_config.SelectboxColumn(options=[""] + hts_names(cfg))})
    with st.expander("매매조건별 기본 파라미터"):
        st.json({sid: info.params for sid, info in found.items()}, expanded=False)
    if not st.button("저장", type="primary", key="lab_enabled_save"):
        return
    new, errs = [], []
    on = edited[edited["켜기"]].copy()
    on["순서"] = on["순서"].fillna(999)
    for r in on.sort_values(["순서", "매매조건"]).to_dict("records"):
        item = {k: v for k, v in items.get(r["매매조건"], (0, {}))[1].items() if k not in ("params", "hts_filter")}
        item["id"] = r["매매조건"]
        try:
            params = json.loads(r["파라미터 (JSON)"] or "{}")
            if not isinstance(params, dict):
                raise ValueError("{…} 형태여야 함")
        except ValueError as exc:
            errs.append(f"{r['매매조건']}: 파라미터 JSON 오류 — {exc}")
            continue
        unknown = set(params) - set(found[r["매매조건"]].params)
        if unknown:
            errs.append(f"{r['매매조건']}: 없는 파라미터 {sorted(unknown)}")
        if params:
            item["params"] = params
        if r.get("조건식 조합"):
            item["hts_filter"] = r["조건식 조합"]
        new.append(item)
    if errs:
        st.error("저장하지 않았습니다\n\n" + "\n".join(f"- {e}" for e in errs))
        return
    config_mod.update_sections({"strategies": {**cfg["strategies"], "enabled": new}}, Path(cfg["_home"]) / "config.yaml")
    st.toast(f"저장했습니다 — 켠 매매조건 {len(new)}개 (다음 장 마감 후 계획부터)")
    st.rerun()


def lab_ai_analysis(cfg: dict, info, r: dict) -> None:
    """🤖 이 백테스트 결과를 AI가 해석 (AI판·관리자)."""
    from ktrade import ai
    with st.expander("🤖 AI 분석 — 이 결과를 해석하고 다음 실험을 제안"):
        s = ai.settings(cfg)
        prov = st.radio("AI", list(ai.PROVIDERS), index=list(ai.PROVIDERS).index(s["provider"]), format_func=ai.PROVIDERS.get,
                        horizontal=True, key=f"ai_an_prov_{info.id}")
        key = f"ai_an_{info.id}_{r['run_at']}_{prov}"
        if st.button("분석 요청", key=f"ai_an_go_{info.id}", disabled=not ai.has_key(prov),
                     help=None if ai.has_key(prov) else "AI 도우미 탭에서 API 키를 먼저 넣으세요"):
            csv = paths(cfg)["experiments"] / info.id / f"{r['run_at']}_{r['setup']}.csv"
            trades = pd.read_csv(csv, encoding="utf-8-sig") if csv.exists() else None
            code_f = Path(str(info.source)) / "strategy.py"
            code = code_f.read_text(encoding="utf-8") if code_f.exists() else ""
            try:
                with st.spinner(f"{ai.PROVIDERS[prov]}에 묻는 중… (1분쯤)"):
                    ss[key] = ai.ask(cfg, ai.ANALYSIS_SYSTEM, ai.analysis_prompt(info, r, trades, code), prov)
            except ai.AiError as exc:
                st.error(str(exc))
        if ss.get(key):
            st.markdown(ss[key])
            st.caption(f"{ai.PROVIDERS[prov]} {s['models'][prov]} — AI 해석은 틀릴 수 있습니다. 투자 조언이 아닙니다.")


def lab_ai_writer(cfg: dict) -> None:
    """🤖 매매조건 작성 도우미 — 요청 양식 → AI → strategy.yaml·strategy.py → 저장·검사 (AI판·관리자)."""
    from ktrade import ai
    from ktrade.cli import _docs_src
    from ktrade.engine import loader
    s = ai.settings(cfg)
    with st.expander("AI 연결 설정 — 본인 API 키 (이 PC의 자격 증명 관리자에만 저장)", expanded=not any(map(ai.has_key, ai.PROVIDERS))):
        prov = st.radio("기본 AI", list(ai.PROVIDERS), index=list(ai.PROVIDERS).index(s["provider"]), format_func=ai.PROVIDERS.get,
                        horizontal=True, key="ai_set_prov")
        cols = st.columns(3)
        models = {p: cols[i].text_input(f"{ai.PROVIDERS[p]} 모델", s["models"][p], key=f"ai_model_{p}") for i, p in enumerate(ai.PROVIDERS)}
        for p in ai.PROVIDERS:
            c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="bottom")
            have = ai.has_key(p)
            v = c1.text_input(f"{ai.PROVIDERS[p]} API 키" + (" — 저장됨 " + credentials.mask(credentials.get(ai.KEY_NAME[p])) if have else ""),
                              type="password", key=f"ai_key_{p}", help=ai.KEY_HELP[p])
            if c2.button("키 저장", key=f"ai_save_{p}", disabled=not v):
                credentials.save({ai.KEY_NAME[p]: v})
                st.rerun()
            if c3.button("지우기", key=f"ai_del_{p}", disabled=not have):
                credentials.clear_one(ai.KEY_NAME[p])
                st.rerun()
        if st.button("기본 AI·모델 저장", key="ai_set_save"):
            config_mod.update_sections({"ai": {"provider": prov, "models": models}}, Path(cfg["_home"]) / "config.yaml")
            st.rerun()
        st.caption("요금은 각 회사 API 계정으로 나갑니다. 요청은 그 회사로만 직접 가고 ktrade 서버를 거치지 않습니다.")
    st.markdown("원하는 매매기법을 적으면 AI가 ktrade 규격(`LLM_CONTEXT.md`)대로 **strategy.yaml · strategy.py**를 씁니다. "
                "받은 코드는 검사한 뒤 저장되고, **실험 탭에서 백테스트**한 다음 '계획에 쓸 매매조건'에서 켭니다.")
    mine = {k: v for k, v in loader.discover(cfg).items() if not is_example(v)}
    base_on = st.selectbox("새로 만들기 / 고치기", ["(새 매매조건)"] + sorted(mine), key="ai_w_base")
    with st.form("ai_writer"):
        c1, c2, c3 = st.columns([2, 1, 1])
        sid = c1.text_input("조건 id (영문 소문자·숫자·_)", "" if base_on.startswith("(") else base_on, placeholder="box_breakout")
        n = c2.number_input("하루 최대 후보", 1, 50, 5)
        prov_w = c3.selectbox("AI", list(ai.PROVIDERS), index=list(ai.PROVIDERS).index(s["provider"]), format_func=ai.PROVIDERS.get)
        pick = st.text_area("종목 선정", placeholder="최근 60일 박스권 상단을 종가로 넘고, 거래량이 20일 평균의 1.5~4배, 주가 2천원 이상, 20일 평균 거래대금 5억 이상")
        excl = st.text_input("제외할 종목", "관리종목, 거래정지")
        timing = st.text_input("매수 타이밍", placeholder="다음 날 시가가 돌파일 종가 이상이고 15:10 이후에도 그 위면, +5%까지")
        rank = st.text_input("우선순위", placeholder="거래대금 큰 순")
        exits = st.text_input("(선택) 이 조건만 쓸 손절·익절", placeholder="손절 박스 하단 -1%, 익절 손절폭의 2배")
        combo = st.selectbox("(선택) 영웅문 조건식과 조합", [""] + hts_names(cfg),
                             help="계획을 만들 때 이 조건식에도 걸린 종목만 남깁니다 — 저장할 때 '계획에 쓸 매매조건'의 조건식 조합으로 들어감")
        go_ = st.form_submit_button("AI에게 작성 요청", type="primary", disabled=not any(map(ai.has_key, ai.PROVIDERS)))
    if go_:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", sid or ""):
            st.error("조건 id는 영문 소문자로 시작, 소문자·숫자·_ 2~40자")
            return
        req = ["위 ktrade 매매조건 규격대로 strategy.yaml, strategy.py 두 파일을 써 줘. 손절·익절 코드는 쓰지 마 (엔진 설정 몫).",
               f"- 조건 id: {sid}", f"- 종목 선정: {pick}", f"- 제외할 종목: {excl}", f"- 매수 타이밍: {timing}",
               f"- 하루 최대 후보 수: {n}", f"- 우선순위: {rank}"] + ([f"- 이 조건만 쓸 손절·익절: {exits} → strategy.yaml exits로"] if exits else [])
        if sid in mine:
            folder = Path(str(mine[sid].source))
            for f in ("strategy.yaml", "strategy.py"):
                if (folder / f).exists():
                    req += [f"\n지금 {f} (이걸 고쳐서, version을 하나 올려):", (folder / f).read_text(encoding="utf-8")]
        try:
            with st.spinner(f"{ai.PROVIDERS[prov_w]}가 작성 중… (1~2분)"):
                text = ai.ask(cfg, ai.writer_system(_docs_src()), "\n".join(req), prov_w)
            ss.ai_draft = {"sid": sid, "text": text, "files": ai.parse_files(text), "combo": combo}
        except ai.AiError as exc:
            st.error(str(exc))
    d = ss.get("ai_draft")
    if not d:
        return
    yml, py = d["files"]
    st.markdown(f"**초안: `{d['sid']}`**")
    t1, t2, t3 = st.tabs(["strategy.py", "strategy.yaml", "AI 설명"])
    t1.code(py, language="python")
    t2.code(yml, language="yaml")
    t3.markdown(re.sub(r"```.*?```", "", d["text"], flags=re.S))
    if st.button("저장하고 검사", type="primary", key="ai_w_save"):
        from ktrade.engine.validate import validate
        folder = paths(cfg)["strategies"] / d["sid"]
        backup = {f: (folder / f).read_text(encoding="utf-8") for f in ("strategy.yaml", "strategy.py") if (folder / f).exists()}
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "strategy.yaml").write_text(yml, encoding="utf-8")
        (folder / "strategy.py").write_text(py, encoding="utf-8")
        rep = validate(config_mod.load(), d["sid"])
        if not rep.ok:
            for f, txt in backup.items():                                # 검사에 떨어지면 원래 파일로
                (folder / f).write_text(txt, encoding="utf-8")
            if not backup:
                for f in ("strategy.yaml", "strategy.py"):
                    (folder / f).unlink(missing_ok=True)
            st.error("검사를 통과하지 못해 저장하지 않았습니다 — 아래 결과를 AI에게 보여 주고 다시 요청하세요.")
            st.code(rep.render(), language=None)
            return
        hit = None
        if d.get("combo"):
            en = [e if isinstance(e, dict) else {"id": str(e)} for e in cfg["strategies"]["enabled"]]
            hit = next((e for e in en if e["id"] == d["sid"]), None)
            if hit:
                hit["hts_filter"] = d["combo"]
                config_mod.update_sections({"strategies": {**cfg["strategies"], "enabled": en}}, Path(cfg["_home"]) / "config.yaml")
        ss.pop("ai_draft", None)
        st.success(f"`{folder}`에 저장, 검사 통과 — 실험 탭에서 백테스트한 뒤 '계획에 쓸 매매조건'에서 켜세요."
                   + (f" 조건식 조합 '{d['combo']}'은 켤 때 고르세요." if d.get("combo") and not hit else ""))
        st.code(rep.render(), language=None)


@st.cache_data(ttl=60)
def plan_docs(folder: str) -> dict[str, list[dict]]:
    return report.plan_docs(Path(folder))


MA_COLORS = ("#ff7f0e", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf")


def bars_fig(df: pd.DataFrame, x: str, mas=(), vol_mas=(), label: str = "일봉", n: int | None = None) -> go.Figure:
    """캔들 + 이동평균선(가격) 위, 거래량 + 거래량 이동평균 아래. df는 이동평균 계산용으로 n보다 길게 줘도 됨."""
    from plotly.subplots import make_subplots
    df = df.reset_index(drop=True)
    ma = {k: df["close"].rolling(k).mean() for k in mas}
    vma = {k: df["volume"].rolling(k).mean() for k in vol_mas}
    keep = slice(-n, None) if n else slice(None)
    d = df.iloc[keep]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=d[x], open=d["open"], high=d["high"], low=d["low"], close=d["close"], name=label,
                                 increasing_line_color=UP, decreasing_line_color=DOWN), row=1, col=1)
    for (k, v), color in zip(ma.items(), MA_COLORS):
        fig.add_trace(go.Scatter(x=d[x], y=v.iloc[keep], name=f"{k}선", line=dict(width=1, color=color)), row=1, col=1)
    up = (d["close"] >= d["open"]).map({True: UP, False: DOWN})
    fig.add_trace(go.Bar(x=d[x], y=d["volume"], name="거래량", marker_color=up, opacity=0.6, showlegend=False), row=2, col=1)
    for (k, v), color in zip(vma.items(), MA_COLORS):
        fig.add_trace(go.Scatter(x=d[x], y=v.iloc[keep], name=f"거래량 {k}", line=dict(width=1, color=color, dash="dot")),
                      row=2, col=1)
    fig.update_layout(height=560, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.04))
    fig.update_xaxes(type="category", nticks=10)
    return fig


def candle(store: Store, symbol: str, n: int = 120, mas: tuple[int, ...] | None = None,
           vol_mas: tuple[int, ...] | None = None, cfg: dict | None = None) -> go.Figure:
    ch = (cfg or {}).get("chart", {}) or {}
    mas = tuple(ch.get("ma", (20, 60)) if mas is None else mas)
    vol_mas = tuple(ch.get("volume_ma", ()) if vol_mas is None else vol_mas)
    df = store.market.daily(symbol, last=n + max(mas or (0,)))
    return bars_fig(df, "date", mas, vol_mas, "일봉", n)


def schedule_settings(cfg: dict) -> None:
    """자동 실행 시각·요일 — 저장하면 config.yaml schedule을 바꾸고 작업 스케줄러에 다시 등록한다."""
    from datetime import time as dtime

    from ktrade.engine import schedule as S
    s, reg = S.settings(cfg), S.registered()
    on = {sub: reg.get(name) for name, sub in S.TASKS.items()} if reg is not None else {}
    head = " · ".join(f"{label} {s[sub] if on.get(sub) else '꺼짐'}" for sub, label in (("daily", "계획"), ("run", "실행"))) \
        if reg is not None else "crontab"
    with st.expander(f"⏰ 자동 실행 (장 마감 후 계획 · 장중 실행) — {head}"):
        st.caption("**장 마감 후 계획**(`ktrade daily`): 시세 받기 → 켠 매매조건으로 다음 거래일 계획. "
                   "**장중 실행**(`ktrade run`): 그 계획대로 매수하고 엔진 규칙(손절·익절)으로 매도, 장 마감까지 돎. "
                   "PC가 켜져 있고 로그인돼 있어야 돕니다. 아래 영웅문 조건식 엔진(5단계 시작)과는 따로 돌고, 둘이 겹치면 4단계 우선순위를 따릅니다. "
                   "계획에 쓸 매매조건은 🧪 백테스트(정식판)에서 켭니다.")
        if reg is not None:
            st.caption("작업 스케줄러: " + " · ".join(f"`{n}` {'등록됨' if ok else '없음'}" for n, ok in reg.items()))
        if msg := ss.pop("schedule_msg", None):
            (st.success if msg[0] else st.error)(msg[1])

        def hm(v, default):
            h, m = (v or default).split(":")
            return dtime(int(h), int(m))

        with st.form("schedule_form"):
            c1, c2 = st.columns(2)
            d_on = c1.checkbox("장 마감 후 계획 (daily)", value=bool(s["daily"]))
            d_at = c1.time_input("시각", hm(s["daily"], "16:30"), step=300, key="sch_daily")
            r_on = c2.checkbox("장중 실행 (run)", value=bool(s["run"]))
            r_at = c2.time_input("시각", hm(s["run"], "08:50"), step=300, key="sch_run")
            days = st.multiselect("요일", list(S.DAYS), default=s["days"], format_func=S.DAY_KO.get)
            st.caption("권장: 계획은 장 마감(15:30) 뒤 일봉이 확정된 16:00 이후, 실행은 장 시작(09:00) 전.")
            b1, b2 = st.columns(2)
            save = b1.form_submit_button("저장하고 등록", type="primary")
            off = b2.form_submit_button("자동 실행 모두 끄기")
        if save or off:
            new = {"daily": d_at.strftime("%H:%M") if d_on and not off else None,
                   "run": r_at.strftime("%H:%M") if r_on and not off else None, "days": days}
            errs = S.check(new)
            if errs:
                st.error(" / ".join(errs))
                return
            try:
                config_mod.update_sections({"schedule": new}, Path(cfg["_home"]) / "config.yaml")
                out = S.install(cfg={**cfg, "schedule": new})
                ss["schedule_msg"] = (True, out)
            except Exception as e:                                       # 작업 스케줄러 거부 등 — 화면에 그대로
                ss["schedule_msg"] = (False, f"등록 실패: {e}")
            st.rerun()
        if reg is None:
            st.code("\n".join(S.cron_lines(cfg)) or "(등록할 작업 없음)", language="bash")


def tab_plan(P: dict, store: Store, cfg: dict) -> None:
    """장 마감 후 계획 — 그날 계획마다 실제 결과·이유와 '계획대로였다면'(일봉 근사), 걸러진 계획, 최근 N일 적중, 장중 조건식 매매."""
    from ktrade.engine.risk import PRIORITY_MODES
    docs = plan_docs(str(P["plans"]))
    pr = cfg.get("priority") or {}
    st.caption(f"우선순위: **{PRIORITY_MODES.get(pr.get('mode', 'plan'))}**"
               + (f" (계획 몫 {pr.get('plan_reserve', 0)}자리)" if pr.get("mode", "plan") == "plan" else "")
               + " — 운영 탭 4단계에서 바꿈. 계획 파일은 지우지 않고 날짜별로 계속 쌓입니다.")
    if not docs:
        st.info("계획이 없습니다. 백테스트(정식판)에서 켠 매매조건이 장 마감 후 다음 거래일 계획을 만듭니다 "
                "(운영 탭 ⏰ 자동 실행, 또는 `ktrade daily`). 일봉 데이터가 있어야 합니다.")
    else:
        cost = float(cfg["backtest"]["cost_pct"])
        t1, t2, t3 = st.tabs(["날짜별 계획", "최근 적중", "걸러진 계획"])
        with t1:
            asof = st.selectbox("기준일 (이날 장 마감 기준 → 다음 거래일에 실행)", list(docs), key="plan_asof")
            day = report.exec_day(store, asof)
            t = report.plan_review(store, docs[asof], asof, cost)
            if t.empty:
                st.info(f"{asof}: 조건에 맞는 종목이 없어 계획이 0건입니다 — 이유는 '걸러진 계획' 탭.")
            else:
                n = t["결과"].value_counts()
                c = st.columns(4)
                c[0].metric("계획", len(t), help=f"실행일 {day}")
                c[1].metric("매수함", int(n.get("매수함", 0)))
                c[2].metric("안 삼 · 못 삼", int(n.get("안 삼", 0) + n.get("못 삼", 0)))
                c[3].metric("조건 도달 (계획대로였다면)", int(t["계획대로였다면"].str.startswith("도달").sum()))
                st.dataframe(t, hide_index=True, width="stretch", column_config={
                    "이유": st.column_config.TextColumn(width="large"), "가상 수익%": st.column_config.NumberColumn(format="%.2f")})
                st.caption(f"실행일 {day}. **결과·이유** = 실제 실행(ktrade run) 기록, **계획대로였다면** = 일봉으로 따라가 본 근사"
                           f"(비용 {cost}% 반영, 실제 체결과 다를 수 있음).")
                pick = st.selectbox("종목 차트", [f"{r['종목']} ({r['코드']})" for r in t.to_dict("records")], key="plan_chart")
                r = t.iloc[[f"{x['종목']} ({x['코드']})" for x in t.to_dict("records")].index(pick)]
                if store.market.has_daily():
                    fig = candle(store, r["코드"], cfg=cfg)
                    for y, name, color in ((r["손절"], "손절", DOWN), (r["목표"], "목표", UP)):
                        if y is not None and y == y:
                            fig.add_hline(y=y, line_dash="dot", line_color=color, annotation_text=f"{name} {y:,.0f}", row=1, col=1)
                    st.plotly_chart(fig, width="stretch")
        with t2:
            n_days = st.slider("최근 기준일 수", 5, 120, 20, 5, key="plan_days")
            summ = report.plan_summary(store, docs, n_days, cost)
            if summ.empty:
                st.info("계획이 아직 없습니다.")
            else:
                st.dataframe(summ, hide_index=True, width="stretch")
                st.caption("조건 도달률 = 계획의 매수 조건에 실제로 닿은 비율. 가상 승률·평균 = 닿았다면 엔진 청산 규칙대로 팔았을 때(일봉 근사). "
                           "건수가 적으면 우연일 수 있습니다. 투자 조언이 아닙니다.")
        with t3:
            asof3 = st.selectbox("기준일", list(docs), key="plan_rej_asof")
            rj = report.rejected(P["plans"], asof3)
            if rj.get("skipped"):
                st.warning("**건너뛴 매매조건**\n\n" + "\n".join(f"- `{k}`: {v}" for k, v in rj["skipped"].items()))
            if rj.get("errors"):
                st.error("**오류 난 매매조건**\n\n" + "\n".join(f"- `{k}`: {str(v).splitlines()[-1][:200]}" for k, v in rj["errors"].items()))
            if rj.get("rejected"):
                st.dataframe(pd.DataFrame(rj["rejected"]).rename(columns={"strategy": "매매조건", "symbol": "종목", "reason": "걸러진 이유"}),
                             hide_index=True, width="stretch")
            if not any(rj.get(k) for k in ("skipped", "errors", "rejected")):
                st.caption("이날 걸러진 계획이 없습니다.")
    st.subheader("장중 조건식 매매", divider="gray")
    hts = store.query("SELECT substr(buy_submitted_at, 1, 10) AS day, strategy, name, symbol, status, entry_price, exit_price, "
                      "exit_reason, note, plan FROM plan_trades WHERE strategy LIKE 'hts:%' ORDER BY id DESC LIMIT 2000")
    if hts.empty:
        st.caption("영웅문 조건식(매매 모드)이 장중에 산 기록이 없습니다. 조건식이 건너뛴 이유는 🔎 영웅문 조건식 탭 신호 일지에 있습니다.")
        return
    d = st.selectbox("날짜", sorted(hts["day"].dropna().unique(), reverse=True), key="plan_hts_day")
    h = hts[hts["day"] == d].copy()
    h["조건식"] = h["strategy"].str[4:]
    h["결과"] = h["status"].map(lambda v: report.STATUS_KO.get(v, v))
    h["수익률%"] = ((h["exit_price"] / h["entry_price"] - 1) * 100).round(2)
    h["편입 근거"] = h["plan"].map(lambda v: (json.loads(v) if isinstance(v, str) and v else {}).get("basis", ""))
    st.dataframe(h[["조건식", "symbol", "결과", "entry_price", "exit_price", "exit_reason", "수익률%", "편입 근거"]]
                 .rename(columns={"symbol": "종목", "entry_price": "매수가", "exit_price": "매도가", "exit_reason": "매도 이유"}),
                 hide_index=True, width="stretch")


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


def tab_chart(cfg: dict, store: Store) -> None:
    from ktrade.data.market import MINUTES, TICKS
    m = store.market
    syms = store.symbol_ranges()
    if syms.empty:
        st.info("받아 둔 일봉이 없습니다 — '📥 데이터' 탭에서 받으세요.")
        return
    labels = [f"{r.name or ''} ({r.symbol})" for r in syms.itertuples()]
    c1, c2, c3 = st.columns([3, 2, 2])
    i = c1.selectbox("종목", range(len(labels)), format_func=lambda k: labels[k])
    row = syms.iloc[i]
    sym = row["symbol"]
    have_min, have_tick = m.intervals("minute"), m.intervals("tick")
    frames = ["일봉"] + ([f"{k}분" for k in MINUTES] if 1 in have_min else [f"{k}분" for k in have_min])         + [f"{k}틱" for k in have_tick]
    tf = c2.selectbox("봉", frames, help="분봉은 1분봉만 받아 두면 3·5·10분…으로 묶어 보여 줍니다. 틱은 받아 둔 단위만.")
    ch = cfg.get("chart", {}) or {}
    ma_opts = sorted({3, 5, 10, 20, 60, 120, 240, *ch.get("ma", [])})
    mas = c3.multiselect("이동평균선", ma_opts, default=[k for k in ch.get("ma", [5, 20, 60, 120]) if k in ma_opts])
    vol_mas = st.multiselect("거래량 이동평균", sorted({5, 20, 60, *ch.get("volume_ma", [])}),
                             default=list(ch.get("volume_ma", [20])))
    if tf == "일봉":
        n = st.slider("봉 개수", 60, 750, 250, 10)
        st.caption(f"{row['start']} ~ {row['end']} · 일봉 {row['bars']}개")
        st.plotly_chart(candle(store, sym, n, tuple(mas), tuple(vol_mas)), width="stretch")
    elif tf.endswith("분"):
        k = int(tf[:-1])
        last = m.last_minute_ts(sym, k if k in have_min else 1)
        if not last:
            st.info(f"{row['name'] or sym}의 분봉이 없습니다 — '📥 데이터' 탭에서 분봉 받을 종목에 넣으세요.")
            return
        day = st.date_input("날짜", value=date.fromisoformat(last[:10]), key="chart_min_day").isoformat()
        days = st.slider("며칠치", 1, 10, 1)
        start = (date.fromisoformat(day) - timedelta(days=days * 2)).isoformat()
        df = m.minute(sym, f"{start} 00:00:00", f"{day} 23:59:59", k)
        df = df[df["ts"].str[:10].isin(sorted(df["ts"].str[:10].unique())[-days:])] if len(df) else df
        if df.empty:
            st.info(f"{day}까지 {k}분봉이 없습니다.")
            return
        st.caption(f"{df['ts'].iloc[0]} ~ {df['ts'].iloc[-1]} · {k}분봉 {len(df)}개"
                   + ("" if k in have_min else " (1분봉을 묶음)"))
        st.plotly_chart(bars_fig(df, "ts", tuple(mas), tuple(vol_mas), f"{k}분봉"), width="stretch")
    else:
        k = int(tf[:-1])
        days = m.tick_days(sym, k)
        if not days:
            st.info(f"{row['name'] or sym}의 {k}틱이 없습니다.")
            return
        day = st.selectbox("날짜", days[::-1])
        df = m.tick(sym, day, k)
        df["x"] = df["ts"].str[11:] + "·" + df["seq"].astype(str)
        st.caption(f"{day} · {k}틱 {len(df):,}개")
        st.plotly_chart(bars_fig(df, "x", tuple(mas), tuple(vol_mas), f"{k}틱", n=min(len(df), 2000)), width="stretch")
    if sorted(mas) != sorted(ch.get("ma", [])) or sorted(vol_mas) != sorted(ch.get("volume_ma", [])):
        if st.button("이 이동평균선을 기본으로 저장", key="chart_save_ma"):
            config_mod.update_sections({"chart": {"ma": sorted(mas), "volume_ma": sorted(vol_mas)}},
                                       Path(cfg["_home"]) / "config.yaml")
            st.success("저장했습니다 (config.yaml chart).")


def tab_data(cfg: dict, store: Store) -> None:
    """받아 둔 시세(종류별 용량) · 받을 범위 설정(config.yaml collect) · 대상 종목 미리보기 · 받기."""
    from ktrade.data import collect as C
    from ktrade.data.market import MINUTES, TICKS
    s = store.summary()
    d = s["daily"]
    c1, c2, c3 = st.columns(3)
    c1.metric("일봉", f"{d['symbols']:,}종목", f"{d['from'] or '-'} ~ {d['to'] or '-'}", delta_color="off")
    c2.metric("분봉", ", ".join(f"{k}분 {v['symbols']}종목" for k, v in s["minute"].items()) or "없음")
    c3.metric("틱", ", ".join(f"{k}틱 {n}일" for k, n in s["tick"].items()) or "없음")
    if s["disk"]:
        st.dataframe(pd.DataFrame([{"종류": k, "파일": v["files"], "용량(MB)": v["mb"]} for k, v in s["disk"].items()]),
                     hide_index=True)
    st.caption(f"시세 `{store.market.root}` — 종류별 폴더(daily·minute·tick·orderbook) 안에 기간별 파일. "
               "지난 기간 파일은 바뀌지 않아 새 파일만 복사하면 백업이 됩니다. 호가는 키움이 지난 기록을 주지 않아 받을 수 없습니다.")
    with st.expander("📁 저장 위치 바꾸기"):
        data_location(cfg)

    c = cfg.get("collect", {}) or {}
    m, t = c.get("minute", {}) or {}, c.get("tick", {}) or {}
    st.subheader("받을 범위")
    with st.form("collect_form"):
        mode = st.radio("종목", ["시장 범위에서 고르기", "종목 직접 지정"], horizontal=True,
                        index=1 if c.get("symbols") else 0)
        a1, a2 = st.columns(2)
        markets = a1.multiselect("시장", ["거래소", "코스닥", "ETF"], default=c.get("market_names") or ["거래소", "코스닥"])
        kinds = a2.multiselect("뺄 종류", list(C.KINDS), default=[k for k in c.get("exclude_kinds", []) if k in C.KINDS])
        b1, b2, b3, b4 = st.columns(4)
        pr, cap = c.get("price") or {}, c.get("market_cap") or {}
        p_lo = b1.number_input("종가 이상(원)", min_value=0, value=pr.get("min"), step=500)
        p_hi = b2.number_input("종가 이하(원)", min_value=0, value=pr.get("max"), step=500)
        c_lo = b3.number_input("시가총액 이상(억)", min_value=0, value=cap.get("min"), step=100)
        c_hi = b4.number_input("시가총액 이하(억)", min_value=0, value=cap.get("max"), step=100)
        warn = st.checkbox("투자유의(정리매매·단기과열·투자위험·투자경고) 제외", value=bool(c.get("exclude_warning", True)))
        syms = st.text_input("직접 지정할 종목 (쉼표 구분)", ",".join(c.get("symbols") or []))
        d1, d2, d3 = st.columns(3)
        rolling = date.today() - timedelta(days=int(365 * float(c.get("years", 3))))     # start 비움 = 오늘부터 years년 전
        start = d1.date_input("시작일", value=date.fromisoformat(str(c["start"])) if c.get("start") else rolling)
        to_today = d2.checkbox("오늘까지 (매일 이어받기)", value=not c.get("end"))
        end = d3.date_input("끝일", value=date.fromisoformat(str(c["end"])) if c.get("end") else date.today())
        e1, e2, e3 = st.columns([1, 2, 2])
        daily = e1.checkbox("일봉", value=bool(c.get("daily", True)))
        msym = e2.text_input("분봉 받을 종목 (쉼표 구분, all = 위 범위 전체)",
                             m["symbols"] if isinstance(m.get("symbols"), str) else ",".join(m.get("symbols") or []))
        mint = e3.multiselect("분 단위", list(MINUTES), default=m.get("intervals") or [1])
        f1, f2, f3 = st.columns([1, 2, 2])
        f1.caption("틱은 키움이 최근 며칠만 줍니다")
        tsym = f2.text_input("틱 받을 종목 (쉼표 구분, all = 위 범위 전체)",
                             t["symbols"] if isinstance(t.get("symbols"), str) else ",".join(t.get("symbols") or []))
        tint = f3.multiselect("틱 단위", list(TICKS), default=t.get("units") or [1])
        saved = st.form_submit_button("설정 저장", type="primary")
    if saved:
        def who(v):
            v = v.strip()
            return "all" if v.lower() == "all" else C.parse_codes(v.split(","))
        try:
            new = {**c, "symbols": C.parse_codes(syms.split(",")) if mode == "종목 직접 지정" else [],
                   "market_names": markets, "exclude_kinds": kinds, "exclude_warning": warn,
                   "price": {"min": p_lo, "max": p_hi}, "market_cap": {"min": c_lo, "max": c_hi},
                   "start": None if not c.get("start") and start == rolling else start.isoformat(), "end": None if to_today else end.isoformat(), "daily": daily,
                   "minute": {**m, "symbols": who(msym), "intervals": mint or [1]},
                   "tick": {**t, "symbols": who(tsym), "units": tint or [1]}}
            C.resolve_scope({**cfg, "collect": new})                                   # 틀린 값이면 저장 전에 막음
        except ValueError as exc:
            st.error(str(exc))
        else:
            config_mod.update_sections({"collect": new}, Path(cfg["_home"]) / "config.yaml")
            st.success("저장했습니다 (config.yaml collect). 명령창 ktrade collect · 장 마감 ktrade daily도 이 범위로 받습니다.")
            cfg = {**cfg, "collect": new}

    try:
        scope = C.resolve_scope(cfg)
    except (ValueError, FileNotFoundError) as exc:
        st.error(f"설정 오류: {exc}")
        return
    st.markdown(f"**지금 범위:** {scope.describe()}")
    uni = store.universe()
    if scope.symbols is None:
        if uni.empty:
            st.info("종목 목록이 아직 없습니다 — '지금 받기'를 누르면 목록부터 받습니다.")
        else:
            if uni["last_price"].isna().all() and (scope.price != (None, None) or scope.market_cap != (None, None)):
                st.warning("종목 목록에 가격·시가총액이 없습니다(예전 목록) — '지금 받기'를 한 번 누르면 새 목록으로 채워집니다.")
            picked = C.pick(uni, scope)
            picked = picked[~picked["code"].isin(scope.exclude)]
            st.caption(f"종목 목록 {len(uni):,}개 중 범위에 드는 종목 **{len(picked):,}개**")
            show = picked.assign(cap=pd.to_numeric(picked["last_price"], errors="coerce")
                                 * pd.to_numeric(picked["list_count"], errors="coerce") / 1e8)
            show = show.rename(columns={"code": "코드", "name": "종목", "market_name": "시장", "last_price": "전일 종가",
                                        "cap": "시총(억)", "state": "상태"})
            st.dataframe(show[["코드", "종목", "시장", "전일 종가", "시총(억)", "상태"]].head(300), hide_index=True, height=240,
                         column_config={"전일 종가": st.column_config.NumberColumn(format="%d"),
                                        "시총(억)": st.column_config.NumberColumn(format="%.0f")})
    if not cfg["_secrets"]["app_key"]:
        st.info("받으려면 키움 키가 필요합니다 (사이드바 로그인).")
        return
    st.caption("수백 종목 이상은 오래 걸리니 바탕화면 'ktrade 명령창'에서 ktrade collect 를 권합니다 (중간에 꺼져도 이어받음).")
    if st.button("지금 받기", key="collect_now"):
        with st.status("받는 중…", expanded=True) as box:
            h = _StatusLog(box)
            lg = logging.getLogger("ktrade")
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
            try:
                res = C.collect(Store(paths(cfg)["db"]), cfg, scope)
            except Exception as exc:
                box.update(label="실패", state="error")
                st.error(f"받기 실패: {str(exc)[:300]}")
                return
            finally:
                lg.removeHandler(h)
            box.update(label=f"완료 — 일봉 {res['symbols']}종목 {res['bars']:,}개 (이미 최신 {res['up_to_date']})", state="complete")
        if res["failed"]:
            st.warning(f"못 받은 종목: {', '.join(res['failed'][:30])}")


def _stat_row(label: str, s: dict) -> dict:
    if not s.get("n"):
        return {"구분": label, "건수": 0, "승률%": None, "평균%": None, "중앙값%": None, "PF": None, "최고%": None, "최저%": None}
    return {"구분": label, "건수": s["n"], "승률%": round(s["win_rate"], 1), "평균%": round(s["avg"], 2), "중앙값%": round(s["median"], 2),
            "PF": round(s["pf"], 2) if s.get("pf") else None, "최고%": round(s["best"], 2), "최저%": round(s["worst"], 2)}


def tab_hts(cfg: dict, store: Store) -> None:
    """영웅문 조건식: 설정 · 날짜별 신호 일지 · 성적표(그때 샀다면 / 매수 신호 / 실제 매매)."""
    from ktrade.engine import exits as X
    from ktrade.engine import forward
    from ktrade.engine.signals import cond_exits, load_conditions
    conds = load_conditions(cfg)
    st.markdown("영웅문4 **조건검색(0150)**에서 만든 조건식을 장중 실시간 신호로 씁니다. 실행: 명령창에서 `ktrade hts watch` "
                "(조건식 목록 `ktrade hts list`). 조건식은 키움 서버가 계산하므로 과거 백테스트 대신 **받은 신호로 성적을 쌓습니다**.")
    if conds:
        rows = []
        for c in conds:
            try:
                ex = X.describe(cond_exits(c))
            except ValueError as exc:
                ex = f"설정 오류: {exc}"
            rows.append({"조건식": c["name"], "모드": {"trade": "매매", "watch": "관찰"}.get(c.get("mode"), c.get("mode")),
                         "매수 시간": "~".join(c["buy_window"]), "확인(초)": c.get("confirm_sec", 0), "추격 한도%": c.get("chase_pct"),
                         "하루 한도": c.get("max_buys_per_day"), "청산": ex})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.info("`config.yaml`의 `hts_conditions`에 조건식이 없습니다 (예시는 config.yaml 안 주석).")
    try:
        days = store.query("SELECT DISTINCT date FROM condition_signals ORDER BY date DESC")["date"].tolist()
    except Exception:
        days = []
    if not days:
        st.info("아직 신호 일지가 없습니다 — 장중에 `ktrade hts watch`를 돌리면 쌓입니다.")
        return
    day = st.selectbox("날짜", days, key="hts_day")
    rep = forward.report(store, cfg, [day])
    if rep["missing_bars"]:
        st.warning(f"1분봉이 없어 가상 결과를 못 매긴 신호 {rep['missing_bars']}건 — 아래 버튼으로 받아서 다시 계산하세요.")
        if st.button("신호 종목 1분봉 받고 다시 계산", key="hts_collect"):
            if not cfg["_secrets"].get("app_key"):
                st.error("키움 로그인이 필요합니다.")
            else:
                with st.spinner("1분봉 받는 중…"):
                    n = forward.collect_minutes(Store(paths(cfg)["db"]), cfg, [day])
                st.success(f"{n}종목 받음")
                st.rerun()
    for name, r in rep["conds"].items():
        st.subheader(f"{name} · {'매매' if r['mode'] == 'trade' else '관찰'}")
        cnt = r["counts"]
        cols = st.columns(5)
        cols[0].metric("편입 (종목·묶음)", f"{r['symbols']} · {r['episodes']}", help=f"편입 이벤트 {r['insert_events']}번 — 이탈 뒤 "
                       f"{r['gap_min']:.0f}분 안 재편입은 같은 편입으로 묶음")
        cols[1].metric("매수" if r["mode"] == "trade" else "매수 신호", cnt.get("BUY", 0) + cnt.get("WOULD_BUY", 0))
        cols[2].metric("건너뜀", cnt.get("SKIP", 0))
        cols[3].metric("헛편입·확인 탈락", cnt.get("CONFIRM_FAIL", 0))
        cols[4].metric("이탈", cnt.get("REMOVE", 0))
        st.caption(f"청산 규칙: {r['exits']}" + (f" · 보유 중 {r['open']}종목" if r["open"] else ""))
        st.dataframe(pd.DataFrame([_stat_row("모든 편입 (묶음마다 첫 편입가에 샀다면)", r["all"]), _stat_row("매수 신호 (내 설정 통과)", r["buy"]),
                                   _stat_row("실제 매매 (청산)", r["actual"])]), hide_index=True, width="stretch")
        if not r["table"].empty:
            with st.expander("신호별 가상 결과"):
                t = r["table"].copy()
                t["ret_pct"] = t["ret_pct"].round(2)
                st.dataframe(t.rename(columns={"time": "편입", "symbol": "종목", "action": "판단", "entry": "가격", "exit_ts": "청산 시각",
                                               "exit_price": "청산가", "reason": "사유", "ret_pct": "수익%", "minutes": "보유(분)"}),
                             hide_index=True, width="stretch")
        if not r["trades"].empty:
            with st.expander("실제 매매"):
                t = r["trades"].copy()
                t["수익률%"] = ((t["exit_price"] / t["entry_price"] - 1) * 100).round(2)
                st.dataframe(t[["symbol", "qty", "buy_submitted_at", "entry_price", "exit_price", "exit_reason", "수익률%"]],
                             hide_index=True, width="stretch")
    with st.expander("신호 일지 전체"):
        sig = store.signals(day)
        names = ["(전체)"] + sorted(sig["cond_name"].unique())
        pick = st.selectbox("조건식", names, key="hts_cond")
        if pick != "(전체)":
            sig = sig[sig["cond_name"] == pick]
        st.dataframe(sig[["ts", "cond_name", "symbol", "event", "price", "action", "reason", "mode"]].iloc[::-1], hide_index=True,
                     width="stretch")
    st.caption("가상 결과는 받은 신호에 1분봉으로 엔진 청산 규칙을 적용한 근사(비용 차감)입니다. 미래 수익을 보장하지 않으며 투자 조언이 아닙니다.")


# ───────────────────────── 운영 (영웅문 조건식 자동매매) ─────────────────────────
@st.fragment(run_every="3s")
def ops_status(cfg: dict) -> None:
    """상단 상태 표시줄 — 모드 · 엔진 · 오늘 손익 · 긴급 정지. 3초마다 다시 그림."""
    from ktrade.dashboard import ops
    store = Store(paths(cfg)["db"])
    s = ops.status(store, cfg)
    with st.container(border=True):
        row = st.container(horizontal=True, vertical_alignment="center")
        with row:
            st.badge(s["mode"], icon=":material/warning:" if s["real"] else ":material/science:",
                     color="red" if s["real"] else "blue")
            if s["hts_running"]:
                hb = s["hts"]
                st.badge(f"조건식 엔진 실행 중 · 등록 {len(hb.get('registered') or [])}/{len(hb.get('conds') or [])}"
                         + (f" · 재연결 {hb['restarts']}번" if hb.get("restarts") else ""), icon=":material/sensors:", color="green")
            else:
                st.badge("조건식 엔진 멈춤", icon=":material/sensors_off:", color="gray")
            st.badge("ktrade run 실행 중" if s["run_running"] else "ktrade run 멈춤", icon=":material/schedule:",
                     color="green" if s["run_running"] else "gray")
            if s["halt"]:
                st.badge("계좌 정지", icon=":material/block:", color="red")
        c = st.columns([1, 1, 1, 1.3], vertical_alignment="bottom")
        p = s["pnl"]
        c[0].metric("오늘 손익 (원)", f"{p['total']:+,.0f}")
        c[1].metric("실현", f"{p['realized']:+,.0f}")
        c[2].metric("평가", f"{p['unrealized']:+,.0f}")
        with c[3]:
            if s["halt"]:
                if st.button("정지 해제", icon=":material/lock_open:", width="stretch", key="ops_release"):
                    ops.release_halt(store)
                    st.rerun()
            else:
                with st.popover("긴급 정지", icon=":material/emergency_home:", type="primary", width="stretch"):
                    st.markdown("오늘 **신규 매수를 멈추고 보유 종목을 모두 시장가 근처 지정가로 팝니다** (실행 중인 엔진이 처리).")
                    if st.button("정지하고 전량 매도", type="primary", key="ops_halt", width="stretch"):
                        ops.emergency_stop(store)
                        st.rerun()
        if s["halt"]:
            st.error(s["halt"].get("reason", ""), icon=":material/block:", title="계좌 정지 중 — 오늘 신규 매수 없음")
            if not s["hts_running"] and not s["run_running"]:
                st.caption("실행 중인 엔진이 없어 보유 매도는 엔진을 다음에 켤 때 처리됩니다.")


def ops_settings(cfg: dict) -> None:
    """왼쪽: 1 조건식 → 2 익절·손절·추적 → 3 분할 → 4 리스크 한도 (저장) → 5 시작·중지."""
    from ktrade.cli import _hts_trade_allowed
    from ktrade.dashboard import ops
    store = Store(paths(cfg)["db"])
    s = ops.status(store, cfg)
    running = s["hts_running"]
    st.subheader("설정", divider="gray")
    if running:
        st.info("엔진이 실행 중입니다. 바꾼 설정은 **중지 → 다시 시작**해야 반영됩니다.", icon=":material/info:")

    table = ops.conditions_table(cfg)
    listed = ss.get("ops_listed") or []
    extra = [n for n in listed if n not in set(table["조건식"])]
    if extra:
        add = pd.DataFrame([{**{c: None for c in ops.COND_COLS}, "사용": False, "조건식": n, "모드": "관찰", "매수 시작": "09:00",
                             "매수 끝": "15:20", "하루 매수 한도": 3, "확인(초)": 0, "추격 한도%": 1.0, "이탈 시 매도": False}
                            for n in extra])
        table = pd.concat([table, add], ignore_index=True)
    if st.button("키움에서 내 조건식 목록 불러오기", icon=":material/download:", disabled=running or not has_key,
                 help="영웅문4 조건검색(0150)에 저장한 조건식 이름을 받아옵니다. 엔진 실행 중에는 못 씀(계정당 웹소켓 하나)."):
        from ktrade.data.hts import HtsClient
        try:
            with st.spinner("조건식 목록 받는 중…"):
                ss.ops_listed = [c.name for c in HtsClient(cfg).list()]
            st.rerun()
        except Exception as exc:
            st.error(f"목록을 못 받았습니다: {exc}")

    ex0 = ops.exits_defaults(cfg)
    r0 = cfg["risk"]
    with st.form("ops_form", border=False):
        with st.container(border=True):
            st.markdown("**1 · 조건식** — 쓸 것에 체크, 매매/관찰 선택 (동시 10개까지)")
            edited = st.data_editor(
                table, key="ops_conds", hide_index=True, alt="조건식 설정", num_rows="dynamic", width="stretch",
                column_config={
                    "사용": st.column_config.CheckboxColumn(width="small"),
                    "조건식": st.column_config.TextColumn(required=True),
                    "모드": st.column_config.SelectboxColumn(options=["매매", "관찰"], required=True, width="small"),
                    "매수 시작": st.column_config.TextColumn(width="small", validate=r"^\d\d:\d\d$"),
                    "매수 끝": st.column_config.TextColumn(width="small", validate=r"^\d\d:\d\d$"),
                    "하루 매수 한도": st.column_config.NumberColumn(min_value=0, step=1, width="small"),
                    "확인(초)": st.column_config.NumberColumn(min_value=0, step=5, width="small",
                                                           help="편입 뒤 N초 동안 이탈 없고 편입가 이상이면 매수 (0 = 즉시)"),
                    "추격 한도%": st.column_config.NumberColumn(min_value=0.0, step=0.5, width="small"),
                    "이탈 시 매도": st.column_config.CheckboxColumn(width="small"),
                })
        with st.container(border=True):
            st.markdown("**2 · 익절 · 손절 · 추적 손절** — 모든 조건식 공통, 평균 매수가 기준 (0 = 끔)")
            a, b, c = st.columns(3)
            stop_v = ex0["stop"].get("value") if ex0["stop"]["type"] == "pct" else 0
            stop_pct = a.number_input("손절 %", 0.0, 30.0, float(stop_v or 0), 0.5)
            tp_v = ex0["take_profit"].get("value") if ex0["take_profit"]["type"] == "pct" else 0
            tp_pct = b.number_input("익절 %", 0.0, 100.0, float(tp_v or 0), 0.5)
            hold = c.number_input("보유 일수", 1, 60, int(ex0["max_hold_days"]), 1, help="1 = 당일 15:15 이후 청산")
            a, b = st.columns(2)
            t = ex0.get("trailing") or {}
            ts_act = a.number_input("추적 손절 발동 +%", 0.0, 50.0, float(t.get("activate_pct") or 0), 0.5,
                                    help="평균 매수가 대비 이만큼 오른 뒤부터 추적")
            ts_trail = b.number_input("추적 손절 고점 대비 -%", 0.0, 30.0, float(t.get("trail_pct") or 0), 0.5, help="0 = 추적 손절 끔")
            intraday = st.toggle("손절은 닿는 즉시 (장중)", ex0["stop_check"] == "intraday", help="끄면 15:10 이후 종가 근처에서만 판단")
        with st.container(border=True):
            st.markdown("**3 · 분할매수 · 분할익절** — 비우면 한 번에")
            a, b = st.columns(2)
            sin0 = pd.DataFrame([{"비율%": s_["pct"], "하락%": s_.get("drop_pct", 0)} for s_ in ex0.get("scale_in") or []],
                                columns=["비율%", "하락%"])
            sout0 = pd.DataFrame([{"비율%": s_["pct"], "상승%": s_["gain_pct"]} for s_ in ex0.get("scale_out") or []],
                                 columns=["비율%", "상승%"])
            with a:
                st.caption("분할매수 — 비율 합 100")
                sin = st.data_editor(sin0, key="ops_sin", alt="분할매수 차수", num_rows="dynamic", hide_index=True, width="stretch",
                                     column_config={"비율%": st.column_config.NumberColumn(min_value=0, max_value=100, step=5),
                                                    "하락%": st.column_config.NumberColumn(min_value=0, step=0.5,
                                                                                          help="첫 매수가 대비 이만큼 떨어지면 이 차수 매수")})
            with b:
                st.caption("분할익절 — 비율 합 100 이하")
                sout = st.data_editor(sout0, key="ops_sout", alt="분할익절 차수", num_rows="dynamic", hide_index=True, width="stretch",
                                      column_config={"비율%": st.column_config.NumberColumn(min_value=0, max_value=100, step=5),
                                                     "상승%": st.column_config.NumberColumn(min_value=0, step=0.5,
                                                                                 help="평균 매수가 대비 이만큼 오르면 체결 수량의 비율%만큼 매도")})
        with st.container(border=True):
            st.markdown("**4 · 리스크 한도** — 계좌 전체 (0 = 끔)")
            a, b, c = st.columns(3)
            amount = a.number_input("종목당 금액 (원)", 0, 1_000_000_000, int(r0.get("order_amount") or 0), 100_000,
                                    help=f"0 = 시작 총자본 × {r0['max_position_pct']}%")
            maxpos = b.number_input("동시 보유 종목", 1, 50, int(r0["max_concurrent_positions"]), 1)
            maxnew = c.number_input("하루 신규 매수", 1, 200, int(r0["max_new_orders_per_day"]), 1)
            a, b, c = st.columns(3)
            profit = a.number_input("하루 수익 목표 (원)", 0, 1_000_000_000, int(r0.get("daily_profit_target") or 0), 10_000,
                                    help="닿으면 그날 신규 매수 중지 + 보유 전량 매도")
            loss = b.number_input("하루 손실 한도 (원)", 0, 1_000_000_000, int(r0.get("daily_loss_limit") or 0), 10_000,
                                  help="닿으면 그날 신규 매수 중지 + 보유 전량 매도")
            timeout = c.number_input("미체결 취소 (초)", 0, 3600, int(r0.get("order_timeout_sec") or 0), 5,
                                     help="0 = 다음 거래일까지 기다림")
            a, b, c = st.columns(3)
            win = r0.get("buy_window") or ["", ""]
            w0 = a.text_input("신규 매수 시작", win[0] or "", placeholder="09:00", help="비우면 조건식별 매수 시간대만")
            w1 = b.text_input("신규 매수 끝", win[1] or "", placeholder="14:30")
            flat = c.text_input("시간 일괄청산", r0.get("flatten_at") or "", placeholder="15:15", help="비우면 끔")
            from ktrade.engine.risk import PRIORITY_MODES
            p0 = {**config_mod.DEFAULTS["priority"], **(cfg.get("priority") or {})}
            sp0 = {**config_mod.DEFAULTS["priority"]["split"], **(p0.get("split") or {})}
            st.markdown("**우선순위** — 장 마감 후 계획(ktrade run)과 조건식이 같은 계좌를 쓸 때")
            a, b = st.columns([2, 1])
            pmode = a.radio("우선순위", list(PRIORITY_MODES), index=list(PRIORITY_MODES).index(p0.get("mode", "plan")),
                            format_func=PRIORITY_MODES.get, horizontal=True, label_visibility="collapsed",
                            help="계획 우선: 오늘 계획 종목은 조건식이 사지 않고, 계획 몫 자리를 남겨 둠")
            reserve = b.number_input("계획 몫 (자리)", 0, 50, int(p0.get("plan_reserve") or 0), 1,
                                     help="계획 우선일 때 동시 보유·하루 매수 한도에서 계획에 남겨 둘 수 (오늘 계획 수까지만)")
            a, b, c, d = st.columns(4)
            sp_pp = a.number_input("나눠 쓰기: 계획 보유", 0, 50, int(sp0["plan"].get("positions") or 0), 1)
            sp_po = b.number_input("계획 하루 매수", 0, 200, int(sp0["plan"].get("orders") or 0), 1)
            sp_hp = c.number_input("조건식 보유", 0, 50, int(sp0["hts"].get("positions") or 0), 1)
            sp_ho = d.number_input("조건식 하루 매수", 0, 200, int(sp0["hts"].get("orders") or 0), 1)
        saved = st.form_submit_button("설정 저장", icon=":material/save:", type="primary", width="stretch")
    if saved:
        try:
            ex = ops.exits_from_form(stop_pct, intraday, tp_pct, ts_act, ts_trail, hold, sin, sout)
            conds = ops.conditions_from_table(edited, cfg.get("hts_conditions") or [], ex)
            risk = {"order_amount": amount or None, "max_concurrent_positions": int(maxpos), "max_new_orders_per_day": int(maxnew),
                    "daily_profit_target": profit or None, "daily_loss_limit": loss or None, "order_timeout_sec": timeout or None,
                    "buy_window": [w0.strip(), w1.strip()] if w0.strip() and w1.strip() else None,
                    "flatten_at": flat.strip() or None}
            prio = {"mode": pmode, "plan_reserve": int(reserve),
                    "split": {"plan": {"positions": int(sp_pp), "orders": int(sp_po)}, "hts": {"positions": int(sp_hp), "orders": int(sp_ho)}}}
            errs = ops.save_settings(cfg, conds, risk, prio)
        except (ValueError, KeyError) as exc:
            errs = [str(exc)]
        if errs:
            st.error("\n".join(f"- {e}" for e in errs), title="저장하지 않았습니다")
        else:
            st.toast("저장했습니다 (config.yaml, 바꾸기 전 파일은 config.yaml.bak)", icon=":material/check:")
            st.rerun()

    with st.container(border=True):
        st.markdown("**5 · 시작 / 중지**")
        broker_ui(cfg, running)
        allow, how = _hts_trade_allowed(cfg)
        trade = [c["name"] for c in cfg.get("hts_conditions") or [] if c.get("mode") == "trade"]
        st.caption(f"매매 조건식 {len(trade)}개 · " + (f"주문: {how}" if allow else f"주문 안 함 — {how} (관찰로 동작)")
                   + " · 같은 키움 키로 실시간 시세를 쓰는 다른 프로그램이 켜져 있으면 서로 끊깁니다.")
        a, b = st.columns(2)
        if a.button("시작", icon=":material/play_arrow:", type="primary", width="stretch", disabled=running, key="ops_start"):
            res = ops.start_watch(cfg, store)
            if res["ok"]:
                st.toast(f"시작했습니다 (pid {res['pid']}) — 등록까지 몇 초 걸립니다", icon=":material/play_arrow:")
            else:
                st.error(res["reason"])
        if b.button("중지", icon=":material/stop:", width="stretch", disabled=not running or s["stop_pending"], key="ops_stop"):
            ops.stop_watch(store)
            st.toast("중지 요청 — 5초 안에 끝납니다. 보유 종목은 다음 실행 때 계속 관리됩니다.", icon=":material/stop:")


def broker_ui(cfg: dict, running: bool) -> None:
    """주문 방식(가상 체결 / 내 키움 계좌) + 실전 키일 때 실전 주문 동의."""
    kinds = {"paper": "가상 체결 (paper)", "kiwoom": "내 키움 계좌 (" + ("모의투자" if is_mock else "실전") + ")"}
    cur = cfg.get("broker", "paper")
    pick = st.radio("주문 방식", list(kinds), index=list(kinds).index(cur) if cur in kinds else 0, format_func=kinds.get,
                    horizontal=True, disabled=running or not has_key, key="ops_broker",
                    help="키움 계좌는 로그인한 키 종류(모의/실전)로 주문합니다. 엔진 실행 중에는 못 바꿈.")
    if pick != cur:
        config_mod.update_sections({"broker": pick}, Path(cfg["_home"]) / "config.yaml")
        st.rerun()
    if cur != "kiwoom" or is_mock:
        return
    if cfg["kiwoom"].get("real_orders"):
        st.warning("실전 주문 동의함 — 매매 조건식이 실전 계좌로 실제 주문합니다.", icon=":material/warning:")
        if st.button("실전 주문 동의 철회", disabled=running, key="ops_real_off"):
            config_mod.set_kiwoom(real_orders=False)
            st.rerun()
        return
    st.error("실전 키입니다. 동의하기 전에는 실전 계좌로 주문하지 않고 관찰로만 동작합니다.", icon=":material/lock:")
    with st.popover("실전 주문 동의", icon=":material/gavel:", disabled=running):
        st.markdown("- 매매(mode: 매매) 조건식이 **실전 계좌로 실제 돈을 주문**합니다.\n- 손실은 본인 책임이며 ktrade는 수익을 보장하지 않습니다.\n"
                    "- 먼저 모의투자로 충분히 돌려 보셨나요? 리스크 한도(4단계)를 정해 두세요.")
        ok = st.checkbox("위 내용을 이해했고 실전 주문에 동의합니다", key="ops_real_ck")
        if st.button("동의하고 저장", type="primary", disabled=not ok, key="ops_real_on"):
            config_mod.set_kiwoom(real_orders=True)
            st.rerun()


@st.fragment(run_every="3s")
def ops_live(cfg: dict) -> None:
    """오른쪽: 편입 종목 · 보유 · 미체결 · 체결 + 실시간 로그. 3초마다 다시 그림."""
    from ktrade.dashboard import ops
    store = Store(paths(cfg)["db"], readonly=True)
    today = ops.last_active_day(store, date.today().isoformat())
    if today != date.today().isoformat():
        st.caption(f"오늘 기록이 아직 없어 마지막 기록일 **{today}**을 보여 줍니다.")
    ins, hold, pend, fills = ops.inserts(store, today), ops.holdings(store), ops.pending(store), ops.executions(store, today)
    labels = {"inserts": f"편입 종목 {int((ins['상태'] == '편입 중').sum()) if not ins.empty else 0}",
              "hold": f"보유 {len(hold)}", "pend": f"미체결 {len(pend)}", "fills": f"체결 {len(fills)}"}
    view = st.segmented_control("보기", list(labels), format_func=labels.get, default="inserts", key="ops_view", required=True,
                                label_visibility="collapsed")
    frames = {"inserts": (ins, "오늘 조건식 편입 종목"), "hold": (hold, "보유 종목"), "pend": (pend, "미체결 주문"),
              "fills": (fills, "오늘 체결")}
    df, alt = frames.get(view or "inserts")
    if df.empty:
        st.caption({"inserts": "오늘 편입 신호가 아직 없습니다.", "hold": "보유 종목이 없습니다.", "pend": "걸어 둔 주문이 없습니다.",
                    "fills": "오늘 체결이 없습니다."}[view or "inserts"])
    else:
        st.dataframe(df, hide_index=True, width="stretch", height=300, alt=alt)
    st.markdown("**실시간 로그**")
    log = ops.recent_log(store, today)
    if log.empty:
        st.caption("오늘 기록이 없습니다.")
    else:
        st.dataframe(log, hide_index=True, width="stretch", height=260, alt="오늘 엔진 판단·주문 기록")
    proc = store.get_setting("process.hts") or {}
    tail = ops.log_tail(proc.get("log"), 25)
    if tail:
        with st.expander(f"엔진 출력 ({Path(proc['log']).name})"):
            st.code(tail, language=None)


def tab_ops(cfg: dict) -> None:
    schedule_settings(cfg)
    ops_status(cfg)
    left, right = st.columns(2, gap="large")
    with left:
        ops_settings(cfg)
    with right:
        st.subheader("실시간", divider="gray")
        ops_live(cfg)
    st.caption("대시보드는 주문을 직접 내지 않습니다 — 시작한 엔진(ktrade hts watch)이 설정대로 매수·매도합니다. "
               "실전 계좌는 5단계에서 동의해야 주문하며, 수익을 보장하지 않습니다.")


def tab_help() -> None:
    st.markdown(f"""
**ktrade로 하는 일** — 매매조건(종목 선정 + 매수 타이밍)은 내가, 손절·익절·실행·백테스트는 엔진이 합니다.

0. **자동매매만 쓴다면** — 키움 로그인 → 🕹️ 운영 탭에서 조건식·손절·익절·한도를 정하고 시작. 차트 데이터는 필요 없습니다.
1. **데이터 받기 (백테스트·계획용, 선택)** — 📥 데이터 탭에서 몇 종목 또는 받을 범위, 또는 명령창 `ktrade collect`
2. **매매조건 만들기** — `ktrade new 내조건` → `~/.ktrade/strategies/내조건/strategy.py` 작성.
   직접 쓰기 어렵다면 `~/.ktrade/docs/LLM_CONTEXT.md`를 Claude·Gemini에 붙여 넣고 원하는 매매기법을 설명하면 코드를 써 줍니다.
3. **실험** — 🧪 백테스트 탭. **체험판**: 예제 매매조건을 내 데이터로 · 영웅문 조건식은 실제 신호를 쌓아 평가. **정식판**(이용 코드): 내 매매조건, `ktrade test 내조건`. 바꿀 때마다 기록이 쌓여 비교됩니다.
4. **켜기** — 정식판 백테스트 탭 '계획에 쓸 매매조건'에서 켬 → 장 마감 후 다음 날 계획이 생깁니다 (**계획** 탭).
5. **모의 운영** — `ktrade run`: 기본은 가상 체결(paper). 키움 모의투자는 `broker: kiwoom`, `is_mock: true`.
6. **자동 실행** — 🕹️ 운영 탭 맨 위 ⏰ 자동 실행에서 시각·요일을 정해 등록 (또는 `ktrade schedule install`, 기본 평일 16:30 계획 · 08:50 실행).

바탕화면의 **ktrade 명령창**에서 명령을 쓰고, 이 대시보드로 결과를 봅니다. 전체 명령: `ktrade --help`
""")


def tab_admin() -> None:
    """관리자 — 이용 코드 발급(이 PC에 서명 키가 있을 때) · 발급 기록."""
    st.markdown(f"관리자 권한: **{access.describe(acc)}** — 모든 기능(기본 기능 + 백테스트)과 이용 코드 발급.")
    seed = access.signing_seed()
    if not seed:
        st.info("이 PC에는 관리자 서명 키가 없어 코드를 발급할 수 없습니다. 서명 키가 있는 PC에서 발급하세요 "
                "(`~/.ktrade/admin/signing_key`를 옮기거나 `ktrade admin init`).")
        return
    if not access.key_registered(seed):
        st.warning("이 PC의 서명 키가 이 버전의 공개 키 목록(PUBLIC_KEYS)에 없어, 발급한 코드가 통하지 않습니다.")
    with st.form("admin_issue"):
        a, b, c = st.columns([2, 1, 1])
        user_id = a.text_input("아이디")
        role = b.selectbox("역할", ["backtest", "admin"], format_func={"backtest": "백테스트", "admin": "관리자"}.get)
        days = c.number_input("유효 기간(일, 0 = 만료 없음)", 0, 3650, 365, 30)
        note = st.text_input("메모 (누구에게, 왜)")
        ok = st.form_submit_button("이용 코드 발급", type="primary")
    if ok:
        try:
            code = access.admin_issue(user_id, role, int(days) or None, note)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success(f"{user_id.strip()} 코드를 발급했습니다 — 아래 코드를 아이디와 함께 전달하세요.")
            st.code(code, language=None)
    rows = access.issued()
    if rows:
        st.markdown("**발급 기록**")
        st.dataframe(pd.DataFrame(rows[::-1]).rename(columns={"at": "발급", "id": "아이디", "role": "역할", "exp": "만료",
                                                              "iat": "발급일", "note": "메모"}), hide_index=True, width="stretch")


def main_view(cfg: dict) -> None:
    """키움 로그인(또는 받아 둔 DB)만 있으면 모든 기본 기능 — 차트 데이터는 백테스트·계획용으로 고를 때만."""
    P = paths(cfg)
    if not P["db"].exists():
        Store(P["db"])                                                   # 빈 DB (매매 기록·조건식 신호용)
    store = Store(P["db"], readonly=True)
    st.title("📈 ktrade")
    hb = store.get_setting("live.heartbeat", {}) or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("매매 모드", f"{cfg['broker']} · {'모의' if cfg['kiwoom'].get('is_mock', True) else '실전'}")
    c2.metric("마지막 점검 (ktrade run)", hb.get("ts", "기록 없음"))
    c3.metric("마지막 일봉", store.last_index_date_before("9999-12-31") or "받지 않음 (선택)")
    full = access.can_backtest(acc)
    names = ["🕹️ 운영", "🗓️ 계획", "💹 매매 기록", "📊 성적", "🔎 영웅문 조건식", "📈 종목 차트", "📥 데이터",
             "🧪 백테스트 " + ("정식판" if full else "체험판"), "❓ 사용법"] + (["🛠️ 관리자"] if access.is_admin(acc) else [])
    tabs = st.tabs(names)
    with tabs[0]:
        tab_ops(cfg)
    with tabs[1]:
        tab_plan(P, store, cfg)
    with tabs[2]:
        tab_trades(store)
    with tabs[3]:
        tab_perf(P, store)
    with tabs[4]:
        tab_hts(cfg, store)
    with tabs[5]:
        tab_chart(cfg, store)
    with tabs[6]:
        tab_data(cfg, store)
    with tabs[7]:
        if full:
            tab_lab(cfg, store)
        else:
            tab_trial(cfg, store)
    with tabs[8]:
        tab_help()
    if access.is_admin(acc):
        with tabs[9]:
            tab_admin()


# ───────────────────────── 화면 고르기 ─────────────────────────
def has_data(cfg: dict) -> bool:
    db = paths(cfg)["db"]
    if not db.exists():
        return False
    try:
        s = Store(db, readonly=True)
        if s.market.has_daily():
            return True
        return not s.query("SELECT 1 FROM condition_signals LIMIT 1").empty   # 영웅문 조건식만 쓰는 경우
    except Exception:
        return False


sidebar()
if ss.page == "login":
    page_login()
elif ss.page == "access":
    page_access()
elif has_key or has_data(base):
    main_view(base)
else:
    page_welcome()
