"""체험 모드 — 키움 키 없이 ktrade가 무엇을 하는지 둘러보는 가상 데이터.

<KTRADE_HOME>/demo/ 를 별도 사용자 폴더처럼 쓴다 (실제 설정·데이터와 섞이지 않음):
  data/market.db   가상 종목 24개·지수 2개, 최근 약 2년 평일 일봉 (실제 시세가 아님)
  experiments/     예제 매매조건 2개(example_breakout·example_pullback)를 백테스트한 실험 기록
  plans/           최근 거래일들의 매매 계획
  plan_trades      백테스트 거래 일부를 '매매 기록'처럼 넣어 둠 (가상)
가상 데이터는 추세·횡보 구간을 섞은 난수로 만든다. 결과 숫자에는 아무 의미가 없고, 화면과 흐름을 보여 주는 용도다.
"""
from __future__ import annotations

import copy
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ktrade.config import paths
from ktrade.data.store import Store

VERSION = 1                       # 생성 방식이 바뀌면 올림 → 다시 만든다
STRATEGIES = ("example_breakout", "example_pullback")
NAMES = ["가상전자", "체험반도체", "샘플바이오", "연습자동차", "모의화학", "가상소프트", "체험에너지", "샘플금융",
         "연습건설", "모의통신", "가상게임", "체험식품", "샘플조선", "연습철강", "모의제약", "가상항공",
         "체험유통", "샘플로봇", "연습배터리", "모의엔터", "가상해운", "체험보안", "샘플소재", "연습관리"]


def home(base_cfg: dict) -> Path:
    return Path(base_cfg["_home"]) / "demo"


def config(base_cfg: dict) -> dict:
    """체험용 설정: 사용자 설정을 바탕으로 폴더만 demo/ 로, 예제 매매조건 2개를 켠 상태."""
    cfg = copy.deepcopy(base_cfg)
    cfg["_home"] = str(home(base_cfg))
    cfg["_secrets"] = {"app_key": "", "app_secret": ""}
    cfg["_demo"] = True
    cfg["broker"] = "paper"
    cfg["strategies"] = {**cfg["strategies"], "dirs": [], "require_backtest": True,
                         "enabled": [{"id": s} for s in STRATEGIES]}
    return cfg


def ready(base_cfg: dict) -> bool:
    m = home(base_cfg) / "demo.json"
    try:
        return json.loads(m.read_text(encoding="utf-8")).get("version") == VERSION
    except (OSError, ValueError):
        return False


def _days(n: int, end: date | None = None) -> list[str]:
    end = end or date.today() - timedelta(days=1)
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=end, periods=n)]


def _walk(rng: np.random.Generator, n: int, start: float, vol: float) -> tuple[np.ndarray, np.ndarray]:
    """상승·하락·횡보 구간이 번갈아 오는 종가 + 그날 수익률."""
    drift = np.zeros(n)
    i = 0
    while i < n:
        span = int(rng.integers(25, 70))
        drift[i:i + span] = rng.choice([0.003, 0.001, 0.0, -0.001, -0.003])    # 오르는 쪽으로 치우치지 않게
        i += span
    ret = drift + rng.normal(0, vol, n)
    return start * np.exp(np.cumsum(ret)), ret


