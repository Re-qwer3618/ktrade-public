"""시세 저장소(Parquet 폴더) · 예전 SQLite 시세 옮기기 · 받을 범위 고르기(종류·가격·시가총액) · 분봉/틱 수집."""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from ktrade.data import collect as C
from ktrade.data.market import Market, resample
from ktrade.data.store import Store


def _bars(dates, close=100.0):
    return pd.DataFrame({"date": dates, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10})


def test_layout_upsert_and_reads(tmp_path):
    m = Market(tmp_path / "market")
    with m.batch():
        m.write_daily(_bars(["2025-12-30", "2026-01-02"]).assign(symbol="AAA001"))
        m.write_daily(_bars(["2026-01-02"], 200).assign(symbol="BBB002"))
        assert not m.files("daily")                                   # batch 안에서는 아직 안 씀
    assert [f.name for f in m.files("daily")] == ["2025.parquet", "2026.parquet"]   # 해마다 파일 하나
    m.write_daily(_bars(["2026-01-02"], 150).assign(symbol="AAA001"))  # 같은 키 → 새 값이 이김
    assert m.daily("AAA001")["close"].tolist() == [100.0, 150.0]
    assert m.closes("2026-01-02", ["AAA001", "BBB002", "ZZZ"]) == {"AAA001": 150.0, "BBB002": 200.0}
    assert m.daily("AAA001", after="2025-12-30", first=1)["date"].tolist() == ["2026-01-02"]
    assert set(m.daily_between("2026-01-01", "2026-01-31")["symbol"]) == {"AAA001", "BBB002"}
    assert m.daily_ranges().set_index("symbol").loc["AAA001", "bars"] == 2


def test_minute_month_files_resample_and_ticks(tmp_path):
    m = Market(tmp_path / "market")
    ts = [f"2026-10-30 09:{k:02d}:00" for k in range(10)]
    m.write_minute(pd.DataFrame({"symbol": "005930", "ts": ts, "open": range(10), "high": range(1, 11),
                                 "low": range(10), "close": range(10), "volume": 1}))
    assert m.files("minute", "1m")[0].name == "2026-10.parquet"
    five = m.minute("005930", "2026-10-30 09:00", "2026-10-30 15:30", interval=5)   # 5분봉을 안 받았으면 1분봉을 묶음
    assert five["ts"].tolist() == ["2026-10-30 09:00:00", "2026-10-30 09:05:00"]
    assert five["volume"].tolist() == [5, 5] and five["high"].tolist() == [5, 10]
    assert m.last_minute_ts("005930") == "2026-10-30 09:09:00"
    tick = pd.DataFrame({"symbol": "005930", "ts": ["2026-10-30 09:00:01"] * 3, "open": [1, 2, 3], "high": [1, 2, 3],
                         "low": [1, 2, 3], "close": [1, 2, 3], "volume": 1})
    m.write_tick(tick)
    m.write_tick(tick.iloc[:2])                                       # 다시 받으면 그 종목·날짜를 통째 교체
    got = m.tick("005930", "2026-10-30")
    assert got["close"].tolist() == [1, 2] and got["seq"].tolist() == [0, 1]
    assert m.tick_days("005930") == ["2026-10-30"] and m.disk()["tick"]["files"] == 1
    assert resample(pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]), 5).empty


def test_old_sqlite_market_tables_move_to_parquet(tmp_path):
    db = tmp_path / "data" / "market.db"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, "
              "PRIMARY KEY (symbol, date))")
    c.execute("CREATE TABLE index_ohlcv (index_code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
              "volume INTEGER, PRIMARY KEY (index_code, date))")
    c.execute("CREATE TABLE minute_ohlcv (symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, "
              "PRIMARY KEY (symbol, ts))")
    c.executemany("INSERT INTO ohlcv VALUES (?,?,1,1,1,?,5)", [("AAA001", "2024-12-31", 10), ("AAA001", "2025-01-02", 11)])
    c.execute("INSERT INTO index_ohlcv VALUES ('001','2025-01-02',1,1,1,2500,9)")
    c.execute("INSERT INTO minute_ohlcv VALUES ('AAA001','2025-01-02 09:00:00',1,1,1,1,1)")
    c.commit()
    c.close()
    s = Store(db, readonly=True)                                      # 대시보드(읽기 전용)로 먼저 열어도 옮긴다
    assert s.market.daily("AAA001")["close"].tolist() == [10.0, 11.0]
    assert s.last_index_date_before("9999-12-31") == "2025-01-02" and s.last_minute_ts("AAA001")
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"ohlcv", "index_ohlcv", "minute_ohlcv"} and "plan_trades" not in tables
    Store(db)                                                         # 다시 열어도 문제없음
    assert not (s.market.root / ".migrating").exists()


