"""바로가기: 지정 폴더(KTRADE_SHORTCUT_DIR)에 만들고 지운다 — 실제 바탕화면은 건드리지 않음."""
from __future__ import annotations

import shutil
import sys

import pytest

from ktrade import cli, shortcut


@pytest.fixture
def sc_dir(tmp_path, monkeypatch, home):
    d = tmp_path / "shortcuts"
    monkeypatch.setenv("KTRADE_SHORTCUT_DIR", str(d))
    monkeypatch.setattr(shortcut, "ktrade_exe", lambda: str(tmp_path / "bin" / ("ktrade.exe" if sys.platform == "win32" else "ktrade")))
    return d


def test_dry_run_makes_nothing(sc_dir):
    out = shortcut.install(dry_run=True)
    assert "ktrade 대시보드" in out or "ktrade-dashboard" in out
    assert not sc_dir.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows .lnk")
def test_windows_lnk_roundtrip(sc_dir, home):
    shortcut.install()
    files = sorted(p.name for p in sc_dir.glob("*.lnk"))
    assert files == sorted(f"{n}.lnk" for n in shortcut.NAMES.values())
    probe = sc_dir.parent / "probe.lnk"   # WScript.Shell은 영문 Windows에서 한글 이름 파일을 못 열어서 영문 이름 사본으로 읽는다
    shutil.copy(sc_dir / "ktrade 대시보드.lnk", probe)
    out = shortcut._ps(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{probe}'); "
                       "$s.TargetPath; $s.Arguments; $s.WorkingDirectory")
    target, args, wd = out.splitlines()
    assert target.endswith("ktrade.exe") and args == "dashboard --open" and wd == str(home)
    assert "바로가기 없음" not in shortcut.status()
    shortcut.remove()
    assert not list(sc_dir.glob("*.lnk")) and "바로가기 없음" in shortcut.status()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux .desktop")
def test_linux_desktop_files(sc_dir):
    shortcut.install()
    body = (sc_dir / "ktrade-dashboard.desktop").read_text(encoding="utf-8")
    assert "dashboard --open" in body and "Terminal=true" in body
    shortcut.remove()
    assert not list(sc_dir.glob("*.desktop"))


def test_cli_shortcut_show(sc_dir, capsys):
    cli.main(["shortcut", "show"])
    assert "바로가기 없음" in capsys.readouterr().out


def test_intro_prints_guide(cfg, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    cli.main(["intro"])
    out = capsys.readouterr().out
    assert "ktrade·Python" in out and "ktrade new 이름" in out
