"""장중 실행 루프 — 장 마감 후 만든 계획(매매조건 + 엔진 청산 규칙)을 실행한다.

매 주기(기본 60초): 시세판(ka10095, 계획·보유 종목 전체를 한 번에) → 휴장 판단(시세 일자가 전부 오늘이 아니면 쉼)
→ 종목별 매수 조건 확인·손절·익절·보유기간 (engine/executor.py).

안전: kiwoom.is_mock: false(실전)이면 --live-confirm 없이는 시작하지 않는다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from ktrade.broker import build as build_broker
from ktrade.config import paths
from ktrade.data.store import Store
from ktrade.engine import planner
from ktrade.engine.executor import Executor
from ktrade.engine.risk import Risk

log = logging.getLogger("ktrade.live")


class QuoteBoard:
    """주기 시작에 받은 시세. 없는 종목만 한 종목씩 조회(fallback)."""

    def __init__(self, source, max_age_sec: float = 120):
        self.source, self.max_age = source, max_age_sec
        self.quotes: dict[str, dict] = {}
        self.at = 0.0

    def refresh(self, symbols: list[str]) -> int:
        self.quotes = {}
        try:
            self.quotes = self.source.quotes(list(dict.fromkeys(symbols))) if symbols else {}
            self.at = time.monotonic()
        except Exception as exc:
            log.warning(f"시세판 조회 실패 — 종목별 조회로 대체: {exc}")
        return len(self.quotes)

    def traded_today(self, yyyymmdd: str) -> bool | None:
        if not self.quotes:
            return None
        return any(q.get("date") == yyyymmdd for q in self.quotes.values())

    def quote(self, symbol: str) -> dict:
        q = self.quotes.get(symbol) if time.monotonic() - self.at <= self.max_age else None
        if q and q.get("price"):
            return q
        q = self.source.quote(symbol) or {}
        if q.get("price"):
            self.quotes[symbol] = q
        return q


def request_stop(store: Store, target: str, now: datetime | None = None) -> None:
    """화면의 '중지' — 돌고 있는 ktrade run(target=run) / hts watch(target=hts)가 다음 주기에 스스로 끝난다."""
    store.set_setting(f"process.stop.{target}", {"at": (now or datetime.now()).isoformat(timespec="seconds")})


def stop_requested(store: Store, target: str, started: datetime) -> bool:
    req = store.get_setting(f"process.stop.{target}") or {}
    return bool(req.get("at")) and req["at"] >= started.isoformat(timespec="seconds")


def market_open(cfg: dict, now: datetime) -> bool:
    return now.weekday() < 5 and cfg["live"]["market_open"] <= now.strftime("%H:%M") <= cfg["live"]["market_close"]


def run(cfg: dict, live_confirm: bool = False, once: bool = False, quote_source=None, broker=None,
        now_fn=datetime.now, sleep=time.sleep) -> None:
    if cfg.get("broker") == "kiwoom" and not cfg["kiwoom"].get("is_mock", True) and not live_confirm:
        raise SystemExit("실전 계좌(is_mock: false)입니다. 충분한 백테스트·모의 운영 뒤 본인 판단이면 --live-confirm 을 붙이세요.")
    store = Store(paths(cfg)["db"])
    broker = broker or build_broker(cfg, store)
    if quote_source is None:
        if not cfg["_secrets"]["app_key"]:
            raise SystemExit("장중 시세를 받으려면 키움 앱키가 필요합니다 (paper 브로커여도 시세는 키움에서 받음). "
                             "대시보드 로그인 화면 또는 ktrade login 으로 입력하세요.")
        from ktrade.data.kiwoom import KiwoomData
        quote_source = KiwoomData(cfg)
    risk = Risk.from_config(cfg, broker.total_deposit())
    board = QuoteBoard(quote_source)
    ex = Executor(store, broker, risk, board.quote, role="plan")
    loaded_for = holiday_noted = ""
    started = now_fn()
    log.info(f"시작: broker={cfg['broker']} is_mock={cfg['kiwoom'].get('is_mock', True)}")
    while True:
        now = now_fn()
        if stop_requested(store, "run", started):
            log.info("화면에서 중지 요청 — 끝냄")
            return
        today = now.strftime("%Y-%m-%d")
        if market_open(cfg, now):
            if loaded_for != today:
                items, why = planner.load_for_today(cfg, store, today)
                ex.set_plans(items)
                loaded_for = today
                log.info(f"계획: {why}")
            board.refresh(sorted(set(ex.symbols()) | set(broker.positions())))
            if board.traded_today(now.strftime("%Y%m%d")) is False:
                if holiday_noted != today:
                    log.info("오늘 일자 시세가 없음 — 휴장일(또는 개장 직후)로 보고 쉰다")
                    holiday_noted = today
            else:
                ex.guard(now)                  # 하루 손익 한도 → 정지(+전량 매도)
                for sym in ex.symbols():
                    try:
                        ex.check(sym, now)
                    except Exception as exc:
                        log.warning(f"[{sym}] 계획 실행 오류: {exc}")
        store.set_setting("live.heartbeat", {"ts": now.isoformat(timespec="seconds"), "market_open": market_open(cfg, now)})
        if once:
            return
        sleep(cfg["live"]["interval_sec"])
