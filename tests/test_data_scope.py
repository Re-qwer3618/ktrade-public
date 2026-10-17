"""사용자가 정하는 범위: 수집(종목·시장·기간·분봉)과 백테스트(기간·대상 종목)."""
from __future__ import annotations

import pandas as pd
import pytest

from ktrade import cli
from ktrade.data import collect as C
from ktrade.sdk.context import DataView


def test_parse_codes_accepts_common_notations():
    assert C.parse_codes(["005930", "A000660", "5930", "# 주석", "", "035720,카카오", "005930"]) == \
        ["005930", "000660", "035720"]
    with pytest.raises(ValueError):
        C.parse_codes(["삼성전자"])


def test_scope_from_config_and_cli_override(cfg, home):
    cfg["collect"].update({"symbols": ["005930"], "start": "2021-01-04", "end": "2023-12-29",
                           "minute": {"symbols": ["005930"], "start": "2026-09-01"}})
    s = C.resolve_scope(cfg)
    assert s.symbols == ["005930"] and (s.start, s.end) == ("2021-01-04", "2023-12-29")
    assert s.minute_symbols == ["005930"] and s.minute_start == "2026-09-01"
    o = C.resolve_scope(cfg, symbols=["000660"], start="2020-01-02", end=None)
    assert o.symbols == ["000660"] and o.start == "2020-01-02" and o.end == "2023-12-29"  # 준 옵션만 덮어씀
    (home / "my.txt").parent.mkdir(parents=True, exist_ok=True)
    (home / "my.txt").write_text("# 관심\n005930\n000660 SK하이닉스\n", encoding="utf-8")
    assert C.resolve_scope(cfg, symbols_file="my.txt").symbols == ["005930", "000660"]
    cfg["collect"].update({"symbols": [], "end": None})
    assert C.resolve_scope(cfg).symbols is None          # 비면 시장 전체
    with pytest.raises(ValueError):
        C.resolve_scope(cfg, start="2024-01-01", end="2023-01-01")
    with pytest.raises(ValueError):
        C.resolve_scope(cfg, start="2024/01/01")


def test_target_symbols_market_mode_excludes(market, cfg):
    store, _ = market
    cfg["collect"]["exclude"] = ["BBB002"]
    assert C.target_symbols(store, C.resolve_scope(cfg)) == ["AAA001"]      # CCC003 관리종목, BBB002 제외
    cfg["collect"]["market_names"] = ["코스닥"]
    cfg["collect"]["exclude"] = []
    assert C.target_symbols(store, C.resolve_scope(cfg)) == ["BBB002"]


def test_fetch_from_incremental_and_backfill():
    assert C.fetch_from(None, None, "2020-01-02", None) == "2020-01-02"               # 처음
    assert C.fetch_from("2020-01-02", "2024-05-02", "2020-01-02", None) == "2024-05-02"  # 이어받기
    assert C.fetch_from("2022-01-03", "2024-05-02", "2020-01-02", None) == "2020-01-02"  # 앞당긴 시작 → 채움
    assert C.fetch_from("2020-01-02", "2023-12-29", "2020-01-02", "2023-12-29") is None  # 이미 다 있음


def test_dataview_symbol_filter(market):
    store, days = market
    v = DataView(store, days[-1], {"AAA001"})
    assert set(v.daily_all(5)["symbol"]) == {"AAA001"}
    assert list(v.universe()["code"]) == ["AAA001"]
    assert v.daily("BBB002").empty and not v.daily("AAA001").empty
    assert not v.index().empty                                   # 지수는 항상 보임


def test_backtest_symbols_and_period(market, cfg, capsys, monkeypatch):
    store, days = market
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    cli.main(["backtest", "example_breakout", "--start", days[21], "--end", days[-1], "--symbols", "BBB002"])
    out = capsys.readouterr()
    assert "1종목 지정" in out.out and "청산된 거래가 없습니다" in out.out            # AAA001을 안 보면 후보 없음
    cli.main(["backtest", "example_breakout", "--start", days[21], "--end", days[-1], "--symbols", "AAA001,ZZZ999"])
    out = capsys.readouterr()
    assert "DB에 일봉이 없는 종목 1개" in out.err and "1종목 지정" in out.out
    cfg["backtest"].update({"start": days[21], "end": "2099-01-01"})                   # 끝은 DB 마지막 거래일로
    cli.main(["backtest", "example_breakout"])
    assert f"{days[21]} ~ {days[-1]}" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["backtest", "example_breakout", "--symbols", "ZZZ999"])


def test_data_and_collect_dry_run(market, cfg, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    cli.main(["data"])
    out = capsys.readouterr().out
    assert "일봉: 3종목" in out and "지수 코스피: 80일" in out
    cli.main(["data", "--symbols", "AAA001"])
    assert "AAA001" in capsys.readouterr().out
    cli.main(["collect", "--symbols", "005930,000660", "--start", "2020-01-02", "--dry-run"])
    out = capsys.readouterr()
    assert "종목 2개 직접 지정 · 일봉 2020-01-02 ~ 오늘" in out.err and "아무것도 받지 않았습니다" in out.out


def test_collect_uses_scope_with_fake_api(market, cfg, monkeypatch):
    """키움 없이: 지정 종목만, 기간 끝(end)까지만 저장."""
    store, _ = market
    calls = []

    class Fake:
        def __init__(self, cfg):
            pass

        def universe(self):
            return pd.DataFrame(columns=["code", "name", "market", "market_name", "state"])

        def index(self, code, since):
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        def daily(self, sym, since):
            calls.append((sym, since))
            d = pd.bdate_range("2019-12-30", "2020-01-10").strftime("%Y-%m-%d")
            return pd.DataFrame({"date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1})

    import ktrade.data.kiwoom as K
    monkeypatch.setattr(K, "KiwoomData", Fake)
    scope = C.resolve_scope(cfg, symbols=["005930"], start="2020-01-02", end="2020-01-08")
    st = C.collect(store, cfg, scope)
    assert calls == [("005930", "2020-01-02")] and st["symbols"] == 1
    assert store.daily_range("005930")[1] == "2020-01-08"
    C.collect(store, cfg, scope)                                 # 두 번째는 이미 범위를 다 가짐 → 다시 안 받음
    assert len(calls) == 1
