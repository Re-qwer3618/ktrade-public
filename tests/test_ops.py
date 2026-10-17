from __future__ import annotations

import json
import platform
from datetime import datetime

import pytest

from ktrade import cli
from ktrade.broker.paper import PaperBroker
from ktrade.config import paths
from ktrade.engine import daily, doctor, live, planner, report, schedule


def test_daily_without_collect(market, cfg, monkeypatch):
    store, days = market
    import ktrade.engine.planner as pl
    monkeypatch.setattr(pl, "last_trading_day", lambda s, on_or_before=None: days[59])
    res = daily.run_daily(cfg, collect=False)
    assert res["steps"]["plan"]["strategies"] == {"example_breakout": 1}
    logs = list(paths(cfg)["logs"].glob("daily_*.json"))
    assert logs and json.loads(logs[0].read_text(encoding="utf-8"))["steps"]["plan"]["asof"] == days[59]


def test_report_after_trade(market, cfg, tmp_path):
    store, days = market
    planner.run_plans(cfg, store, days[59])
    broker = PaperBroker(tmp_path / "p.json", 1e7)

    class Q:
        def quotes(self, syms): return {s: self.quote(s) for s in syms}
        def quote(self, s): return {"price": 10400, "open": 10350, "high": 10400, "low": 10300, "volume": 1,
                                    "date": days[60].replace("-", "")}
    live.run(cfg, once=True, quote_source=Q(), broker=broker, now_fn=lambda: datetime.fromisoformat(f"{days[60]}T15:12:00"))
    txt = report.text(store, paths(cfg)["plans"])
    assert "example_breakout v2" in txt and "계획 1" in txt
    ex = report.executed(store)
    assert int(ex.loc[("example_breakout", "v2"), "open"]) == 1


def test_doctor_offline_reports_missing_key(cfg, market):
    rows = {name: ok for name, ok, _ in doctor.checks(cfg, online=False)}
    assert rows["키움 앱키"] is False and rows["켠 매매조건"] is True
    print(doctor.render(doctor.checks(cfg, online=False)))


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows 작업 스케줄러")
def test_schedule_dry_run_changes_nothing(home):
    out = schedule.install(dry_run=True)
    assert out.count("schtasks") == 2 and "ktrade-daily" in out and "-m ktrade daily" in out
    assert "KTRADE_HOME" in out  # 테스트처럼 다른 사용자 폴더를 쓰면 작업에도 넘긴다


def test_cli_help_lists_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for c in ("daily", "report", "doctor", "schedule", "new-strategy", "backtest"):
        assert c in out


def test_dashboard_renders(market, cfg):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    store, days = market
    planner.run_plans(cfg, store, days[59])
    import ktrade.dashboard.app as mod
    at = AppTest.from_file(mod.__file__, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("AAA001" in str(df.value.to_dict()) for df in at.dataframe)