def _bars(rng: np.random.Generator, close: np.ndarray, ret: np.ndarray, days: list[str], base_vol: float) -> pd.DataFrame:
    n = len(close)
    prev = np.concatenate([[close[0]], close[:-1]])
    open_ = prev * (1 + rng.normal(0, 0.006, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    volume = base_vol * np.exp(rng.normal(0, 0.3, n)) * (1 + np.clip(ret, 0, None) * 40)   # 크게 오른 날 거래량 증가
    tick = 10 if close.mean() < 50000 else 100
    r = lambda a: np.round(a / tick) * tick  # noqa: E731
    return pd.DataFrame({"date": days, "open": r(open_), "high": r(high), "low": r(low), "close": r(close),
                         "volume": volume.astype(int)})


def make_market(store: Store, n_days: int = 500, seed: int = 7, end: date | None = None) -> list[str]:
    rng = np.random.default_rng(seed)
    days = _days(n_days, end)
    for code, start in (("001", 2500.0), ("101", 800.0)):
        c, r = _walk(rng, n_days, start, 0.009)
        store.upsert_index(code, _bars(rng, c, r, days, 4e8))
    codes = [f"DEMO{i:02d}" for i in range(1, len(NAMES) + 1)]
    store.upsert_universe(pd.DataFrame({
        "code": codes, "name": NAMES,
        "market": ["0" if i % 2 == 0 else "10" for i in range(len(codes))],
        "market_name": ["거래소" if i % 2 == 0 else "코스닥" for i in range(len(codes))],
        "state": ["관리종목" if name == "연습관리" else "" for name in NAMES]}))
    for code in codes:
        price = float(rng.choice([4000, 9000, 18000, 35000, 72000]))
        c, r = _walk(rng, n_days, price, float(rng.uniform(0.014, 0.026)))
        store.upsert_daily(code, _bars(rng, c, r, days, 3e9 / price))      # 하루 거래대금 약 30억 원
    return days


def build(base_cfg: dict, progress=None, n_days: int = 500, plan_days: int = 15) -> dict:
    """가상 DB → 예제 백테스트(실험 기록) → 최근 계획 → 가상 매매 기록. progress(문구)로 진행 상황을 알린다."""
    from ktrade.engine import backtest, experiments, loader
    from ktrade.engine.planner import run_plans

    say = progress or (lambda msg: None)
    h = home(base_cfg)
    if h.exists():
        shutil.rmtree(h)
    cfg = config(base_cfg)
    P = paths(cfg)
    say("가상 시세 만드는 중")
    store = Store(P["db"])
    days = make_market(store, n_days)
    found = loader.discover(cfg)
    start, end = days[70], days[-1]
    results = {}
    for sid in STRATEGIES:
        info = found[sid]
        cond = loader.load(info)
        rules = loader.exits_for(cfg, info)
        say(f"예제 매매조건 백테스트: {sid}")
        res = backtest.run(store, info, cond, rules, start, end, float(cfg["backtest"]["cost_pct"]),
                           int(cfg["risk"]["max_plans_per_strategy"]), float(cfg["backtest"]["oos_ratio"]))
        experiments.save(cfg, info, cond.params, rules, res, note="체험 모드 (가상 데이터)")
        res["version"] = info.version
        results[sid] = res
    say("최근 매매 계획 만드는 중")
    for d in days[-plan_days:]:
        run_plans(cfg, store, d)
    say("가상 매매 기록 넣는 중")
    _fake_trades(store, results, days, float(cfg["paper"]["starting_cash"]) * float(cfg["risk"]["max_position_pct"]) / 100)
    store.set_setting("live.heartbeat", {"ts": f"{days[-1]} 15:30", "note": "체험 모드"})
    (h / "demo.json").write_text(json.dumps({"version": VERSION, "built": date.today().isoformat(),
                                              "end": end}, ensure_ascii=False), encoding="utf-8")
    return {sid: r.get("trades", 0) for sid, r in results.items()}


def _fake_trades(store: Store, results: dict, days: list[str], budget: float) -> None:
    since = days[-90]
    for sid, res in results.items():
        t = res.get("table")
        if t is None or t.empty:
            continue
        for r in t[t["entry_date"] >= since].itertuples():
            plan = {"plan_id": f"demo-{sid}-{r.asof}-{r.symbol}", "symbol": r.symbol, "name": r.name}
            tid = store.add_plan_trade(plan, sid, res.get("version", "v1"), max(1, int(budget // r.entry)), r.entry, f"{r.entry_date}T15:10:00")
            store.update_plan_trade(tid, status="closed", entry_date=r.entry_date, entry_price=r.entry,
                                    exit_date=r.exit_date, exit_price=r.exit, exit_reason=r.reason,
                                    note="체험 모드 (가상)")
            store.log_decision(sid, r.symbol, "매수 조건 충족", "매수", f"{r.name} {r.entry:,.0f}원", f"{r.entry_date}T15:10:00")
            store.log_decision(sid, r.symbol, r.reason, "매도", f"{r.name} {r.exit:,.0f}원 ({r.ret_pct:+.1f}%)",
                               f"{r.exit_date}T15:20:00")
