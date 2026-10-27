"""영웅문 조건검색식 (HTS 조건식) — 목록 조회·1회 검색. 키움 REST 웹소켓의 CNSRLST / CNSRREQ(search_type=0).

  ktrade hts list                  # 영웅문에 저장한 조건식 (번호·이름)
  ktrade hts scan "01_오전스켈핑"   # 지금 그 조건식에 걸린 종목 (주문 없음)

조건식은 영웅문4 '조건검색(0150)'에서 만들고 저장한다 — 키움 서버가 계산하므로 ktrade에서 백테스트는 할 수 없다.
웹소켓 실시간 세션은 계정당 하나뿐이라, 같은 키로 실시간 시세·조건검색을 쓰는 다른 프로그램이 떠 있으면 서로 끊긴다.
이 모듈은 요청할 때만 잠깐 연결하고 바로 닫는다. (장중 실시간 편입·이탈은 1b 단계에서 따로 만든다.)
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

PREFIX_KIND = {"A": "주식", "J": "ELW", "Q": "ETN"}
SIGN = {"1": "상한", "2": "상승", "3": "보합", "4": "하한", "5": "하락"}


class HtsError(RuntimeError):
    pass


@dataclass(frozen=True)
class HtsCondition:
    seq: str
    name: str


# ---------------- 응답 해석 ----------------
def parse_list(msg: dict) -> list[HtsCondition]:
    """CNSRLST 응답 → 조건식 목록. data는 [[seq, 이름], …] (문서 예시) 또는 [{seq, name}, …]."""
    out = []
    for it in msg.get("data") or []:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            seq, name = it[0], it[1]
        elif isinstance(it, dict):
            seq, name = it.get("seq"), it.get("name")
        else:
            continue
        if seq is not None:
            out.append(HtsCondition(str(seq).strip(), str(name or "").strip()))
    return out


def find(conds: list[HtsCondition], key: str) -> HtsCondition:
    """번호 또는 이름으로 찾는다 — 번호 일치 → 이름 일치 → 이름 일부(하나만 맞을 때)."""
    k = str(key).strip()
    for c in conds:
        if c.seq == k:
            return c
    for c in conds:
        if c.name == k:
            return c
    part = [c for c in conds if k.lower() in c.name.lower()]
    if len(part) == 1:
        return part[0]
    if not part:
        raise HtsError(f"조건식 '{k}'이(가) 없습니다 — ktrade hts list 로 이름을 확인하세요 (영웅문에서 저장했는지도)")
    raise HtsError(f"'{k}'이(가) 들어간 조건식이 여럿입니다: " + ", ".join(f"{c.seq} {c.name}" for c in part[:10])
                   + " — 번호나 전체 이름으로 지정하세요")


def _num(v: Any) -> float | None:
    """'000021850', '-00000100' 같은 부호 포함 0채움 숫자 → 수."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def parse_rows(data: list[dict]) -> pd.DataFrame:
    """CNSRREQ(일반) data → 표. 종목코드 접두어(A 주식 / J ELW / Q ETN)는 떼어 kind로."""
    rows = []
    for d in data or []:
        raw = str(d.get("9001") or d.get("jmcode") or "").strip()
        if not raw:
            continue
        kind = PREFIX_KIND.get(raw[0], "") if raw[0].isalpha() and len(raw) == 7 else "주식"
        code = raw[1:] if kind and len(raw) == 7 else raw
        rate = _num(d.get("12"))
        rows.append({"code": code, "name": str(d.get("302") or "").strip(), "kind": kind,
                     "price": abs(_num(d.get("10")) or 0) or None, "change": _num(d.get("11")),
                     "rate": rate / 100 if rate is not None else None, "sign": SIGN.get(str(d.get("25") or "").strip(), ""),
                     "volume": _num(d.get("13")), "open": abs(_num(d.get("16")) or 0) or None,
                     "high": abs(_num(d.get("17")) or 0) or None, "low": abs(_num(d.get("18")) or 0) or None})
    cols = ["code", "name", "kind", "price", "change", "rate", "sign", "volume", "open", "high", "low"]
    return pd.DataFrame(rows, columns=cols)


