"""설정 — 사용자 폴더(KTRADE_HOME, 기본 ~/.ktrade) 하나에 모두 둔다.

    ~/.ktrade/config.yaml   설정 (ktrade init이 기본값으로 만든다)
    키움 앱키               대시보드 로그인 화면 또는 `ktrade login`으로 입력 → OS 자격 증명 저장소 (ktrade/credentials.py)
                            (예전 방식 ~/.ktrade/.env·환경변수 KIWOOM_APP_KEY/SECRET도 읽으며 그쪽이 우선)
    ~/.ktrade/demo/         체험 모드용 가상 데이터 (키 없이 둘러보기, ktrade/demo.py)
    ~/.ktrade/data/         market/ (시세 Parquet: daily·index·minute·tick·orderbook 폴더) + market.db (종목 목록·매매 기록)
                            — config data_dir로 다른 폴더(다른 드라이브)에 둘 수 있다 (ktrade data-dir, 대시보드 📥 데이터 탭)
    ~/.ktrade/plans/        장 마감 후 만든 계획 파일 (<기준일>/<조건>.json)
    ~/.ktrade/strategies/   내 매매조건 폴더들 (조건 하나 = 폴더 하나, strategy.yaml + strategy.py)
    ~/.ktrade/experiments/  백테스트 실험 기록 (조건을 바꿀 때마다 쌓이고 비교된다)

역할: 매매조건(종목 선정·매수 타이밍)은 사용자, 손절·이익실현·보유기간(exits)·실행·백테스트는 엔진.
안전 기본값: broker는 paper(가상 체결), kiwoom을 고르더라도 is_mock: true(키움 모의투자 서버).
실전(is_mock: false) 주문은 사용자가 동의해야만 나간다 — 조건식 매매는 kiwoom.real_orders: true(운영 탭 5단계·ktrade real-orders on),
매매조건 계획 실행은 `ktrade run --live-confirm`.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "data_dir": None,                  # 시세·DB 폴더. 비면 <KTRADE_HOME>/data, 상대 경로는 KTRADE_HOME 기준
    "broker": "paper",                 # paper(가상 체결, 키 없이 연습) | kiwoom
    "kiwoom": {"is_mock": True, "real_orders": False},
    "paper": {"starting_cash": 10_000_000},
    "exits": {                         # 손절·이익실현 (엔진 몫) — 조건별 덮어쓰기는 strategy.yaml exits / enabled[].exits
        "stop": {"type": "pct", "value": 7.0, "period": 14, "buffer_pct": 1.0},
        "take_profit": {"type": "pct", "value": 10.0},
        "trailing": {"activate_pct": None, "trail_pct": None},
        "max_hold_days": 10,
        "stop_check": "close",
        "stop_check_after": "15:10",
        "scale_in": [],                # 분할매수 [{pct: 50}, {pct: 50, drop_pct: 3}] — 비면 한 번에
        "scale_out": [],               # 분할익절 [{pct: 50, gain_pct: 3}] — 비면 목표가·추적 손절로 한 번에
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
        "order_amount": None,              # 종목당 매수 금액(원). 정하면 max_position_pct 대신 이 금액
        "buy_window": None,                # 신규 매수 시간대 ["09:00", "14:30"] (None = 계획·조건식 설정대로)
        "daily_profit_target": None,       # 하루 수익 목표(원) — 닿으면 그날 신규 매수 중지 + 보유 전량 매도
        "daily_loss_limit": None,          # 하루 손실 한도(원, 양수로) — 닿으면 그날 신규 매수 중지 + 보유 전량 매도
        "flatten_at": None,                # 시간 일괄청산 "15:15" — 이 시각 이후 보유 전량 매도 (단타용)
        "order_timeout_sec": None,         # 미체결 자동 취소(초) — 지나면 남은 수량 취소 (None = 다음 거래일까지 기다림)
    },
    "strategies": {
        "dirs": [],                        # 추가로 조건을 찾을 폴더 (KTRADE_HOME/strategies는 항상 포함)
        "enabled": [],                     # [{id: example_breakout, params: {...}, exits: {...}}] — 위에 있을수록 우선
        "require_backtest": True,          # 지금 설정으로 백테스트한 기록이 없는 조건은 계획을 만들지 않음
    },
    "external_plans": [],                  # [{id: my_strategy, dir: "C:/.../plans", use_engine_exits: false}]
    "hts_conditions": [],                  # 영웅문 조건식 (장중 실시간 편입 신호) — 형식은 engine/signals.py DEFAULT_COND
    "collect": {                                            # 키움에서 받아 DB에 쌓을 범위 (사용자가 정함)
        "symbols": [], "symbols_file": None,                   # 종목 직접 지정. 비면 market_names 시장 전체
        "market_names": ["거래소", "코스닥"],                  # ETF도 쓰려면 "ETF" 추가
        "exclude": [], "exclude_states": ["관리종목", "거래정지"],
        "exclude_kinds": ["ETF", "ETN", "스팩", "우선주"],      # 시장 범위에서 뺄 종류 (collect.KINDS)
        "exclude_warning": True,                               # 투자유의(정리매매·단기과열·투자위험·투자경고) 제외
        "price": {"min": None, "max": None},                   # 전일 종가(원) 범위
        "market_cap": {"min": None, "max": None},              # 시가총액(억 원) 범위
        "start": None, "end": None, "years": 3,                # 기간: start~end, start가 비면 오늘부터 years년 전
        "daily": True,                                         # 일봉 받기
        "minute": {"symbols": [], "intervals": [1], "start": None},  # N분봉 받을 종목 (all = 위 범위 전체), 키움이 주는 범위까지
        "tick": {"symbols": [], "units": [1], "start": None, "max_pages": 50},  # N틱봉 (키움은 최근 며칠만 줌)
    },
    "chart": {"ma": [5, 20, 60, 120], "volume_ma": [20]},     # 차트 이동평균선 (저장하지 않고 볼 때 계산)
    "live": {"interval_sec": 60, "market_open": "09:00", "market_close": "15:30"},
}

TEMPLATE = """# ktrade 설정 — 바꾼 뒤 ktrade run을 다시 시작해야 반영됩니다.
# 역할: 매매조건(종목 선정·매수 타이밍)은 내가 strategies/ 에 작성, 손절·이익실현·실행·백테스트는 엔진.

