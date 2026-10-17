"""키 보관 — 대시보드 로그인 화면이나 `ktrade login`에서 입력한 키움 앱키를 저장·조회한다.

저장 위치: OS 자격 증명 저장소 (Windows 자격 증명 관리자 · macOS 키체인 · Linux Secret Service).
그런 저장소가 없는 환경(서버 등)에서만 <KTRADE_HOME>/credentials.json 에 둔다.
사용자가 파일을 직접 만들 필요는 없다. 예전 방식(~/.ktrade/.env, 환경변수)도 계속 읽으며, 그쪽이 우선한다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

NAMES = ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET")


def _home() -> Path:
    from ktrade.config import home
    return home()


def _service() -> str:
    """기본 폴더(~/.ktrade)면 'ktrade', KTRADE_HOME을 따로 쓰면 폴더마다 따로 보관한다."""
    h = _home().resolve()
    return "ktrade" if h == (Path.home() / ".ktrade").resolve() else f"ktrade:{h}"


def _keyring():
    if os.getenv("KTRADE_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # keyring 미설치·백엔드 오류 → 파일로
        return None


def _file() -> Path:
    return _home() / "credentials.json"


def _read_file() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def where() -> str:
    return "Windows 자격 증명 관리자" if _keyring() and os.name == "nt" else \
        "OS 자격 증명 저장소" if _keyring() else str(_file())


def get(name: str) -> str:
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(_service(), name)
            if v:
                return v
        except Exception:
            pass
    return str(_read_file().get(name) or "")


def load_all() -> dict[str, str]:
    return {n: get(n) for n in NAMES}


def save(values: dict[str, str]) -> str:
    """values = {KIWOOM_APP_KEY: ..., KIWOOM_APP_SECRET: ...}. 저장한 곳을 돌려준다."""
    values = {k: str(v).strip() for k, v in values.items() if k in NAMES}
    kr = _keyring()
    if kr:
        try:
            for k, v in values.items():
                kr.set_password(_service(), k, v)
            return where()
        except Exception:
            pass
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({**_read_file(), **values}, indent=1), encoding="utf-8")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return str(f)


def clear() -> None:
    kr = _keyring()
    if kr:
        for n in NAMES:
            try:
                kr.delete_password(_service(), n)
            except Exception:
                pass
    f = _file()
    if f.exists():
        rest = {k: v for k, v in _read_file().items() if k not in NAMES}
        if rest:
            f.write_text(json.dumps(rest, indent=1), encoding="utf-8")
        else:
            f.unlink()


def mask(v: str) -> str:
    return (v[:4] + "…" + v[-2:]) if len(v) > 8 else ("•" * len(v) if v else "")
