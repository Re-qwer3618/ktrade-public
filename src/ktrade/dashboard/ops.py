"""운영 화면 뒤의 일 — 화면(app.py tab_ops)과 떼어 둬서 테스트할 수 있게.

대시보드는 주문을 직접 내지 않는다. 하는 일은
  · 설정 저장 (config.yaml의 hts_conditions·risk만, config.update_sections)
  · 엔진 시작 = `ktrade hts watch`를 별도 프로세스로 띄움 / 중지 = 중지 요청(process.stop.hts)을 남김 → 엔진이 5초 안에 스스로 끝냄
  · 긴급 정지 = trading.halt(flatten) → 돌고 있는 엔진이 신규 매수를 멈추고 보유를 모두 판다
  · 상태·편입·보유·미체결·체결·로그 읽기 (DB의 condition_signals·plan_trades·decisions·settings)
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from ktrade.config import paths
from ktrade.data.store import Store
from ktrade.engine.executor import HALT_KEY, PNL_KEY, book, halt_state

HTS_FRESH_SEC = 20          # hts watch는 5초마다 heartbeat


def _age(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        return (now - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return None


def status(store: Store, cfg: dict, now: datetime | None = None) -> dict:
    """상단 상태 표시줄 — 모드, 엔진(hts watch·ktrade run) 동작 여부, 오늘 손익, 정지 상태."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    hts = store.get_setting("hts.heartbeat") or {}
    run = store.get_setting("live.heartbeat") or {}
    proc = store.get_setting("process.hts") or {}
    pnl = store.get_setting(PNL_KEY) or {}
    hts_age, run_age = _age(hts.get("ts"), now), _age(run.get("ts"), now)
    stop_req = store.get_setting("process.stop.hts") or {}
    real = cfg.get("broker") == "kiwoom" and not cfg["kiwoom"].get("is_mock", True)
    return {
        "mode": "paper (가상 체결)" if cfg.get("broker", "paper") == "paper" else ("키움 실전" if real else "키움 모의투자"),
        "real": real,
        "hts_running": hts_age is not None and hts_age < HTS_FRESH_SEC,
        "hts": hts, "hts_age": hts_age, "proc": proc,
        "stop_pending": bool(stop_req.get("at")) and stop_req["at"] >= (hts.get("ts") or "") and hts_age is not None
        and hts_age < HTS_FRESH_SEC,
        "run_running": run_age is not None and run_age < 2 * float(cfg["live"].get("interval_sec", 60)) + 30,
        "run": run,
        "pnl": pnl if pnl.get("date") == today else {"date": today, "realized": 0, "unrealized": 0, "total": 0},
        "halt": halt_state(store, today),
    }


def emergency_stop(store: Store, reason: str = "긴급 정지 (운영 화면)") -> None:
    from ktrade.engine.executor import set_halt
    set_halt(store, datetime.now().strftime("%Y-%m-%d"), reason, flatten=True)
    store.log_decision("engine", "", "HALT", "HALT", reason + " — 신규 매수 중지, 보유 전량 매도")


def release_halt(store: Store) -> None:
    from ktrade.engine.executor import clear_halt
    clear_halt(store)
    store.log_decision("engine", "", "HALT", "RELEASE", "정지 해제 (운영 화면)")


def start_watch(cfg: dict, store: Store, now: datetime | None = None, popen=subprocess.Popen) -> dict:
    """`ktrade hts watch`를 이 대시보드와 따로 사는 프로세스로 띄운다(대시보드를 닫아도 계속). 이미 돌고 있으면 띄우지 않는다."""
    now = now or datetime.now()
    st = status(store, cfg, now)
    if st["hts_running"]:
        return {"ok": False, "reason": "이미 실행 중"}
    if not cfg.get("hts_conditions") and not cfg.get("forward_test"):
        return {"ok": False, "reason": "조건식이 없습니다 — 1단계에서 고르고 저장하세요 (또는 백테스트 체험판에서 테스트할 조건식)"}
    if not cfg["_secrets"].get("app_key"):
        return {"ok": False, "reason": "키움 로그인이 필요합니다 (조건검색은 키움 서버가 계산)"}
    logs = paths(cfg)["logs"]
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"hts_watch_{now:%Y%m%d_%H%M%S}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "KTRADE_HOME": cfg["_home"]}
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with open(log, "w", encoding="utf-8") as fh:
        proc = popen([sys.executable, "-m", "ktrade", "hts", "watch"], stdout=fh, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, env=env, creationflags=flags, cwd=cfg["_home"])
    info = {"pid": proc.pid, "started": now.isoformat(timespec="seconds"), "log": str(log)}
    store.set_setting("process.hts", info)
    store.log_decision("engine", "", "PROCESS", "START", f"hts watch 시작 (pid {proc.pid}, 로그 {log.name})",
                       now.isoformat(timespec="seconds"))
    return {"ok": True, **info}


