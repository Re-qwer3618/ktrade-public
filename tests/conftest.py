"""테스트용 가짜 시장: 지수 2개, 종목 3개, 평일 80일.

AAA: 60일째(인덱스 59)에 20일 최고 종가를 +3% 돌파, 다음 날부터 하루 약 +84원씩 오른다.
BBB: 횡보 (후보 없음). CCC: 관리종목 (예제가 제외).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ktrade import config as config_mod
from ktrade.data.store import Store


def _days(n=80):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-01-01", periods=n)]


def make_market(store: Store) -> list[str]:
    days = _days()
    for code in ("001", "101"):
        store.upsert_index(code, pd.DataFrame({"date": days, "open": 100.0, "high": 101.0, "low": 99.0,
                                               "close": np.linspace(100, 104, len(days)), "volume": 1}))
    store.upsert_universe(pd.DataFrame({"code": ["AAA001", "BBB002", "CCC003"], "name": ["에이", "비", "씨"],
                                        "market": ["0", "10", "10"], "market_name": ["거래소", "코스닥", "코스닥"],
                                        "state": ["증거금40%", "증거금40%", "관리종목"]}))
    n = len(days)
    a = np.full(n, 10000.0)
    a[59] = 10300.0
    a[60:] = np.linspace(10400, 12000, n - 60)
    vol = np.full(n, 100000)
    vol[59] = 150000

    def bars(close, v):
        return pd.DataFrame({"date": days, "open": close * 0.995, "high": close * 1.01, "low": close * 0.99,
                             "close": close, "volume": v})
    da = bars(a, vol)
    da.loc[60, "open"] = 10350.0
    store.upsert_daily("AAA001", da)
    store.upsert_daily("BBB002", bars(np.full(n, 5000.0), np.full(n, 200000)))
    c = np.full(n, 8000.0)
    c[59] = 8300.0
    store.upsert_daily("CCC003", bars(c, vol))
    return days


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KTRADE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    monkeypatch.delenv("KIWOOM_APP_SECRET", raising=False)
    monkeypatch.setenv("KTRADE_NO_KEYRING", "1")      # 테스트가 실제 자격 증명 저장소를 건드리지 않게
    return tmp_path / "home"


@pytest.fixture
def cfg(home):
    c = config_mod.load()
    c["strategies"]["enabled"] = [{"id": "example_breakout", "params": {"min_avg_value": 0}}]
    c["strategies"]["require_backtest"] = False
    return c


@pytest.fixture
def market(cfg):
    store = Store(config_mod.paths(cfg)["db"])
    days = make_market(store)
    return store, days