def _uni():
    rows = [("005930", "삼성전자", "거래소", "", 70000, 5_900_000_000, "0"),
            ("005935", "삼성전자우", "거래소", "", 58000, 800_000_000, "0"),
            ("069500", "KODEX 200", "거래소", "", 35000, 100_000_000, "0"),
            ("123450", "엔에이치스팩30호", "코스닥", "", 2000, 5_000_000, "0"),
            ("222220", "작은회사", "코스닥", "", 1500, 10_000_000, "3"),
            ("333330", "중간회사", "코스닥", "관리종목", 9000, 20_000_000, "0"),
            ("00088K", "한화3우B", "거래소", "", 20000, 1_000_000, "0")]
    return pd.DataFrame(rows, columns=["code", "name", "market_name", "state", "last_price", "list_count", "order_warning"])


def test_pick_kinds_warning_price_and_cap(cfg):
    cfg["collect"].update({"exclude_kinds": ["ETF", "스팩", "우선주"], "exclude_warning": True})
    assert C.pick(_uni(), C.resolve_scope(cfg))["code"].tolist() == ["005930"]      # 관리종목·투자유의도 빠짐
    cfg["collect"].update({"exclude_kinds": [], "exclude_warning": False, "exclude_states": [],
                           "price": {"min": 1800, "max": 40000}, "market_cap": {"min": 100}})
    assert C.pick(_uni(), C.resolve_scope(cfg))["code"].tolist() == ["069500", "123450", "333330", "00088K"]
    with pytest.raises(ValueError):
        C.resolve_scope({**cfg, "collect": {**cfg["collect"], "exclude_kinds": ["ELW"]}})
    with pytest.raises(ValueError):
        C.resolve_scope(cfg, price={"min": 5000, "max": 1000})
    with pytest.raises(ValueError):
        C.resolve_scope(cfg, minute_intervals="7")


def test_collect_minute_intervals_and_ticks(market, cfg, monkeypatch):
    store, _ = market
    seen = []

    class Fake:
        def __init__(self, cfg):
            pass

        def universe(self):
            return pd.DataFrame(columns=["code", "name", "market", "market_name", "state"])

        def index(self, code, since):
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        def minute(self, sym, since, max_pages=3, interval=1):
            seen.append(("m", sym, interval))
            return pd.DataFrame({"ts": ["2026-10-30 09:00:00"], "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})

        def tick(self, sym, since, max_pages=50, ticks=1):
            seen.append(("t", sym, ticks))
            return pd.DataFrame({"ts": ["2026-10-30 09:00:01"] * 2, "open": 1, "high": 1, "low": 1, "close": [1, 2],
                                 "volume": 1})

    import ktrade.data.kiwoom as K
    monkeypatch.setattr(K, "KiwoomData", Fake)
    scope = C.resolve_scope(cfg, symbols=["AAA001"], minute_symbols="all", daily=False,
                            minute_intervals="1,5", tick_symbols="AAA001", tick_units="10")
    assert "1·5분봉 범위 전체" in scope.describe() and "10틱 1종목" in scope.describe() and "일봉 안 받음" in scope.describe()
    st = C.collect(store, cfg, scope)
    assert ("m", "AAA001", 5) in seen and ("t", "AAA001", 10) in seen and st["bars"] == 0
    assert store.market.intervals("minute") == [1, 5] and store.market.tick("AAA001", "2026-10-30", 10)["close"].tolist() == [1, 2]
    assert Store(store.path).summary()["tick"] == {10: 1}


def test_data_dir_setting_and_move(market, cfg, tmp_path):
    from ktrade import config as config_mod
    store, _ = market
    store.market.write_minute(pd.DataFrame({"symbol": "AAA001", "ts": ["2026-10-30 09:00:00"], "open": 1, "high": 1,
                                            "low": 1, "close": 1, "volume": 1}))
    old = config_mod.paths(cfg)["data"]
    n_daily = store.market.daily_stats()["rows"]
    dst, moved = config_mod.set_data_dir(cfg, tmp_path / "elsewhere" / "kdata")
    assert set(moved) >= {"market.db", "market"} and not (old / "market.db").exists()
    cfg2 = config_mod.load()                                           # config.yaml data_dir에 적혀 다음부터 그쪽을 씀
    assert config_mod.paths(cfg2)["db"] == dst / "market.db"
    s = Store(config_mod.paths(cfg2)["db"], readonly=True)
    assert s.market.daily_stats()["rows"] == n_daily and s.last_minute_ts("AAA001")
    with pytest.raises(ValueError):                                    # 같은 이름이 이미 있는 폴더로는 안 옮김
        config_mod.set_data_dir(cfg2, dst)
    (old / "market.db").write_text("x")
    with pytest.raises(ValueError):
        config_mod.set_data_dir(cfg2, old)
    (old / "market.db").unlink()
    config_mod.set_data_dir(cfg2, old)                                  # 기본 폴더로 돌아오면 data_dir을 비움
    assert config_mod.load()["data_dir"] is None and (old / "market.db").exists()
    from ktrade import demo
    assert config_mod.paths(demo.config({**cfg2, "data_dir": str(dst)}))["data"] == demo.home(cfg2) / "data"
