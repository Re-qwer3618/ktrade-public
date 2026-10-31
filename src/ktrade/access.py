"""이용 권한 — 0.9.3부터 백테스트·매매조건·계획 등 모든 기본 기능은 로그인 없이 모두에게 열려 있다.
이용 코드는 관리자(이용 코드 발급·AI 기능)만 쓴다. AI판은 출시 때 구독으로 따로 연다(ai_subscribed).

  역할   admin = 이용 코드 발급(관리자 탭·ktrade admin) + AI 기능   backtest = 예전 코드 호환(지금은 아무 기능도 더 열지 않음)
  코드   KT1.<내용>.<서명> — 내용(아이디·역할·만료일)을 관리자 서명 키(Ed25519)로 서명한 것.
         ktrade는 아래 PUBLIC_KEYS(공개 키)로 서명만 확인한다 → 서버 없이 어느 PC에서나 쓰고, 위조할 수 없다.
  저장   로그인한 코드: <KTRADE_HOME>/access.json · 환경변수 KTRADE_ACCESS_CODE가 있으면 그게 우선 (자동 실행·개발용)
         관리자 서명 키: <KTRADE_HOME>/admin/signing_key — 이 파일이 있는 PC에서만 발급할 수 있다. git·배포에 넣지 말 것.

키움 앱키 로그인(credentials.py)과는 별개다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

PUBLIC_KEYS: tuple[str, ...] = (         # 관리자 서명 키의 공개 키(hex). ktrade admin init 이 알려 주는 값을 여기에
    "0466d352b486b3ee7a3e970e071a04f3973fe9b140974b4d29a11d4153954483",   # 2026-10-30 집 PC (~/.ktrade/admin/signing_key)
)
ROLES = ("backtest", "admin")
PREFIX = "KT1"


# ───────────── Ed25519 (RFC 8032) — 서명 확인용 추가 패키지 없이 ─────────────
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A, B = (y1 - x1) * (y2 - x2) % _P, (y1 + x1) * (y2 + x2) % _P
    C, D = t1 * 2 * _D * t2 % _P, z1 * 2 * z2 % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return E * F % _P, G * H % _P, F * G % _P, E * H % _P


def _mul(s: int, p):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p, s = _add(p, p), s >> 1
    return q


def _same(p, q) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _enc(p) -> bytes:
    zi = pow(p[2], _P - 2, _P)
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _dec(s: bytes):
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _h(m: bytes) -> int:
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    return (a & ((1 << 254) - 8)) | (1 << 254), h[32:]


def public_key(seed: bytes) -> bytes:
    return _enc(_mul(_expand(seed)[0], _G))


def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _enc(_mul(a, _G))
    r = _h(prefix + msg) % _L
    R = _enc(_mul(r, _G))
    return R + ((r + _h(R + pub + msg) * a) % _L).to_bytes(32, "little")


def verify_sig(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    A, R = _dec(pub), _dec(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if A is None or R is None or s >= _L:
        return False
    return _same(_mul(s, _G), _add(R, _mul(_h(sig[:32] + pub + msg) % _L, A)))


# ───────────── 이용 코드 ─────────────
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(seed: bytes, user_id: str, role: str = "backtest", days: int | None = 365, today: date | None = None) -> str:
    """이용 코드 만들기. days=None이면 만료 없음."""
    user_id = user_id.strip()
    if not user_id:
        raise ValueError("아이디가 비었습니다")
    if role not in ROLES:
        raise ValueError(f"역할은 {', '.join(ROLES)} 중 하나")
    today = today or date.today()
    body = {"id": user_id, "role": role, "iat": today.isoformat(),
            "exp": (today + timedelta(days=days)).isoformat() if days else None}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    return f"{PREFIX}.{_b64(raw)}.{_b64(sign(seed, raw))}"


def check(code: str, user_id: str | None = None, keys: tuple[str, ...] | None = None, today: date | None = None) -> dict:
    """코드 확인 → 내용(id·role·exp). 틀리면 ValueError(이유). user_id를 주면 코드의 아이디와 같아야 한다."""
    try:
        prefix, body, sig = code.strip().split(".")
        raw, sig_b = _unb64(body), _unb64(sig)
        info = json.loads(raw)
    except (ValueError, AttributeError):
        raise ValueError("이용 코드 형식이 아닙니다") from None
    if prefix != PREFIX:
        raise ValueError("이용 코드 형식이 아닙니다")
    keys = PUBLIC_KEYS if keys is None else keys
    if not any(verify_sig(bytes.fromhex(k), raw, sig_b) for k in keys):
        raise ValueError("관리자가 발급한 코드가 아닙니다 (서명 불일치)")
    if user_id is not None and info.get("id") != user_id.strip():
        raise ValueError("아이디와 이용 코드가 맞지 않습니다")
    if info.get("role") not in ROLES:
        raise ValueError("알 수 없는 역할")
    if info.get("exp") and info["exp"] < (today or date.today()).isoformat():
        raise ValueError(f"만료된 코드입니다 ({info['exp']}) — 관리자에게 새 코드를 받으세요")
    return info


def _home() -> Path:
    from ktrade.config import home
    return home()


def _file() -> Path:
    return _home() / "access.json"


def login(user_id: str, code: str) -> dict:
    info = check(code, user_id)
    _file().parent.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps({"id": info["id"], "code": code.strip()}, ensure_ascii=False), encoding="utf-8")
    return info


def logout() -> None:
    _file().unlink(missing_ok=True)


def current() -> dict | None:
    """지금 쓸 수 있는 권한 (환경변수 KTRADE_ACCESS_CODE → access.json). 없거나 만료·위조면 None."""
    env = os.getenv("KTRADE_ACCESS_CODE", "").strip()
    if env:
        code, user_id = env, None
    else:
        try:
            saved = json.loads(_file().read_text(encoding="utf-8"))
            code, user_id = saved["code"], saved["id"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
    try:
        return check(code, user_id)
    except ValueError:
        return None


def can_backtest(info: dict | None) -> bool:
    """백테스트(매매조건 실험·계획에 켜기)는 모두에게 — 0.9.3부터 로그인 없이."""
    return True


def is_admin(info: dict | None) -> bool:
    return bool(info) and info.get("role") == "admin"


def ai_subscribed() -> bool:
    """AI판 구독 — 이용 코드와 따로 파는 월 구독(결제 후 사용자 본인 API 키로 Claude·Gemini·ChatGPT 연결).
    결제 연동은 출시 때 붙인다. 그 전까지는 구독자가 없고 관리자만 쓴다."""
    return False


def can_ai(info: dict | None) -> bool:
    """AI 기능(백테스트 분석·매매조건 작성 도우미) — 관리자는 항상, 그 밖에는 AI판 구독자만."""
    return is_admin(info) or ai_subscribed()


def describe(info: dict | None) -> str:
    if not info:
        return "없음"
    role = {"admin": "관리자", "backtest": "백테스트"}.get(info["role"], info["role"])
    return f"{info['id']} · {role} · " + (f"{info['exp']}까지" if info.get("exp") else "만료 없음")


# ───────────── 관리자 (서명 키가 있는 PC) ─────────────
def _admin_dir() -> Path:
    return _home() / "admin"


def signing_seed() -> bytes | None:
    try:
        return bytes.fromhex((_admin_dir() / "signing_key").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def admin_init() -> tuple[str, bool]:
    """서명 키가 없으면 만든다 → (공개 키 hex, 새로 만들었는지)."""
    seed = signing_seed()
    if seed:
        return public_key(seed).hex(), False
    seed = secrets.token_bytes(32)
    _admin_dir().mkdir(parents=True, exist_ok=True)
    (_admin_dir() / "signing_key").write_text(seed.hex(), encoding="utf-8")
    return public_key(seed).hex(), True


def key_registered(seed: bytes) -> bool:
    return public_key(seed).hex() in PUBLIC_KEYS


def admin_issue(user_id: str, role: str = "backtest", days: int | None = 365, note: str = "") -> str:
    seed = signing_seed()
    if not seed:
        raise ValueError("이 PC에 관리자 서명 키가 없습니다 (ktrade admin init)")
    code = issue(seed, user_id, role, days)
    info = check(code, keys=(public_key(seed).hex(),))
    with (_admin_dir() / "issued.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**info, "at": datetime.now().isoformat(timespec="seconds"), "note": note}, ensure_ascii=False) + "\n")
    return code


def issued() -> list[dict]:
    try:
        lines = (_admin_dir() / "issued.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(ln) for ln in lines if ln.strip()]
