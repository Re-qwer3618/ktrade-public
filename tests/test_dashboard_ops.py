"""3단계 운영 화면: 설정 저장(주석 보존) · 상태 · 편입/보유/미체결/체결 · 시작·중지·긴급 정지 · 화면이 오류 없이 그려지는지."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from ktrade import config as C
from ktrade.broker.paper import PaperBroker
from ktrade.dashboard import ops
from ktrade.data.store import Store
from ktrade.engine.executor import HALT_KEY, Executor, halt_state
from ktrade.engine.risk import Risk

APP = Path(__file__).resolve().parents[1] / "src" / "ktrade" / "dashboard" / "app.py"
NOW = datetime(2026, 10, 27, 9, 30, 0)
DAY = NOW.strftime("%Y-%m-%d")


def test_update_sections_keeps_other_lines_and_comments(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(C.TEMPLATE, encoding="utf-8")
    C.update_sections({"hts_conditions": [{"name": "단타전략-2", "mode": "trade"}],
                       "risk": {**C.DEFAULTS["risk"], "daily_loss_limit": 300000}}, p)
    text = p.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    assert cfg["hts_conditions"] == [{"name": "단타전략-2", "mode": "trade"}] and cfg["risk"]["daily_loss_limit"] == 300000
    assert "# 키움에서 받아 DB에 쌓을 범위" in text and "#  - name: \"01_오전스켈핑\"" in text     # 다른 항목·예시 주석 그대로
    assert cfg["collect"]["market_names"] == ["거래소", "코스닥"] and cfg["broker"] == "paper"
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == C.TEMPLATE
    C.update_sections({"new_key": {"a": 1}}, p)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["new_key"] == {"a": 1}


def test_update_sections_shared_objects_and_repeated_saves(tmp_path):
    """조건식 여러 개가 같은 exits dict를 공유해도 앵커(&id001) 없이 쓰고, 여러 번 저장해도 목록이 불어나지 않는다."""
    p = tmp_path / "config.yaml"
    p.write_text(C.TEMPLATE, encoding="utf-8")
    ex = {"stop": {"type": "pct", "value": 3}}
    conds = [{"name": "A", "exits": ex}, {"name": "B", "exits": ex}]
    for n in (2, 2, 1, 3):
        C.update_sections({"hts_conditions": (conds * 2)[:n], "risk": dict(C.DEFAULTS["risk"])}, p)
        text = p.read_text(encoding="utf-8")
        assert "&id" not in text and len(yaml.safe_load(text)["hts_conditions"]) == n
    assert yaml.safe_load(text)["collect"]["market_names"] == ["거래소", "코스닥"]


def test_settings_roundtrip_and_validation(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(C.TEMPLATE, encoding="utf-8")
    cfg = C.load()
    df = pd.DataFrame([{"사용": True, "조건식": "단타전략-2", "모드": "매매", "매수 시작": "09:00", "매수 끝": "10:30",
                        "하루 매수 한도": 5, "확인(초)": 0, "추격 한도%": 1.0, "이탈 시 매도": False},
                       {"사용": False, "조건식": "안씀", "모드": "관찰", "매수 시작": "09:00", "매수 끝": "15:20",
                        "하루 매수 한도": 3, "확인(초)": 0, "추격 한도%": 1.0, "이탈 시 매도": False}])
    sin = pd.DataFrame([{"비율%": 50, "하락%": 0}, {"비율%": 50, "하락%": 1.5}])
    sout = pd.DataFrame([{"비율%": 50, "상승%": 2}])
    ex = ops.exits_from_form(3, True, 4, 2, 1, 1, sin, sout)
    conds = ops.conditions_from_table(df, [], ex)
    assert [c["name"] for c in conds] == ["단타전략-2"] and conds[0]["exits"]["scale_in"][1]["drop_pct"] == 1.5
    assert ops.save_settings(cfg, conds, {"daily_loss_limit": 200000, "buy_window": ["09:00", "14:30"]}) == []
    cfg2 = C.load()
    assert cfg2["hts_conditions"][0]["buy_window"] == ["09:00", "10:30"] and cfg2["risk"]["daily_loss_limit"] == 200000
    assert ops.conditions_table(cfg2)["조건식"].tolist() == ["단타전략-2"]
    assert ops.exits_defaults(cfg2)["scale_out"] == [{"pct": 50.0, "gain_pct": 2.0}]
    bad = ops.exits_from_form(1, True, 4, 0, 0, 1, sin, sout)              # 손절 1% < 2차 간격 1.5%
    errs = ops.save_settings(cfg2, ops.conditions_from_table(df, [], bad), {"buy_window": ["10:00", "09:00"]})
    assert any("분할매수 전에 손절" in e for e in errs) and any("시작 < 끝" in e for e in errs)
    assert C.load()["risk"]["daily_loss_limit"] == 200000                    # 오류면 저장 안 함


def _filled_store(tmp_path, db="m.db"):
    store = Store(tmp_path / db)
    store.add_signal("단타전략-2", "14", "A", "I", "BUY", "매수 100주", 10000.0, "091000", "trade", "p-A", ts=f"{DAY}T09:10:00")
    store.add_signal("단타전략-2", "14", "B", "I", "SKIP", "가격 범위 밖", 500.0, "091100", "trade", ts=f"{DAY}T09:11:00")
    store.add_signal("단타전략-2", "14", "B", "D", "REMOVE", "", None, "091200", "trade", ts=f"{DAY}T09:12:00")
    broker = PaperBroker(tmp_path / "p.json", 10_000_000)
    px = {"A": 10000.0}
    ex = Executor(store, broker, Risk(10_000_000, max_position_pct=10), lambda s: {"price": px[s], "open": px[s]})
    ex.add_plan("hts:단타전략-2", "hts", {"plan_id": "p-A", "symbol": "A", "max_hold_days": 1,
                                         "entry": {"after": "09:00", "min_open": 0, "min_price": 0, "max_price": 1e9}})
    ex.check("A", NOW.replace(minute=10))
    px["A"] = 10200.0
    ex.guard(NOW)
    return store


def test_status_and_tables(tmp_path, cfg):
    store = _filled_store(tmp_path)
    store.set_setting("hts.heartbeat", {"ts": (NOW - timedelta(seconds=3)).isoformat(timespec="seconds"),
                                        "conds": ["단타전략-2"], "registered": ["단타전략-2"], "restarts": 0})
    s = ops.status(store, cfg, NOW)
    assert s["hts_running"] and not s["run_running"] and s["mode"].startswith("가상매매") and s["pnl"]["unrealized"] == 20_000
    assert not ops.status(store, cfg, NOW + timedelta(minutes=1))["hts_running"]
    ins = ops.inserts(store, DAY)
    assert dict(zip(ins["종목"], ins["상태"])) == {"A": "편입 중", "B": "이탈"}
    assert ins.set_index("종목").loc["B", "판단"] == "건너뜀"
    h = ops.holdings(store)
    assert h.iloc[0]["종목"] == "A" and h.iloc[0]["잔고수량"] == 100 and h.iloc[0]["매입가"] == 10000
    assert ops.pending(store, NOW).empty
    f = ops.executions(store, DAY)
    assert f.iloc[0]["구분"] == "매수" and f.iloc[0]["수량"] == 100
    assert not ops.recent_log(store, DAY).empty


def test_emergency_stop_release_and_watch_process(tmp_path, cfg):
    store = _filled_store(tmp_path)
    ops.emergency_stop(store)
    assert halt_state(store, datetime.now().strftime("%Y-%m-%d"))["flatten"]
    ops.release_halt(store)
    assert store.get_setting(HALT_KEY) == {}
    calls = []

    class P:
        pid = 4321

    def fake_popen(args, **kw):
        calls.append((args, kw))
        return P()
    assert ops.start_watch(cfg, store, NOW, popen=fake_popen)["reason"].startswith("조건식이 없습니다")
    cfg["hts_conditions"] = [{"name": "단타전략-2", "mode": "trade"}]
    assert "키움 로그인" in ops.start_watch(cfg, store, NOW, popen=fake_popen)["reason"]
    cfg["_secrets"] = {"app_key": "k", "app_secret": "s"}
    res = ops.start_watch(cfg, store, NOW, popen=fake_popen)
    assert res["ok"] and res["pid"] == 4321 and calls[0][0][-3:] == ["ktrade", "hts", "watch"]
    assert calls[0][1]["env"]["KTRADE_HOME"] == cfg["_home"] and store.get_setting("process.hts")["pid"] == 4321
    store.set_setting("hts.heartbeat", {"ts": NOW.isoformat(timespec="seconds")})
    assert ops.start_watch(cfg, store, NOW, popen=fake_popen)["reason"] == "이미 실행 중" and len(calls) == 1
    ops.stop_watch(store)
    assert store.get_setting("process.stop.hts")["at"]


def test_ops_tab_renders_and_saves(home):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(C.TEMPLATE.replace("hts_conditions: []", 'hts_conditions:\n  - {name: "단타전략-2", mode: trade}'),
                                      encoding="utf-8")
    _filled_store(home / "data", "market.db")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs][0] == "🕹️ 자동매매"
    labels = [m.label for m in at.metric]
    assert "당일 손익 (원)" in labels
    nums = {n.label: n for n in at.number_input}
    nums["당일 손실한도 (원)"].set_value(150000)
    next(b for b in at.button if b.label == "설정 저장").click()
    at.run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    saved = C.load()
    assert saved["risk"]["daily_loss_limit"] == 150000 and saved["hts_conditions"][0]["name"] == "단타전략-2"
    assert saved["hts_conditions"][0]["exits"]["stop"] == {"type": "pct", "value": 2.0}     # 조건식 기본 청산이 그대로 저장
    next(b for b in at.button if b.label == "정지하고 전량 매도").click()
    at.run()
    assert halt_state(Store(home / "data" / "market.db"), datetime.now().strftime("%Y-%m-%d"))


def test_duplicated_conditions_from_old_save_bug(home):
    """예전 저장 버그로 '- name:' 묵은 항목이 뒤에 남은 config — 앞(새) 것을 쓰고, 다시 저장하면 정리된다."""
    home.mkdir(parents=True, exist_ok=True)
    text = C.TEMPLATE.replace("hts_conditions: []", "hts_conditions:\n- name: A\n  mode: trade\n- name: A\n  mode: watch", 1)
    assert text != C.TEMPLATE
    (home / "config.yaml").write_text(text, encoding="utf-8")
    cfg = C.load()
    from ktrade.engine.signals import load_conditions
    assert [c["mode"] for c in load_conditions(cfg)] == ["trade"]
    df = ops.conditions_table(cfg)
    assert df["조건식"].tolist() == ["A"]
    assert ops.save_settings(cfg, ops.conditions_from_table(df, cfg["hts_conditions"], None), {}) == []
    assert [c["name"] for c in C.load()["hts_conditions"]] == ["A"]


def test_start_run_process(tmp_path, cfg):
    """자동매매 탭 '매매계획 실행' — ktrade run을 따로 띄우고, 실전은 실전 주문 동의 뒤에만(--live-confirm)."""
    store = _filled_store(tmp_path)
    calls = []

    class P:
        pid = 777

    def fake_popen(args, **kw):
        calls.append(args)
        return P()
    cfg["_secrets"] = {}
    assert "키움 로그인" in ops.start_run(cfg, store, NOW, popen=fake_popen)["reason"]
    cfg["_secrets"] = {"app_key": "k", "app_secret": "s"}
    res = ops.start_run(cfg, store, NOW, popen=fake_popen)
    assert res["ok"] and calls[-1][-1] == "run" and store.get_setting("process.run")["pid"] == 777
    store.set_setting("live.heartbeat", {"ts": NOW.isoformat(timespec="seconds")})
    assert ops.start_run(cfg, store, NOW, popen=fake_popen)["reason"] == "이미 실행 중"
    ops.stop_run(store)
    assert store.get_setting("process.stop.run")["at"]
    store.set_setting("live.heartbeat", {})
    cfg["broker"], cfg["kiwoom"] = "kiwoom", {**cfg["kiwoom"], "is_mock": False, "real_orders": False}
    assert "실전 주문 동의" in ops.start_run(cfg, store, NOW, popen=fake_popen)["reason"]
    cfg["kiwoom"]["real_orders"] = True
    assert ops.start_run(cfg, store, NOW, popen=fake_popen)["ok"] and calls[-1][-2:] == ["run", "--live-confirm"]