def stop_watch(store: Store) -> None:
    from ktrade.engine.live import request_stop
    request_stop(store, "hts")
    store.log_decision("engine", "", "PROCESS", "STOP", "hts watch 중지 요청 (운영 화면)")


def log_tail(path: str | None, n: int = 40) -> str:
    if not path or not Path(path).exists():
        return ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readlines()[-n:])


# ---------------- 오른쪽 탭 ----------------
def last_active_day(store: Store, today: str) -> str:
    """오늘 신호·판단 기록이 없으면(장 전·휴일) 마지막으로 기록이 있는 날."""
    r = store.query("SELECT MAX(d) AS d FROM (SELECT MAX(date) AS d FROM condition_signals WHERE date <= ? "
                    "UNION ALL SELECT MAX(substr(timestamp, 1, 10)) FROM decisions WHERE substr(timestamp, 1, 10) <= ?)",
                    (today, today))
    d = r["d"].iloc[0] if not r.empty else None
    return today if not d or d == today else str(d)


def inserts(store: Store, day: str) -> pd.DataFrame:
    """오늘 편입 종목 — 조건식·종목마다 마지막 상태(편입 중/이탈)와 판단."""
    sig = store.signals(day)
    if sig.empty:
        return pd.DataFrame(columns=["조건식", "종목", "상태", "첫 편입", "마지막", "가격", "판단", "사유"])
    ev = sig[sig["event"].isin(["I", "D", "INIT"])]
    rows = []
    for (cond, sym), g in ev.groupby(["cond_name", "symbol"], sort=False):
        last = g.iloc[-1]
        ins = g[g["event"].isin(["I", "INIT"])]
        decided = g[~g["action"].isin(["REMOVE", "INIT", "WAIT"])]
        act = decided.iloc[-1] if not decided.empty else last
        rows.append({"조건식": cond, "종목": sym, "상태": "이탈" if last["event"] == "D" else "편입 중",
                     "첫 편입": (ins["ts"].iloc[0] if not ins.empty else last["ts"])[11:19], "마지막": last["ts"][11:19],
                     "가격": ins["price"].dropna().iloc[-1] if not ins["price"].dropna().empty else None,
                     "판단": act["action"], "사유": act["reason"]})
    return pd.DataFrame(rows).sort_values("마지막", ascending=False, ignore_index=True)


def _active(store: Store) -> list[dict]:
    r = store.query("SELECT * FROM plan_trades WHERE status IN ('submitted', 'open', 'closing') ORDER BY id")
    return r.to_dict("records")


def holdings(store: Store) -> pd.DataFrame:
    rows = []
    for t in _active(store):
        fills, sells, pend = book(t)
        held = int(t["qty"] or 0) if t["status"] != "submitted" else 0
        rows.append({"종목": t["symbol"], "조건": str(t["strategy"]).removeprefix("hts:"), "상태": t["status"],
                     "보유": held, "계획": t.get("target_qty"), "평균가": t["entry_price"], "최고가": t.get("peak"),
                     "매수 차수": sum(1 for f in fills if f["qty"] > 0), "분할 매도": sum(1 for s in sells if s["qty"] > 0),
                     "매수일": t.get("entry_date"), "주문 중": "예" if pend else ""})
    return pd.DataFrame(rows, columns=["종목", "조건", "상태", "보유", "계획", "평균가", "최고가", "매수 차수", "분할 매도",
                                       "매수일", "주문 중"])


