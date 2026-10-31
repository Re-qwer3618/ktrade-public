"""백테스트 이용 코드(서명·로그인·잠금) · 관리자 발급 · 실전 주문 동의."""
from __future__ import annotations

from datetime import date

import pytest
import yaml

from conftest import TEST_SEED
from ktrade import access, cli
from ktrade import config as config_mod


def test_ed25519_rfc8032_vector():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pub = access.public_key(seed)
    assert pub.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig = access.sign(seed, b"")
    assert sig.hex().startswith("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015")
    assert access.verify_sig(pub, b"", sig) and not access.verify_sig(pub, b"x", sig)


def test_code_check(home):
    code = access.issue(TEST_SEED, "kim", "backtest", 30, today=date(2026, 1, 1))
    assert access.check(code, "kim", today=date(2026, 1, 31))["role"] == "backtest"
    with pytest.raises(ValueError, match="만료"):
        access.check(code, "kim", today=date(2026, 2, 1))
    with pytest.raises(ValueError, match="맞지 않"):
        access.check(code, "lee", today=date(2026, 1, 2))
    other = access.issue(bytes(32), "kim", "admin", None)                 # 등록 안 된 키로 만든 코드
    with pytest.raises(ValueError, match="서명"):
        access.check(other, "kim")
    head, body, sig = code.split(".")
    forged = access._b64(access._unb64(body).replace(b"backtest", b"admin\x00\x00\x00"))
    with pytest.raises(ValueError):
        access.check(f"{head}.{forged}.{sig}", "kim")
    with pytest.raises(ValueError, match="형식"):
        access.check("아무거나", "kim")


def test_backtest_locked_without_login(home, monkeypatch, capsys):
    monkeypatch.delenv("KTRADE_ACCESS_CODE")
    assert access.current() is None
    for argv in (["test", "x"], ["backtest", "x"], ["experiments", "x"], ["compare", "x"]):
        with pytest.raises(SystemExit, match="이용 코드"):
            cli.main(argv)
    code = access.issue(TEST_SEED, "kim", "backtest", 30)
    cli.main(["access", "login", "--id", "kim", "--code", code])
    info = access.current()
    assert access.can_backtest(info) and not access.is_admin(info)
    assert (home / "access.json").exists()
    with pytest.raises(SystemExit, match="실험 기록이 없"):                 # 잠금은 풀렸고, 기록이 없을 뿐
        cli.main(["experiments", "x"])
    cli.main(["access", "logout"])
    assert access.current() is None
    with pytest.raises(SystemExit, match="로그인 실패"):
        cli.main(["access", "login", "--id", "lee", "--code", code])


def test_admin_issue(home, monkeypatch, capsys):
    pub, new = access.admin_init()
    assert new and (home / "admin" / "signing_key").exists()
    assert access.admin_init() == (pub, False)
    monkeypatch.setattr(access, "PUBLIC_KEYS", (pub,))
    cli.main(["admin", "issue", "park", "--role", "admin", "--days", "0", "--note", "테스트"])
    code = capsys.readouterr().out.split("이용 코드: ")[1].split()[0]
    assert access.check(code, "park")["exp"] is None
    assert access.issued()[0]["id"] == "park" and access.issued()[0]["note"] == "테스트"


def test_real_orders_consent(home):
    real = {"broker": "kiwoom", "kiwoom": {"is_mock": False}}
    ok, why = cli._hts_trade_allowed(real)
    assert not ok and "동의" in why
    ok, why = cli._hts_trade_allowed({**real, "kiwoom": {"is_mock": False, "real_orders": True}})
    assert ok and "실전" in why
    p = home / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(config_mod.TEMPLATE, encoding="utf-8")
    config_mod.set_kiwoom(is_mock=False, real_orders=True)
    k = yaml.safe_load(p.read_text(encoding="utf-8"))["kiwoom"]
    assert k == {"is_mock": False, "real_orders": True}
    cli.main(["real-orders", "off"])
    assert config_mod.load()["kiwoom"]["real_orders"] is False
    p.write_text("broker: kiwoom\nkiwoom:\n  is_mock: false\n", encoding="utf-8")      # 예전 설정 파일 (real_orders 줄 없음)
    config_mod.set_kiwoom(real_orders=True)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["kiwoom"] == {"real_orders": True, "is_mock": False}


def test_dashboard_tabs_by_access(home, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from test_dashboard_ops import APP, _filled_store
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(config_mod.TEMPLATE, encoding="utf-8")
    _filled_store(home / "data", "market.db")
    at = AppTest.from_file(str(APP), default_timeout=60).run()                 # 관리자 코드(conftest) → 정식판 + AI + 관리자
    assert not at.exception, at.exception
    tabs = [t.label for t in at.tabs]
    assert "🧪 백테스트 정식판" in tabs and "🛠️ 관리자" in tabs
    assert any("AI 기능 사용 가능" in x.value for x in at.sidebar.success)                # 관리자 = AI판
    monkeypatch.delenv("KTRADE_ACCESS_CODE")
    at = AppTest.from_file(str(APP), default_timeout=60).run()                 # 코드 없음 → 기본 기능 + 체험판
    assert not at.exception, at.exception
    tabs = [t.label for t in at.tabs]
    assert tabs[0] == "🕹️ 자동매매" and "🧪 백테스트 체험판" in tabs and "🛠️ 관리자" not in tabs
    assert any(b.label == "🔓 백테스트 정식판 로그인" for b in at.sidebar.button)


def test_dashboard_without_chart_data(home, monkeypatch):
    """키움 키만 있고 차트 데이터가 없어도 운영·조건식 등 기본 화면이 열린다 (데이터는 백테스트용 선택)."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from test_dashboard_ops import APP
    from ktrade import credentials
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(config_mod.TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(credentials, "get", lambda n: "x" if n.startswith("KIWOOM") else "")
    monkeypatch.delenv("KTRADE_ACCESS_CODE")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    tabs = [t.label for t in at.tabs]
    assert tabs[0] == "🕹️ 자동매매" and "🗓️ 매매계획" in tabs
    assert any(m.label == "최근 일봉" and m.value == "받지 않음 (선택)" for m in at.metric)


def test_ai_access_and_parsing(monkeypatch):
    """AI판: 관리자만(구독 결제 전), 키 없으면 친절한 오류, 답에서 두 파일 꺼내기."""
    from ktrade import ai, credentials
    assert access.can_ai({"role": "admin"}) and not access.can_ai({"role": "backtest"}) and not access.can_ai(None)
    monkeypatch.setattr(credentials, "get", lambda n: "")
    with pytest.raises(ai.AiError, match="API 키가 없습니다"):
        ai.ask({}, "s", "p", "gemini")
    y, py = ai.parse_files("설명\n```yaml\nid: x\n```\n그리고\n```python\nclass X: pass\n```")
    assert y == "id: x\n" and py == "class X: pass\n"
    with pytest.raises(ai.AiError):
        ai.parse_files("코드 없음")
    assert ai.settings({"ai": {"models": {"gemini": "g-x"}}})["models"] == {**config_mod.DEFAULTS["ai"]["models"], "gemini": "g-x"}
