"""자동 실행 등록 (ktrade schedule) — 평일 장 마감 후 daily, 장 시작 전 run.

Windows: 작업 스케줄러에 ktrade-daily(평일 16:30), ktrade-run(평일 08:50) 두 작업을 현재 사용자로 등록한다(관리자 권한 불필요,
로그온해 있을 때 실행). macOS/Linux: crontab 줄을 보여 준다(직접 crontab -e로 붙여 넣기 — 남의 crontab을 함부로 고치지 않으려고).
--dry-run이면 등록 명령만 보여 주고 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys

from ktrade.config import home

TASKS = {"ktrade-daily": ("daily", "16:30"), "ktrade-run": ("run", "08:50")}


def _cmd(sub: str) -> str:
    env = f'set "KTRADE_HOME={home()}" && ' if os.getenv("KTRADE_HOME") else ""
    log = home() / "logs" / f"{sub}.log"
    return f'cmd /c "{env}"{sys.executable}" -m ktrade {sub} >> "{log}" 2>&1"'


def windows_commands() -> list[list[str]]:
    return [["schtasks", "/Create", "/F", "/TN", name, "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", at,
             "/TR", _cmd(sub)] for name, (sub, at) in TASKS.items()]


def cron_lines() -> list[str]:
    env = f"KTRADE_HOME={home()} " if os.getenv("KTRADE_HOME") else ""
    return [f"30 16 * * 1-5 {env}{sys.executable} -m ktrade daily >> {home()}/logs/daily.log 2>&1",
            f"50 8 * * 1-5 {env}{sys.executable} -m ktrade run >> {home()}/logs/run.log 2>&1"]


def install(dry_run: bool = False) -> str:
    (home() / "logs").mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        return "crontab -e 에 아래 두 줄을 붙여 넣으세요:\n" + "\n".join(cron_lines())
    cmds = windows_commands()
    if dry_run:
        return "\n".join(subprocess.list2cmdline(c) for c in cmds)
    for c in cmds:
        subprocess.run(c, check=True, capture_output=True)
    return f"등록: {', '.join(TASKS)} (작업 스케줄러에서 확인·변경 가능)"


def remove(dry_run: bool = False) -> str:
    if platform.system() != "Windows":
        return "crontab -e 에서 ktrade 두 줄을 지우세요."
    cmds = [["schtasks", "/Delete", "/F", "/TN", name] for name in TASKS]
    if dry_run:
        return "\n".join(subprocess.list2cmdline(c) for c in cmds)
    for c in cmds:
        subprocess.run(c, capture_output=True)
    return f"삭제: {', '.join(TASKS)}"


def status() -> str:
    if platform.system() != "Windows":
        return "crontab -l 로 확인"
    found = []
    for name in TASKS:
        r = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, text=True)
        if r.returncode == 0:
            found.append(name)
    return ("등록됨: " + ", ".join(found)) if found else "등록 안 됨 (ktrade schedule install)"