def pending(store: Store, now: datetime | None = None) -> pd.DataFrame:
    now = now or datetime.now()
    rows = []
    for t in _active(store):
        p = book(t)[2]
        if p:
            rows.append({"종목": t["symbol"], "구분": "매수" if p["side"] == "BUY" else "매도", "수량": p["qty"],
                         "주문가": p["price"], "주문 시각": p["at"][11:19], "경과(초)": int(_age(p["at"], now) or 0),
                         "주문번호": p.get("order_no") or "", "사유": p.get("reason") or (f"{p.get('step', 0) + 1}차 매수")})
    return pd.DataFrame(rows, columns=["종목", "구분", "수량", "주문가", "주문 시각", "경과(초)", "주문번호", "사유"])


def executions(store: Store, day: str) -> pd.DataFrame:
    """오늘 체결 — 매수 차수와 매도를 시간순으로."""
    r = store.query("SELECT * FROM plan_trades WHERE substr(updated_at, 1, 10) = ? OR status IN ('open', 'closing')", (day,))
    rows = []
    for t in r.to_dict("records"):
        fills, sells, _ = book(t)
        name = str(t["strategy"]).removeprefix("hts:")
        for f in fills:
            if f["qty"] > 0 and str(f.get("at", ""))[:10] == day:
                rows.append({"시각": f["at"][11:19], "종목": t["symbol"], "조건": name, "구분": "매수", "수량": f["qty"],
                             "가격": f["price"], "사유": f"{f['step'] + 1}차"})
        for s in sells:
            if s["qty"] > 0 and str(s.get("at", ""))[:10] == day:
                rows.append({"시각": s["at"][11:19], "종목": t["symbol"], "조건": name, "구분": "매도", "수량": s["qty"],
                             "가격": s["price"], "사유": s.get("reason", "")})
    df = pd.DataFrame(rows, columns=["시각", "종목", "조건", "구분", "수량", "가격", "사유"])
    return df.sort_values("시각", ascending=False, ignore_index=True)


def recent_log(store: Store, day: str, n: int = 60) -> pd.DataFrame:
    r = store.query("SELECT timestamp, strategy, symbol, signal, action, detail FROM decisions "
                    "WHERE substr(timestamp, 1, 10) = ? ORDER BY id DESC LIMIT ?", (day, n))
    r["timestamp"] = r["timestamp"].str[11:19]
    return r.rename(columns={"timestamp": "시각", "strategy": "조건", "symbol": "종목", "signal": "신호", "action": "처리",
                             "detail": "내용"})


# ---------------- 왼쪽 설정 ----------------
COND_COLS = ["사용", "조건식", "모드", "매수 시작", "매수 끝", "하루 매수 한도", "확인(초)", "추격 한도%", "이탈 시 매도"]


def conditions_table(cfg: dict) -> pd.DataFrame:
    from ktrade.engine.signals import DEFAULT_COND
    rows, seen = [], set()
    for c in cfg.get("hts_conditions") or []:
        c = {**DEFAULT_COND, **c}
        if c["name"] in seen:                     # 예전 저장 버그로 겹친 항목 — 앞(새) 것만, 다음 저장 때 파일도 정리됨
            continue
        seen.add(c["name"])
        rows.append({"사용": True, "조건식": c["name"], "모드": "매매" if c.get("mode") == "trade" else "관찰",
                     "매수 시작": c["buy_window"][0], "매수 끝": c["buy_window"][1], "하루 매수 한도": int(c["max_buys_per_day"]),
                     "확인(초)": int(c.get("confirm_sec") or 0), "추격 한도%": float(c.get("chase_pct") or 0),
                     "이탈 시 매도": bool(c.get("exit_on_remove"))})
    return pd.DataFrame(rows, columns=COND_COLS)


