"""설정 — 사용자 폴더(KTRADE_HOME, 기본 ~/.ktrade) 하나에 모두 둔다.

    ~/.ktrade/config.yaml   설정 (ktrade init이 기본값으로 만든다)
    키움 앱키               대시보드 로그인 화면 또는 `ktrade login`으로 입력 → OS 자격 증명 저장소 (ktrade/credentials.py)
                            (예전 방식 ~/.ktrade/.env·환경변수 KIWOOM_APP_KEY/SECRET도 읽으며 그쪽이 우선)
    ~/.ktrade/demo/         체험 모드용 가상 데이터 (키 없이 둘러보기, ktrade/demo.py)
    ~/.ktrade/data/         market.db (시세·매매 기록)
    ~/.ktrade/plans/        장 마감 후 만든 계획 파일 (<기준일>/<조건>.json)
    ~/.ktrade/strategies/   내 매매조건 (조건 하나 = <id>.py 파일 하나, 또는 폴더 하나) — strategies.dirs로 다른 폴더 추가
    ~/.ktrade/experiments/  백테스트 실험 기록 (조건을 바꿀 때마다 쌓이고 비교된다)

역할: 매매조건(종목 선정·매수 타이밍)은 사용자, 손절·이익실현·보유기간(exits)·실행·백테스트는 엔진.
안전 기본값: broker는 paper(가상 체결), kiwoom을 고르더라도 is_mock: true(키움 모의투자 서버).
실전(is_mock: false)은 사용자가 직접 바꾸고 `ktrade run --live-confirm`으로만 켜진다.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "broker": "paper",                 # paper(가상 체결, 키 없이 연습) | kiwoom
    "kiwoom": {"is_mock": True},
    "paper": {"starting_cash": 10_000_000},
    "exits": {                         # 손절·이익실현 (엔진 몫) — 조건별 덮어쓰기는 strategy.yaml exits / enabled[].exits
        "stop": {"type": "pct", "value": 7.0, "period": 14, "buffer_pct": 1.0},
        "take_profit": {"type": "pct", "value": 10.0},
        "trailing": {"activate_pct": None, "trail_pct": None},
        "max_hold_days": 10,
        "stop_check": "close",
        "stop_check_after": "15:10",
    },
    "backtest": {"start": None, "end": None, "years": 3,   # 기간: start~end, 비면 DB 마지막 거래일부터 years년
                 "symbols": [], "symbols_file": None,          # 대상 종목: 비면 DB에 있는 전 종목
                 "cost_pct": 0.25, "oos_ratio": 0.3},
    "risk": {
        "max_position_pct": 2.0,           # 종목당 매수금액 = 시작 총자본 × 이 %
        "max_concurrent_positions": 5,     # 동시 보유 종목 수 한도 (조건 전체 합)
        "intraday_stop_pct": None,         # 사용자 장중 손절 % 안전망 (None = 끔, 엔진 exits와 별도)
        "order_slippage_pct": 0.5,         # 주문 지정가 = 현재가 ± 이 % (체결 보장용 범위)
        "max_new_orders_per_day": 10,      # 하루 신규 매수 주문 한도 (조건 전체 합)
        "max_plans_per_strategy": 20,      # 조건 하나가 하루에 낼 수 있는 계획 수 한도
    },
    "strategies": {
        "dirs": [],                        # 추가로 조건을 찾을 폴더 (첫 폴더에 ktrade new가 만듦, ${ENV} 사용 가능)
        "enabled": [],                     # [{id: example_breakout, params: {...}, exits: {...}}] — 위에 있을수록 우선
        "require_backtest": True,          # 지금 설정으로 백테스트한 기록이 없는 조건은 계획을 만들지 않음
    },
    "external_plans": [],                  # [{id: my_strategy, dir: "C:/.../plans", use_engine_exits: false}]
    "collect": {                                            # 키움에서 받아 DB에 쌓을 범위 (사용자가 정함)
        "symbols": [], "symbols_file": None,                   # 종목 직접 지정. 비면 market_names 시장 전체
        "market_names": ["거래소", "코스닥"],                  # ETF도 쓰려면 "ETF" 추가
        "exclude": [], "exclude_states": ["관리종목", "거래정지"],
        "start": None, "end": None, "years": 3,                # 기간: start~end, start가 비면 오늘부터 years년 전
        "minute": {"symbols": [], "start": None},              # 1분봉도 받을 종목 (키움이 주는 범위까지)
    },
    "live": {"interval_sec": 60, "market_open": "09:00", "market_close": "15:30"},
}

TEMPLATE = """# ktrade 설정 — 바꾼 뒤 ktrade run을 다시 시작해야 반영됩니다.
# 역할: 매매조건(종목 선정·매수 타이밍)은 내가 strategies/ 에 작성, 손절·이익실현·실행·백테스트는 엔진.

# paper = 가상 체결(키 없이 연습), kiwoom = 키움 REST API 주문
broker: paper

kiwoom:
  is_mock: true        # true = 키움 모의투자 서버. 실전(false)은 충분한 백테스트·모의 운영 뒤 본인 판단으로만.

paper:
  starting_cash: 10000000