# 시세·DB를 둘 폴더 (비우면 사용자 폴더 안 data). 옮길 때는 ktrade data-dir <새 폴더> 또는 대시보드 📥 데이터 탭 — 기존 데이터도 같이 옮겨 줌
data_dir: null

# paper = 가상 체결(키 없이 연습), kiwoom = 키움 REST API 주문
broker: paper

kiwoom:
  is_mock: true        # true = 키움 모의투자 서버. 실전(false)은 충분한 백테스트·모의 운영 뒤 본인 판단으로만.
  real_orders: false   # 실전 키일 때 조건식 매매가 실제로 주문해도 되는지 (본인 동의). 키를 새로 저장하면 false로 돌아감

paper:
  starting_cash: 10000000

# 손절·이익실현·보유기간 — 모든 매매조건에 공통 적용 (조건별로 바꾸려면 strategy.yaml 또는 아래 enabled[].exits)
exits:
  stop: {type: pct, value: 7}          # pct(매수가 -%) | atr(매수가 - ATR×value, period) | low(value일 최저가 -buffer_pct%) | none
  take_profit: {type: pct, value: 10}  # pct(매수가 +%) | r(손절폭 × value) | none
  trailing: {activate_pct: null, trail_pct: null}   # 예: {activate_pct: 5, trail_pct: 4} = +5% 이후 최고가에서 -4%면 매도
  max_hold_days: 10
  stop_check: close                    # close = 15:10 이후에만 손절 판단(장중 흔들림 무시) | intraday = 닿는 즉시
  scale_in: []                         # 분할매수 예: [{pct: 50}, {pct: 50, drop_pct: 3}] = 절반 산 뒤 첫 매수가 -3%에서 나머지
  scale_out: []                        # 분할익절 예: [{pct: 50, gain_pct: 3}] = 평균가 +3%에서 절반, 나머지는 목표가·추적 손절

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
  order_amount: null            # 종목당 매수 금액(원). 예: 1000000 — 정하면 max_position_pct 대신
  buy_window: null              # 신규 매수 시간대. 예: ["09:00", "14:30"]
  daily_profit_target: null     # 하루 수익 목표(원). 닿으면 그날 매수 중지 + 보유 전량 매도
  daily_loss_limit: null        # 하루 손실 한도(원). 예: 300000 — 닿으면 그날 매수 중지 + 보유 전량 매도
  flatten_at: null              # 시간 일괄청산. 예: "15:15" — 이 시각 이후 보유 전량 매도 (단타)
  order_timeout_sec: null       # 미체결 자동 취소(초). 예: 30 — 지나면 남은 수량 취소
  # 하루 손익 한도·시간 일괄청산은 계좌 단위라 백테스트(종목별 거래)에는 반영되지 않습니다.

