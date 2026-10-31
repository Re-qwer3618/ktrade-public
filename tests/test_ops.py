from __future__ import annotations

import json
import platform
from datetime import datetime
from pathlib import Path

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
    import ktrade.dashboard as dash                   # app.py를 import하면 스크립트가 bare 모드로 한 번 돌아 버린다 — 경로만
    at = AppTest.from_file(str(Path(dash.__file__).parent / "app.py"), default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("AAA001" in str(df.value.to_dict()) for df in at.dataframe)


def test_schedule_settings_from_config(home):
    cfg = {"schedule": {"daily": "17:05", "run": None, "days": ["TUE", "MON"]}}
    s = schedule.settings(cfg)
    assert s == {"daily": "17:05", "run": None, "days": ["MON", "TUE"]}
    assert schedule.check({"daily": "25:00", "run": "08:50", "days": []}) and not schedule.check(s)
    lines = schedule.cron_lines(cfg)
    assert len(lines) == 1 and lines[0].startswith("5 17 * * 1,2 ") and "ktrade daily" in lines[0]
    if platform.system() == "Windows":
        out = schedule.install(dry_run=True, cfg=cfg)
        assert "/ST 17:05" in out and "MON,TUE" in out and "/Delete /F /TN ktrade-run" in out
    with pytest.raises(ValueError):
        schedule.install(dry_run=True, cfg={"schedule": {"daily": "9:5"}})


def test_plan_review_reasons_and_virtual_result(tmp_path):
    """C: 계획마다 실제 결과·이유(매수함·시가 미달·구간 미도달·실행 안 됨) + 계획대로였다면."""
    from datetime import datetime as DT

    from ktrade.data.store import Store
    store = Store(tmp_path / "m.db")
    e = {"min_open": 100, "min_price": 100, "max_price": 110, "after": "15:10"}
    plans = [{"plan_id": f"p{i}", "symbol": f"00000{i}", "name": f"S{i}", "rank": i, "stop": 90, "target": 130, "entry": e,
              "max_hold_days": 5, "basis": "테스트"} for i in range(1, 5)]
    docs = [{"strategy": "brk", "version": "v1", "asof": "2026-10-28", "plans": plans}]
    store.add_plan_trade(plans[0], "brk", "v1", 10, 105, "2026-10-29T15:11:00")
    store.log_decision("brk", "000002", "PLAN_SKIP", "NONE", "p2 시가 95 < 조건 100 — 오늘 매수 안 함", "2026-10-29T15:10:00")
    store.log_decision("brk", "000003", "PLAN_WAIT", "NONE", "p3 현재가 120가 매수 구간 밖 — 계속 확인", "2026-10-29T15:10:00")
    fake = {"000001": {"triggered": True, "reason": "목표가", "ret_pct": 3.2}, "000002": {"triggered": False}}
    t = report.plan_review(store, docs, "2026-10-28", simulate_fn=lambda s, p, a, c: fake.get(p.symbol),
                           now=DT(2026, 10, 30, 10, 0))
    assert report.exec_day(store, "2026-10-28") == "2026-10-29"               # 지수 일봉이 없으면 다음 평일
    got = dict(zip(t["코드"], zip(t["결과"], t["이유"], t["계획대로였다면"])))
    assert got["000001"][0] == "매수함" and got["000001"][2] == "도달 · 목표가 +3.20%"
    assert got["000002"][:2] == ("안 삼", "시가 95 < 조건 100 — 오늘 매수 안 함") and got["000002"][2] == "조건 미도달"
    assert got["000003"][0] == "안 삼" and "장 마감까지 매수 구간에 안 들어옴" in got["000003"][1]
    assert got["000004"][0] == "실행 안 됨" and got["000004"][2] == "데이터 없음"
    t2 = report.plan_review(store, docs, "2026-10-28", simulate_fn=lambda *a: None, now=DT(2026, 10, 29, 11, 0))
    assert dict(zip(t2["코드"], t2["결과"]))["000003"] == "대기"                # 실행일 장중엔 아직 대기