# 손절·이익실현·보유기간 — 모든 매매조건에 공통 적용 (조건별로 바꾸려면 strategy.yaml 또는 아래 enabled[].exits)
exits:
  stop: {type: pct, value: 7}          # pct(매수가 -%) | atr(매수가 - ATR×value, period) | low(value일 최저가 -buffer_pct%) | none
  take_profit: {type: pct, value: 10}  # pct(매수가 +%) | r(손절폭 × value) | none
  trailing: {activate_pct: null, trail_pct: null}   # 예: {activate_pct: 5, trail_pct: 4} = +5% 이후 최고가에서 -4%면 매도
  max_hold_days: 10
  stop_check: close                    # close = 15:10 이후에만 손절 판단(장중 흔들림 무시) | intraday = 닿는 즉시

# 백테스트 = 내 매매조건 + DB(아래 collect로 받은 데이터). 명령 옵션(--start --end --symbols)이 있으면 그게 우선
backtest:
  start: null       # 예: 2022-01-01  (비면 DB 마지막 거래일부터 years년 전)
  end: null         # 예: 2025-12-31  (비면 DB 마지막 거래일)
  years: 3
  symbols: []       # 이 종목만 대상으로 (예: [005930, 000660]). 비면 DB에 있는 전 종목
  symbols_file: null
  cost_pct: 0.25    # 왕복 비용 (수수료 + 거래세 근사)
  oos_ratio: 0.3    # 뒤 30% 기간을 '확인용'으로 따로 보여 줌 (앞에서만 좋은 조건인지)

risk:
  max_position_pct: 2.0
  max_concurrent_positions: 5
  intraday_stop_pct: null
  order_slippage_pct: 0.5
  max_new_orders_per_day: 10
  max_plans_per_strategy: 20

strategies:
  dirs: []                      # 내 매매조건 폴더 (첫 폴더에 ktrade new가 만듦). 예: ["D:/trading/my-conditions"], ["${DEV_ROOT}/conditions"]
  require_backtest: true        # 백테스트 안 한 설정의 조건은 실행하지 않음 (조건을 바꾸면 ktrade test <id>)
  enabled: []                   # 실행할 내 매매조건 (위에 있을수록 우선). 예:
  #  - id: my_condition
  #    params: {}
  #    exits: {stop: {type: pct, value: 5}}   # 이 조건만 청산 설정 다르게 (선택)

# 파이썬이 아닌 프로그램이 계획 파일(schema 1)을 만드는 경우 그 폴더를 적는다 (docs/PLAN_SCHEMA.md)
external_plans: []

# 키움에서 받아 DB에 쌓을 범위 — ktrade collect / daily 가 이대로 받는다 (받아 둔 범위 확인: ktrade data)
collect:
  symbols: []                  # 종목 직접 지정 (예: [005930, 000660]). 비면 아래 시장 전체
  symbols_file: null           # 또는 종목 파일 (한 줄에 하나, ~/.ktrade 기준 상대경로 가능)
  market_names: [거래소, 코스닥] # symbols가 비었을 때 받을 시장. ETF도 쓰려면 ETF 추가
  exclude: []                  # 빼고 받을 종목
  exclude_states: [관리종목, 거래정지]
  start: null                  # 예: 2020-01-01  (비면 오늘부터 years년 전)
  end: null                    # 예: 2025-12-31  (비면 오늘까지, 매일 이어받기)
  years: 3
  minute:                      # 1분봉 (선택, 지정 종목만 — 키움이 주는 범위까지, 보통 최근 몇 달)
    symbols: []
    start: null

live:
  interval_sec: 60
"""


def home() -> Path:
    return Path(os.getenv("KTRADE_HOME") or Path.home() / ".ktrade").expanduser()


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path | None = None) -> dict:
    h = home()
    p = path or h / "config.yaml"
    user = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg = _merge(DEFAULTS, user or {})
    env = h / ".env"
    if env.exists():
        from dotenv import dotenv_values
        for k, v in dotenv_values(env).items():
            os.environ.setdefault(k, v or "")
    cfg["_home"] = str(h)
    key, secret = os.getenv("KIWOOM_APP_KEY", ""), os.getenv("KIWOOM_APP_SECRET", "")
    if not (key and secret):                 # 환경변수·.env에 없으면 로그인으로 저장한 키
        from ktrade import credentials
        saved = credentials.load_all()
        key, secret = key or saved["KIWOOM_APP_KEY"], secret or saved["KIWOOM_APP_SECRET"]
    cfg["_secrets"] = {"app_key": key, "app_secret": secret}
    return cfg


def set_is_mock(is_mock: bool, path: Path | None = None) -> None:
    """config.yaml의 kiwoom.is_mock만 바꾼다 (주석·나머지 설정은 그대로). 로그인 화면에서 키 종류를 고를 때."""
    import re
    p = path or home() / "config.yaml"
    text = p.read_text(encoding="utf-8") if p.exists() else TEMPLATE
    val = "true" if is_mock else "false"
    new, n = re.subn(r"(?m)^(\s+is_mock:\s*)(true|false)\b", rf"\g<1>{val}", text, count=1)
    if not n:
        new = text.rstrip("\n") + f"\n\nkiwoom:\n  is_mock: {val}\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")


def paths(cfg: dict) -> dict[str, Path]:
    h = Path(cfg["_home"])
    return {"home": h, "db": h / "data" / "market.db", "plans": h / "plans", "strategies": h / "strategies",
            "logs": h / "logs", "experiments": h / "experiments"}