strategies:
  dirs: []
  require_backtest: true        # 백테스트 안 한 설정의 조건은 실행하지 않음 (조건을 바꾸면 ktrade test <id>)
  enabled: []                   # 실행할 내 매매조건 (위에 있을수록 우선). 예:
  #  - id: my_condition
  #    params: {}
  #    exits: {stop: {type: pct, value: 5}}   # 이 조건만 청산 설정 다르게 (선택)

# 파이썬이 아닌 프로그램이 계획 파일(schema 1)을 만드는 경우 그 폴더를 적는다 (docs/PLAN_SCHEMA.md)
external_plans: []

# 영웅문4 '조건검색(0150)'에서 만든 조건식을 장중 실시간 신호로 (ktrade hts list 로 이름 확인, ktrade hts watch 로 관찰)
# 지금은 관찰(watch)만 — 편입·이탈과 '샀다면' 판단을 신호 일지에 남기고 주문은 하지 않는다 (ktrade hts signals)
hts_conditions: []
#  - name: "01_오전스켈핑"       # 조건식 이름 (일부만 적어도 하나로 정해지면 됨)
#    mode: watch
#    buy_window: ["09:00", "10:30"]
#    price_range: [1000, 50000]
#    chase_pct: 1.0             # 편입가 대비 이만큼 넘게 오르면 안 삼
#    confirm_sec: 0             # 0 = 편입 즉시 / 30 = 30초 동안 이탈 없고 편입가 이상일 때
#    max_buys_per_day: 3
#    reentry: false
#    exit_on_remove: false      # 조건 이탈에 매도 (매도 조건식 역할)

# 키움에서 받아 DB에 쌓을 범위 — ktrade collect / daily 가 이대로 받는다 (받아 둔 범위 확인: ktrade data)
collect:
  symbols: []                  # 종목 직접 지정 (예: [005930, 000660]). 비면 아래 시장 전체
  symbols_file: null           # 또는 종목 파일 (한 줄에 하나, ~/.ktrade 기준 상대경로 가능)
  market_names: [거래소, 코스닥] # symbols가 비었을 때 받을 시장. ETF도 쓰려면 ETF 추가
  # ↓ 시장 범위에서 고르기 (symbols로 직접 지정한 종목에는 적용하지 않음)
  exclude: []                  # 빼고 받을 종목
  exclude_states: [관리종목, 거래정지]
  exclude_kinds: [ETF, ETN, 스팩, 우선주]   # 뺄 종류: ETF ETN 스팩 리츠 우선주
  exclude_warning: true        # 투자유의(정리매매·단기과열·투자위험·투자경고) 제외
  price: {min: null, max: null}       # 전일 종가(원), 예: {min: 1000, max: 50000}
  market_cap: {min: null, max: null}  # 시가총액(억 원), 예: {min: 500}
  start: null                  # 예: 2020-01-01  (비면 오늘부터 years년 전)
  end: null                    # 예: 2025-12-31  (비면 오늘까지, 매일 이어받기)
  years: 3
  daily: true                  # 일봉
  minute:                      # N분봉 (선택) — 키움이 주는 범위까지(보통 최근 몇 달), 그 뒤로는 매일 이어받아 쌓임
    symbols: []                # 종목 목록, 또는 all = 위 범위 전체 (오래 걸림)
    intervals: [1]             # 1 3 5 10 15 30 45 60 (1분봉만 받아도 차트에서 3·5·10분으로 묶어 볼 수 있음)
    start: null
  tick:                        # N틱봉 (선택) — 키움은 최근 며칠만 줌, 매일 받아야 쌓임
    symbols: []
    units: [1]                 # 1 3 5 10 30
    start: null
    max_pages: 50              # 종목·하루당 최대 요청 수 (1틱은 하루에 수만 건)
# 호가창은 키움이 지난 기록을 주지 않아 받을 수 없습니다 (실시간으로 쌓은 것만 data/market/orderbook/).

