"""매매조건이 돌려주는 것(Pick)과, 엔진이 그것으로 만드는 매매 계획(Plan)·계획 파일 형식 (plan schema 1).

사용자(매매조건 작성자)가 정하는 것: **어떤 종목을(종목 선정)**, **언제·어떤 가격 조건에서 살지(매수 타이밍 = Entry)**.
엔진이 정하는 것: 손절·이익실현·보유기간(ExitRules, config.yaml / strategy.yaml의 exits) → Plan.stop / Plan.target.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PLAN_SCHEMA = 1


@dataclass
class Entry:
    """매수 타이밍. 실행일(기준일 다음 거래일)에 시가 >= min_open 이고, after(HH:MM) 이후 현재가가
    min_price 초과 max_price 이하이면 매수한다. 아래 도우미로 만드는 것을 권장한다."""
    min_open: float
    min_price: float
    max_price: float
    after: str = "15:10"

    @staticmethod
    def confirm(ref: float, chase_pct: float = 5.0, after: str = "15:10") -> "Entry":
        """다음 날 확인 매수: 시가가 ref(보통 기준일 종가) 이상으로 시작하고 after 이후에도 ref 위면 매수,
        ref × (1 + chase_pct%)보다 비싸면 추격하지 않는다."""
        return Entry(min_open=ref, min_price=ref, max_price=round(ref * (1 + chase_pct / 100), 2), after=after)

    @staticmethod
    def at_open(ref: float, gap_pct: float = 3.0) -> "Entry":
        """다음 날 장 시작에 매수: 시가가 ref ±gap_pct% 안에서 시작하면 09:00부터 그 범위에서 산다."""
        lo, hi = ref * (1 - gap_pct / 100), ref * (1 + gap_pct / 100)
        return Entry(min_open=round(lo, 2), min_price=round(lo, 2), max_price=round(hi, 2), after="09:00")

    @staticmethod
    def pullback(low: float, high: float, after: str = "09:00") -> "Entry":
        """눌림 매수: 실행일 중 현재가가 (low, high] 구간에 들어오면 산다 (시가 조건 없음)."""
        return Entry(min_open=0.01, min_price=low, max_price=high, after=after)


@dataclass
class Pick:
    """매매조건이 고른 종목 하나 — 손절·익절은 정하지 않는다(엔진 몫)."""
    symbol: str
    entry: Entry
    reason: str = ""            # 왜 골랐는지 (대시보드·리포트에 그대로 보임)
    rank: int | None = None     # 작을수록 우선 (하루 계획 수 한도 안에서 위부터)
    name: str = ""
    market: str = ""            # "코스피" | "코스닥"
    meta: dict[str, Any] = field(default_factory=dict)  # 근거 숫자 — 백테스트 결과를 이 값별로 나눠 볼 수 있다


@dataclass
class Plan:
    """엔진이 Pick + 손절·익절 규칙으로 만든 실행 단위 (계획 파일에 저장되는 모양)."""
    symbol: str
    entry: Entry
    stop: float | None          # 손절가 (없으면 손절 안 함 — exits.stop.type: none)
    target: float | None        # 목표가 (없으면 익절 없음)
    max_hold_days: int = 20
    name: str = ""
    market: str = ""
    rank: int | None = None
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    exits: dict[str, Any] = field(default_factory=dict)  # 실행에 필요한 청산 설정 (trailing, stop_check …)
    intraday: dict[str, Any] = field(default_factory=dict)  # 장중 조건 {id, params} — 있으면 매수 시점은 장중 조건이 정함(Entry는 무시)

    def to_dict(self, strategy_id: str, version: str, asof: str) -> dict:
        d = asdict(self)
        d["plan_id"] = plan_id(strategy_id, version, asof, self.symbol)
        d["basis"] = d.pop("reason")
        return d

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        e = d["entry"]
        return Plan(symbol=str(d["symbol"]),
                    entry=Entry(e["min_open"], e["min_price"], e["max_price"], e.get("after", "15:10")),
                    stop=d.get("stop"), target=d.get("target"), max_hold_days=int(d.get("max_hold_days", 20)),
                    name=d.get("name", ""), market=d.get("market", ""), rank=d.get("rank"),
                    reason=d.get("basis") or d.get("reason", ""),
                    meta={k: v for k, v in d.items() if k in ("conditions", "metrics", "zone")} | (d.get("meta") or {}),
                    exits=d.get("exits") or {}, intraday=d.get("intraday") or {})


def plan_id(strategy_id: str, version: str, asof: str, symbol: str) -> str:
    return f"{strategy_id}-{version}-{asof.replace('-', '')}-{symbol}"


def plan_file(strategy_id: str, version: str, asof: str, plans: list[Plan], status: str = "experimental",
              generated_at: str = "", skipped: dict | None = None, code_hash: str = "") -> dict:
    return {"schema": PLAN_SCHEMA, "generated_at": generated_at, "asof": asof, "strategy": strategy_id,
            "version": version, "status": status, "code_hash": code_hash,
            "plans": [p.to_dict(strategy_id, version, asof) for p in plans], "skipped": skipped or {}}
