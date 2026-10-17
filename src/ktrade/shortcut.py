"""바탕화면·시작 메뉴 바로가기 (ktrade shortcut install|remove|show) — 명령어가 낯선 사람도 더블클릭으로 쓰게.

  ktrade 대시보드 : 더블클릭 → 대시보드를 띄우고 브라우저로 연다 (창을 닫으면 대시보드도 꺼짐)
  ktrade 명령창   : ktrade 명령을 바로 쓸 수 있는 터미널 (점검 결과를 먼저 보여 줌)
  ktrade 폴더     : 설정·키·데이터·내 매매조건 폴더 (~/.ktrade)

Windows는 .lnk(바탕화면 + 시작 메뉴 'ktrade' 폴더), macOS는 바탕화면 .command, Linux는 .desktop(앱 메뉴 + 바탕화면).
KTRADE_SHORTCUT_DIR을 주면 그 폴더 한 곳에만 만든다(시험용).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ktrade.config import home

NAMES = {"dashboard": "ktrade 대시보드", "console": "ktrade 명령창", "folder": "ktrade 폴더"}


def ktrade_exe() -> str:
    """바로가기가 부를 ktrade 실행 파일. uv tool 설치면 ~/.local/bin/ktrade(.exe)."""
    found = shutil.which("ktrade")
    if found:
        return str(Path(found).resolve())
    cand = Path(sys.argv[0])
    for p in (cand, cand.with_suffix(".exe")):
        if p.exists() and p.stem == "ktrade":
            return str(p.resolve())
    raise RuntimeError("ktrade 실행 파일을 찾지 못했습니다 (설치 후 새 터미널에서 다시 실행하세요)")


def _ps(script: str) -> str:
    """-EncodedCommand(UTF-16) — 명령줄 코드 페이지와 상관없이 한글 경로가 그대로 간다."""
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def _q(s: str) -> str:  # PowerShell 작은따옴표 문자열
    return "'" + str(s).replace("'", "''") + "'"


def _windows_dirs() -> list[Path]:
    if os.getenv("KTRADE_SHORTCUT_DIR"):
        return [Path(os.environ["KTRADE_SHORTCUT_DIR"])]
    out = _ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Environment]::GetFolderPath('Desktop'); "
              "[Environment]::GetFolderPath('Programs')").splitlines()
    return [Path(out[0]), Path(out[1]) / "ktrade"]


def _windows_items(exe: str) -> list[dict]:
    h = home()
    bin_dir = str(Path(exe).parent)
    console = f"$env:Path = {_q(bin_dir)} + ';' + $env:Path; Set-Location {_q(h)}; & ktrade intro"  # 안내 문구는 ktrade intro가
    ps = shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    return [
        {"name": NAMES["dashboard"], "target": exe, "args": "dashboard --open", "icon": exe + ",0",
         "desc": "ktrade 대시보드를 띄우고 브라우저로 엽니다 (이 창을 닫으면 꺼짐)"},
        {"name": NAMES["console"], "target": ps, "args": f"-NoExit -NoProfile -ExecutionPolicy Bypass -Command \"{console}\"",
         "icon": ps + ",0", "desc": "ktrade 명령을 쓰는 터미널"},
        {"name": NAMES["folder"], "target": str(Path(os.getenv("WINDIR", r"C:\Windows")) / "explorer.exe"),
         "args": f"\"{h}\"", "icon": "", "desc": "ktrade 설정·데이터·매매조건 폴더"},
    ]


def install(dry_run: bool = False) -> str:
    exe = ktrade_exe()
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        dirs, items = _windows_dirs(), _windows_items(exe)
        lines = []
        for d in dirs:
            for it in items:
                lnk = d / f"{it['name']}.lnk"
                lines.append(f"{lnk}  →  {it['target']} {it['args']}")
                if dry_run:
                    continue
                d.mkdir(parents=True, exist_ok=True)
                # WScript.Shell은 시스템 코드 페이지 밖 글자(영문 Windows의 한글 등)가 든 파일 이름으로 저장하지 못한다(CI 실측) →
                # 영문 임시 이름으로 저장한 뒤 바꾼다
                tmp = d / f"_ktrade_{os.getpid()}.lnk"
                _ps(f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_q(tmp)}); "
                    f"$s.TargetPath = {_q(it['target'])}; $s.Arguments = {_q(it['args'])}; "
                    f"$s.WorkingDirectory = {_q(h)}; $s.Description = {_q(it['desc'])}; "
                    + (f"$s.IconLocation = {_q(it['icon'])}; " if it["icon"] else "") + "$s.Save()")
                os.replace(tmp, lnk)
        return ("만들 바로가기 (--dry-run):\n" if dry_run else "바로가기를 만들었습니다:\n") + "\n".join("  " + x for x in lines)
    if sys.platform == "darwin":
        d = Path(os.getenv("KTRADE_SHORTCUT_DIR") or Path.home() / "Desktop")
        files = {d / f"{NAMES['dashboard']}.command": f'#!/bin/sh\n"{exe}" dashboard --open\n',
                 d / f"{NAMES['console']}.command": f'#!/bin/sh\ncd "{h}"\n"{exe}" doctor --offline\n'
                                                     f'export PATH="{Path(exe).parent}:$PATH"\nexec "$SHELL" -l\n',
                 d / f"{NAMES['folder']}.command": f'#!/bin/sh\nopen "{h}"\n'}
    else:
        dirs = [Path(os.environ["KTRADE_SHORTCUT_DIR"])] if os.getenv("KTRADE_SHORTCUT_DIR") else \
            [Path.home() / ".local/share/applications"] + ([Path.home() / "Desktop"] if (Path.home() / "Desktop").is_dir() else [])

        def entry(name, cmd, term):
            return (f"[Desktop Entry]\nType=Application\nName={name}\nExec={cmd}\nTerminal={'true' if term else 'false'}\n"
                    "Categories=Office;Finance;\n")
        files = {}
        for d in dirs:
            files[d / "ktrade-dashboard.desktop"] = entry(NAMES["dashboard"], f'"{exe}" dashboard --open', True)
            files[d / "ktrade-console.desktop"] = entry(NAMES["console"], f'sh -c \'cd "{h}"; "{exe}" doctor --offline; exec "$SHELL"\'', True)
            files[d / "ktrade-folder.desktop"] = entry(NAMES["folder"], f'xdg-open "{h}"', False)
    if not dry_run:
        for f, body in files.items():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
            f.chmod(0o755)
    return ("만들 바로가기 (--dry-run):\n" if dry_run else "바로가기를 만들었습니다:\n") + "\n".join(f"  {f}" for f in files)


def _existing() -> list[Path]:
    if sys.platform == "win32":
        return [d / f"{n}.lnk" for d in _windows_dirs() for n in NAMES.values() if (d / f"{n}.lnk").exists()]
    if sys.platform == "darwin":
        d = Path(os.getenv("KTRADE_SHORTCUT_DIR") or Path.home() / "Desktop")
        return [d / f"{n}.command" for n in NAMES.values() if (d / f"{n}.command").exists()]
    dirs = [Path(os.environ["KTRADE_SHORTCUT_DIR"])] if os.getenv("KTRADE_SHORTCUT_DIR") else \
        [Path.home() / ".local/share/applications", Path.home() / "Desktop"]
    return [d / f"ktrade-{k}.desktop" for d in dirs for k in NAMES if (d / f"ktrade-{k}.desktop").exists()]


def remove(dry_run: bool = False) -> str:
    found = _existing()
    if not dry_run:
        for f in found:
            f.unlink()
        if sys.platform == "win32" and not os.getenv("KTRADE_SHORTCUT_DIR"):
            menu = _windows_dirs()[1]
            if menu.is_dir() and not any(menu.iterdir()):
                menu.rmdir()
    return ("지울 바로가기:\n" if dry_run else "바로가기를 지웠습니다:\n") + ("\n".join(f"  {f}" for f in found) or "  (없음)")


def status() -> str:
    found = _existing()
    return "\n".join(f"  {f}" for f in found) if found else "바로가기 없음 (ktrade shortcut install)"
