"""자동 실행 등록 (ktrade schedule) — 장 마감 후 daily(수집 → 다음 날 계획), 장 시작 전 run(계획 실행).

시각·요일은 config.yaml schedule (기본 평일 16:30 daily, 08:50 run, 시각을 null로 두면 그 작업은 등록하지 않음) —
대시보드 🗓️ 계획 탭에서 바꾸면 저장과 함께 다시 등록한다.
Windows: 작업 스케줄러에 ktrade-daily, ktrade-run을 현재 사용자로 등록한다(관리자 권한 불필요, 로그온해 있을 때 실행).
macOS/Linux: crontab 줄을 보여 준다(직접 crontab -e로 붙여 넣기 — 남의 crontab을 함부로 고치지 않으려고).
--dry-run이면 등록 명령만 보여 주고 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import sys

from ktrade.config import DEFAULTS, home

TASKS = {"ktrade-daily": "daily", "ktrade-run": "run"}
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
DAY_KO = dict(zip(DAYS, "월화수목금토일"))
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def settings(cfg: dict | None = None) -> dict:
    """{daily: "16:30"|None, run: "08:50"|None, days: [MON..]} — config에 없으면 기본값."""
    s = {**DEFAULTS["schedule"], **((cfg or {}).get("schedule") or {})}
    return {"daily": s.get("daily") or None, "run": s.get("run") or None, "days": [d for d in DAYS if d in (s.get("days") or [])]}


def check(s: dict) -> list[str]:
    errs = [f"{k} 시각은 HH:MM (예: 16:30) — 지금 {s[k]!r}" for k in ("daily", "run") if s.get(k) and not _HHMM.match(str(s[k]))]
    if not s.get("days"):
        errs.append("요일을 하나 이상 고르세요")
    return errs


def _cmd(sub: str) -> str:
    env = f'set "KTRADE_HOME={home()}" && ' if os.getenv("KTRADE_HOME") else ""
    log = home() / "logs" / f"{sub}.log"
    return f'cmd /c "{env}"{sys.executable}" -m ktrade {sub} >> "{log}" 2>&1"'


def windows_commands(cfg: dict | None = None) -> list[list[str]]:
    """시각이 있는 작업은 /Create(/F = 있으면 바꿈), 시각이 없는 작업은 /Delete."""
    s = settings(cfg)
    out = []
    for name, sub in TASKS.items():
        if s[sub]:
            out.append(["schtasks", "/Create", "/F", "/TN", name, "/SC", "WEEKLY", "/D", ",".join(s["days"]), "/ST", s[sub],
                        "/TR", _cmd(sub)])
        else:
            out.append(["schtasks", "/Delete", "/F", "/TN", name])
    return out


def cron_lines(cfg: dict | None = None) -> list[str]:
    s = settings(cfg)
    env = f"KTRADE_HOME={home()} " if os.getenv("KTRADE_HOME") else ""
    dow = ",".join(str((DAYS.index(d) + 1) % 7) for d in s["days"])    # cron: 0 = 일요일
    return [f"{int(s[sub][3:])} {int(s[sub][:2])} * * {dow} {env}{sys.executable} -m ktrade {sub} >> {home()}/logs/{sub}.log 2>&1"
            for sub in TASKS.values() if s[sub]]


def install(dry_run: bool = False, cfg: dict | None = None) -> str:
    errs = check(settings(cfg))
    if errs:
        raise ValueError("; ".join(errs))
    (home() / "logs").mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        return "crontab -e 에 아래 줄을 붙여 넣으세요 (예전 ktrade 줄은 지우고):\n" + "\n".join(cron_lines(cfg))
    cmds = windows_commands(cfg)
    if dry_run:
        return "\n".join(subprocess.list2cmdline(c) for c in cmds)
    for c in cmds:
        subprocess.run(c, check=c[1] == "/Create", capture_output=True)    # 없는 작업 /Delete 실패는 괜찮음
    return "등록: " + (", ".join(c[4] for c in cmds if c[1] == "/Create") or "없음") + " (작업 스케줄러에서도 확인 가능)"


def remove(dry_run: bool = False) -> str:
    if platform.system() != "Windows":
        return "crontab -e 에서 ktrade 줄을 지우세요."
    cmds = [["schtasks", "/Delete", "/F", "/TN", name] for name in TASKS]
    if dry_run:
        return "\n".join(subprocess.list2cmdline(c) for c in cmds)
    for c in cmds:
        subprocess.run(c, capture_output=True)
    return f"삭제: {', '.join(TASKS)}"


def registered() -> dict[str, bool] | None:
    """{작업 이름: 등록 여부} — Windows가 아니면 None (crontab은 직접 확인)."""
    if platform.system() != "Windows":
        return None
    return {name: subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True).returncode == 0 for name in TASKS}


def status() -> str:
    reg = registered()
    if reg is None:
        return "crontab -l 로 확인"
    found = [n for n, ok in reg.items() if ok]
    return ("등록됨: " + ", ".join(found)) if found else "등록 안 됨 (ktrade schedule install)"