# 차트 이동평균선 — 저장하지 않고 볼 때 종가·거래량으로 계산
chart:
  ma: [5, 20, 60, 120]         # 가격 이동평균 (봉 개수)
  volume_ma: [20]              # 거래량 이동평균

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


def update_sections(updates: dict, path: Path | None = None) -> Path:
    """config.yaml의 최상위 항목 몇 개만 바꿔 쓴다 (운영 화면 저장). 나머지 줄·주석은 그대로, 바꾸기 전 파일은 config.yaml.bak.
    항목 블록 = 'key:' 줄부터 들여쓴 줄·빈 줄이 이어지는 데까지(뒤쪽 빈 줄은 남김). 없던 항목은 끝에 붙인다."""
    p = path or home() / "config.yaml"
    old = p.read_text(encoding="utf-8") if p.exists() else TEMPLATE
    lines = old.splitlines(keepends=True)
    for key, value in updates.items():
        flow = None if isinstance(value, (dict, list)) else False           # 값 하나면 '{key: v}'가 아니라 'key: v'
        block = yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False, default_flow_style=flow, width=120)
        start = next((i for i, ln in enumerate(lines) if ln.startswith(f"{key}:")), None)
        if start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(block)
            continue
        end = start + 1
        while end < len(lines) and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines[start:end] = [block]
    text = "".join(lines)
    yaml.safe_load(text)                                                # 깨진 YAML이면 쓰기 전에 예외
    if p.exists():
        p.with_suffix(".yaml.bak").write_text(old, encoding="utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


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


def set_kiwoom(path: Path | None = None, **flags: bool) -> None:
    """config.yaml의 kiwoom 아래 참/거짓 값(is_mock·real_orders)만 바꾼다 (주석·나머지 설정은 그대로).
    로그인 화면에서 키 종류를 고를 때, 운영 탭에서 실전 주문에 동의할 때."""
    import re
    p = path or home() / "config.yaml"
    text = p.read_text(encoding="utf-8") if p.exists() else TEMPLATE
    for name, on in flags.items():
        val = "true" if on else "false"
        text, n = re.subn(rf"(?m)^(\s+{name}:\s*)(true|false)\b", rf"\g<1>{val}", text, count=1)
        if n:
            continue
        m = re.search(r"(?m)^kiwoom:[^\n]*\n", text)
        if m:
            text = text[:m.end()] + f"  {name}: {val}\n" + text[m.end():]
        else:
            text = text.rstrip("\n") + f"\n\nkiwoom:\n  {name}: {val}\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def set_is_mock(is_mock: bool, path: Path | None = None) -> None:
    set_kiwoom(path, is_mock=is_mock)


def data_dir(cfg: dict) -> Path:
    """시세·DB 폴더 — config data_dir (상대 경로는 KTRADE_HOME 기준), 비면 <KTRADE_HOME>/data."""
    h = Path(cfg["_home"])
    d = cfg.get("data_dir")
    return (h / Path(str(d)).expanduser()) if d else h / "data"


def set_data_dir(cfg: dict, new: str | Path, move: bool = True) -> tuple[Path, list[str]]:
    """데이터 폴더를 바꾼다: (move면) 지금 폴더의 DB·시세를 옮긴 뒤 config.yaml data_dir에 적는다.
    돌려주는 것 = (새 폴더, 옮긴 이름). 사용자 폴더 안 data로 돌아가면 data_dir을 비운다."""
    h = Path(cfg["_home"])
    dst = Path(str(new).strip().strip('"')).expanduser()
    if not str(new).strip():
        raise ValueError("폴더를 입력하세요")
    dst = (h / dst if not dst.is_absolute() else dst).resolve()
    if dst.exists() and not dst.is_dir():
        raise ValueError(f"폴더가 아니라 파일입니다: {dst}")
    moved: list[str] = []
    if move:
        from ktrade.data.store import move_data
        moved = move_data(data_dir(cfg), dst)
    else:
        dst.mkdir(parents=True, exist_ok=True)
    update_sections({"data_dir": None if dst == (h / "data").resolve() else str(dst)}, h / "config.yaml")
    return dst, moved


def paths(cfg: dict) -> dict[str, Path]:
    h, d = Path(cfg["_home"]), data_dir(cfg)
    return {"home": h, "data": d, "db": d / "market.db", "plans": h / "plans", "strategies": h / "strategies",
            "logs": h / "logs", "experiments": h / "experiments"}
