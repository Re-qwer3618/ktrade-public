"""로그인(키 보관)·체험 모드."""
from __future__ import annotations

import yaml

from ktrade import config as config_mod
from ktrade import credentials, demo
from ktrade.data.store import Store
from ktrade.engine import experiments


def test_saved_keys_are_loaded(home):
    assert config_mod.load()["_secrets"]["app_key"] == ""
    where = credentials.save({"KIWOOM_APP_KEY": " key123456 ", "KIWOOM_APP_SECRET": "sec"})
    assert where.endswith("credentials.json")
    s = config_mod.load()["_secrets"]
    assert s == {"app_key": "key123456", "app_secret": "sec"}
    credentials.clear()
    assert config_mod.load()["_secrets"]["app_key"] == ""
    assert not (home / "credentials.json").exists()


def test_env_file_takes_priority(home, monkeypatch):
    credentials.save({"KIWOOM_APP_KEY": "saved", "KIWOOM_APP_SECRET": "saved"})
    monkeypatch.setenv("KIWOOM_APP_KEY", "fromenv")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "fromenv")
    assert config_mod.load()["_secrets"]["app_key"] == "fromenv"


def test_set_is_mock_keeps_rest(home):
    p = home / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(config_mod.TEMPLATE, encoding="utf-8")
    config_mod.set_is_mock(False)
    text = p.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["kiwoom"]["is_mock"] is False
    assert "# ktrade 설정" in text and text.count("is_mock:") == 1
    config_mod.set_is_mock(True)
    assert config_mod.load()["kiwoom"]["is_mock"] is True


def test_demo_build(home):
    base = config_mod.load()
    assert not demo.ready(base)
    counts = demo.build(base, n_days=160, plan_days=3)
    assert demo.ready(base) and set(counts) == set(demo.STRATEGIES)
    cfg = demo.config(base)
    store = Store(config_mod.paths(cfg)["db"], readonly=True)
    assert store.summary()["daily"]["symbols"] == len(demo.NAMES)
    for sid in demo.STRATEGIES:
        assert experiments.history(cfg, sid)
    assert len(list(config_mod.paths(cfg)["plans"].glob("*/*.json"))) >= 3
    assert not (home / "data" / "market.db").exists()          # 실제 데이터 폴더와 섞이지 않음
