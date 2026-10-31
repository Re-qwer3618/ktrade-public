"""매매조건 찾기·불러오기.

매매조건 하나 = 폴더 하나:
    my_condition/
      strategy.yaml   id, version, entry("strategy.py:클래스"), description, author, params, exits(선택 — 손절·익절 덮어쓰기)
      strategy.py     ktrade.sdk.Condition 상속 클래스 (종목 선정 + 매수 타이밍만)

찾는 곳 (앞이 우선): KTRADE_HOME/strategies → config strategies.dirs → 동봉 예제 → pip 패키지 entry point
("ktrade.strategies" 그룹). code_hash = 폴더 안 .py·.yaml 내용의 지문 — 백테스트 기록과 짝지어
'조건을 바꿨는데 아직 백테스트 안 함'을 알아낸다 (engine/experiments.py).

주의: strategy.py는 일반 파이썬 코드라 무엇이든 할 수 있다. 믿을 수 있는 조건만 넣는다(ktrade validate가 금지 import를 잡지만 샌드박스는 아님).
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ktrade.config import paths
from ktrade.sdk.condition import Condition
from ktrade.sdk.intraday import Intraday

KINDS = ("condition", "intraday")   # 장 마감 후 종목 선정 | 장중 매수 판단


@dataclass
class StrategyInfo:
    id: str
    version: str
    description: str
    author: str
    source: str            # 폴더 경로 또는 "entry-point:<모듈:클래스>"
    params: dict
    entry: str = ""
    exits: dict = field(default_factory=dict)
    code_hash: str = ""
    cls: type | None = None
    kind: str = "condition"


def bundled_dir() -> Path | None:
    here = Path(__file__).resolve()
    for cand in (here.parents[3] / "strategies", here.parents[1] / "_bundled_strategies"):  # 소스 체크아웃 / 설치본
        if cand.is_dir():
            return cand
    return None


def search_dirs(cfg: dict) -> list[Path]:
    dirs = [paths(cfg)["strategies"]] + [Path(d).expanduser() for d in cfg["strategies"].get("dirs", [])]
    b = bundled_dir()
    if b:
        dirs.append(b)
    return [d for d in dirs if d.is_dir()]


def folder_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(list(folder.rglob("*.py")) + list(folder.rglob("*.yaml"))):
        if "__pycache__" in f.parts:
            continue
        h.update(f.relative_to(folder).as_posix().encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _from_folder(folder: Path) -> StrategyInfo | None:
    mf = folder / "strategy.yaml"
    if not mf.exists():
        return None
    m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    for key in ("id", "entry"):
        if not m.get(key):
            raise ValueError(f"{mf}: '{key}'가 없습니다")
    if m.get("kind", "condition") not in KINDS:
        raise ValueError(f"kind는 condition(장 마감 후 종목 선정) 또는 intraday(장중 매수 판단) (받은 값 {m.get('kind')}) — "
                         "0.3 이하의 plan/signal 전략은 Condition.select()로 옮기세요 (docs/CONDITION_GUIDE.md)")
    return StrategyInfo(id=m["id"], version=str(m.get("version", "v1")), description=m.get("description", ""),
                        author=m.get("author", ""), source=str(folder), params=m.get("params") or {}, entry=m["entry"],
                        exits=m.get("exits") or {}, code_hash=folder_hash(folder), kind=m.get("kind", "condition"))


INVALID: dict[str, str] = {}   # 마지막 discover에서 무시한 폴더 → 사유 (ktrade strategies·doctor가 보여 줌)


def discover(cfg: dict) -> dict[str, StrategyInfo]:
    """잘못된 폴더 하나가 다른 조건까지 막지 않게, 문제 있는 폴더는 건너뛰고 INVALID에 사유를 남긴다."""
    found: dict[str, StrategyInfo] = {}
    INVALID.clear()
    for d in search_dirs(cfg):
        for folder in sorted(p for p in d.iterdir() if p.is_dir()):
            try:
                info = _from_folder(folder)
            except Exception as exc:
                INVALID[str(folder)] = str(exc)
                print(f"[ktrade] 매매조건 폴더 무시: {folder} — {exc}", file=sys.stderr)
                continue
            if info and info.id not in found:
                found[info.id] = info
    for ep in importlib.metadata.entry_points(group="ktrade.strategies"):
        try:
            cls = ep.load()
        except Exception as exc:  # 설치된 조건 하나가 깨져도 나머지는 쓴다
            print(f"[ktrade] 조건 패키지 {ep.name} 불러오기 실패: {exc}", file=sys.stderr)
            continue
        if cls.id and cls.id not in found:
            src = inspect.getsource(cls).encode()
            found[cls.id] = StrategyInfo(id=cls.id, version=cls.version, description=cls.description, author="",
                                         source=f"entry-point:{ep.value}", params=dict(cls.default_params),
                                         exits=dict(getattr(cls, "exits", {}) or {}),
                                         code_hash=hashlib.sha256(src).hexdigest()[:12], cls=cls,
                                         kind=getattr(cls, "kind", "condition"))
    return found


def _load_class(info: StrategyInfo) -> type:
    if info.cls is not None:
        return info.cls
    file, _, clsname = info.entry.partition(":")
    path = Path(info.source) / file
    mod_name = f"ktrade_condition_{info.id}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path}를 불러올 수 없습니다")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    cls = getattr(mod, clsname)
    base = Intraday if info.kind == "intraday" else Condition
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise TypeError(f"{info.id}: {clsname}는 ktrade.sdk.{base.__name__}을 상속해야 합니다")
    return cls


def of_kind(found: dict[str, StrategyInfo], kind: str) -> dict[str, StrategyInfo]:
    return {k: v for k, v in found.items() if v.kind == kind}


def load(info: StrategyInfo, params: dict | None = None) -> Condition | Intraday:
    cls = _load_class(info)
    cls.id, cls.version = info.id, info.version
    return cls({**info.params, **(params or {})})


def exits_for(cfg: dict, info: StrategyInfo, override: dict | None = None) -> dict:
    from ktrade.engine import exits
    return exits.resolve(cfg.get("exits"), info.exits, override)


def enabled(cfg: dict) -> list[tuple[StrategyInfo, Condition, dict]]:
    """config strategies.enabled 순서대로 (앞이 우선) — (정보, 조건 인스턴스, 청산 설정)."""
    found = discover(cfg)
    out = []
    for item in cfg["strategies"].get("enabled", []):
        item = item if isinstance(item, dict) else {"id": str(item)}
        sid = item["id"]
        if sid not in found:
            raise KeyError(f"설정에 켠 매매조건 '{sid}'를 찾지 못했습니다 (ktrade strategies로 목록 확인)")
        info = found[sid]
        if info.kind != "condition":
            raise KeyError(f"'{sid}'는 장중 조건입니다 — 계획에 쓸 매매조건(장 마감 후 종목 선정)에 장중 조건으로 붙이세요")
        out.append((info, load(info, item.get("params")), exits_for(cfg, info, item.get("exits"))))
    return out