def _check(msg: dict, what: str) -> None:
    code = msg.get("return_code")
    if code not in (None, 0, "0"):
        raise HtsError(f"{what} 실패 (return_code {code}): {msg.get('return_msg') or ''}".strip())


# ---------------- 웹소켓 요청 ----------------
def _default_factory(cfg: dict):
    from kiwoom_client import KiwoomAPI
    s = cfg["_secrets"]
    if not s.get("app_key") or not s.get("app_secret"):
        raise HtsError("키움 앱키가 없습니다 — 대시보드 로그인 화면 또는 ktrade login")
    api = KiwoomAPI(app_key=s["app_key"], app_secret=s["app_secret"], is_mock=cfg["kiwoom"].get("is_mock", True))
    api.login()
    return api.create_websocket()


class HtsClient:
    """요청마다 웹소켓을 열고(로그인) 응답을 받은 뒤 닫는다. ws_factory는 시험용으로 바꿔 끼울 수 있다."""

    def __init__(self, cfg: dict, ws_factory: Callable[[dict], Any] | None = None, timeout: float = 15.0):
        self.cfg, self.timeout = cfg, timeout
        self.ws_factory = ws_factory or _default_factory

    def list(self) -> list[HtsCondition]:
        return asyncio.run(self._session(lambda conv: conv.conditions()))

    def scan(self, key: str, max_pages: int = 20) -> tuple[HtsCondition, pd.DataFrame]:
        async def job(conv: "_Conversation"):
            cond = find(await conv.conditions(), key)
            return cond, await conv.search(cond, max_pages)
        return asyncio.run(self._session(job))

    async def _session(self, job):
        ws = self.ws_factory(self.cfg)
        conv = _Conversation(ws, self.timeout)
        try:
            await ws.connect()
        except Exception as exc:
            raise HtsError(f"키움 웹소켓 연결 실패: {exc}") from exc
        listener = asyncio.create_task(ws.listen())
        try:
            return await job(conv)
        finally:
            with contextlib.suppress(Exception):
                await ws.disconnect()
            listener.cancel()
            with contextlib.suppress(BaseException):
                await listener


class _Conversation:
    """한 연결 안에서 요청 하나 → 같은 trnm 응답 하나를 기다린다 (조건검색 응답은 trnm으로만 구분된다)."""

    def __init__(self, ws, timeout: float):
        self.ws, self.timeout = ws, timeout
        self._waiting: dict[str, asyncio.Future] = {}
        for trnm in ("CNSRLST", "CNSRREQ"):
            ws.on_trnm(trnm, self._make_handler(trnm))

    def _make_handler(self, trnm: str):
        async def handler(msg: dict) -> None:
            fut = self._waiting.pop(trnm, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        return handler

    async def _ask(self, trnm: str, payload: dict, what: str) -> dict:
        fut = asyncio.get_running_loop().create_future()
        self._waiting[trnm] = fut
        await self.ws.send(payload)
        try:
            msg = await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError:
            raise HtsError(f"{what}: {self.timeout:.0f}초 안에 응답이 없습니다 — 같은 키로 실시간 시세·조건검색을 쓰는 "
                           "다른 프로그램이 떠 있으면 연결이 끊깁니다(계정당 웹소켓 하나)") from None
        _check(msg, what)
        return msg

    async def conditions(self) -> list[HtsCondition]:
        msg = await self._ask("CNSRLST", {"trnm": "CNSRLST"}, "조건식 목록 조회(CNSRLST)")
        return parse_list(msg)

    async def search(self, cond: HtsCondition, max_pages: int = 20) -> pd.DataFrame:
        frames, cont, nxt = [], "N", ""
        for _ in range(max_pages):
            msg = await self._ask("CNSRREQ", {"trnm": "CNSRREQ", "seq": cond.seq, "search_type": "0", "stex_tp": "K",
                                              "cont_yn": cont, "next_key": nxt}, f"조건검색 '{cond.name}'(CNSRREQ)")
            frames.append(parse_rows(msg.get("data") or []))
            if str(msg.get("cont_yn") or "N").strip() != "Y" or not str(msg.get("next_key") or "").strip():
                break
            cont, nxt = "Y", str(msg["next_key"]).strip()
        df = pd.concat(frames, ignore_index=True) if frames else parse_rows([])
        return df.drop_duplicates("code").reset_index(drop=True)