def conditions_from_table(df: pd.DataFrame, old: list[dict], exits: dict | None) -> list[dict]:
    """표 → hts_conditions. 표에 없는 키(price_range·reentry 등)는 예전 설정을 그대로 둔다. exits는 모든 조건식에 같이 적용."""
    prev = {c["name"]: c for c in old or []}
    out = []
    for r in df.to_dict("records"):
        name = str(r.get("조건식") or "").strip()
        if not name or not r.get("사용", True):
            continue
        c = dict(prev.get(name, {"name": name}))
        c.update({"mode": "trade" if r.get("모드") == "매매" else "watch",
                  "buy_window": [str(r.get("매수 시작") or "09:00"), str(r.get("매수 끝") or "15:20")],
                  "max_buys_per_day": int(r.get("하루 매수 한도") or 1), "confirm_sec": int(r.get("확인(초)") or 0),
                  "chase_pct": float(r.get("추격 한도%") or 0), "exit_on_remove": bool(r.get("이탈 시 매도"))})
        if exits is not None:
            c["exits"] = exits
        out.append(c)
    return out


def save_settings(cfg: dict, conds: list[dict], risk: dict, priority: dict | None = None) -> list[str]:
    """검사 → 저장. 오류가 있으면 저장하지 않고 오류 목록을 돌려준다."""
    from ktrade import config as C
    from ktrade.engine.signals import cond_exits, load_conditions
    errs = []
    names = [c["name"] for c in conds]
    if len(set(names)) != len(names):
        errs.append("같은 조건식이 두 번 있음")
    if len(set(names) | set(cfg.get("forward_test") or [])) > 10:
        errs.append("실시간 조건검색은 동시에 10개까지 (키움 한도)")
    try:
        for c in load_conditions({"hts_conditions": conds}):
            cond_exits(c)
    except ValueError as e:
        errs.append(str(e))
    win = risk.get("buy_window")
    if win and not (len(win) == 2 and win[0] < win[1]):
        errs.append("매수 시간대는 시작 < 끝")
    for k in ("order_amount", "daily_profit_target", "daily_loss_limit", "order_timeout_sec"):
        if risk.get(k) is not None and float(risk[k]) < 0:
            errs.append(f"{k}는 0 이상")
    if errs:
        return errs
    if priority is not None and priority.get("mode") not in ("plan", "hts", "split"):
        errs.append("우선순위는 plan · hts · split 중 하나")
        return errs
    C.update_sections({"hts_conditions": conds, "risk": {**cfg["risk"], **risk}, **({"priority": priority} if priority else {})},
                      Path(cfg["_home"]) / "config.yaml")
    return []


def exits_defaults(cfg: dict) -> dict:
    """2·3단계 입력의 처음 값 — 첫 조건식의 청산 설정(없으면 조건식 기본 청산)."""
    from ktrade.engine.signals import cond_exits, load_conditions
    conds = load_conditions(cfg)
    return cond_exits(conds[0] if conds else {"name": "-"})


def exits_from_form(stop_pct: float, intraday: bool, tp_pct: float, ts_activate: float, ts_trail: float, hold_days: int,
                    scale_in: pd.DataFrame, scale_out: pd.DataFrame) -> dict:
    """화면 입력 → 조건식 exits. 0 = 끔 (손절·익절·추적 손절)."""
    sin = [{"pct": float(r["비율%"]), "drop_pct": float(r.get("하락%") or 0)}
           for r in scale_in.dropna(subset=["비율%"]).to_dict("records") if float(r["비율%"]) > 0]
    sout = [{"pct": float(r["비율%"]), "gain_pct": float(r["상승%"])}
            for r in scale_out.dropna(subset=["비율%", "상승%"]).to_dict("records") if float(r["비율%"]) > 0]
    return {"stop": {"type": "pct", "value": float(stop_pct)} if stop_pct > 0 else {"type": "none"},
            "take_profit": {"type": "pct", "value": float(tp_pct)} if tp_pct > 0 else {"type": "none"},
            "trailing": {"activate_pct": float(ts_activate) if ts_trail > 0 else None,
                         "trail_pct": float(ts_trail) if ts_trail > 0 else None},
            "stop_check": "intraday" if intraday else "close", "max_hold_days": int(hold_days),
            "scale_in": sin if len(sin) > 1 else [], "scale_out": sout}
