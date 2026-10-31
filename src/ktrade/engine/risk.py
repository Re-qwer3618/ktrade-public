"""사용자 리스크 한도 (config risk) — 전략과 무관하게 엔진이 지킨다.

종목 단위: 종목당 매수 금액(order_amount 원, 없으면 시작 총자본 × max_position_pct%), 동시 보유 종목 수, 하루 신규 매수 수,
          매수 시간대(buy_window), 미체결 자동 취소(order_timeout_sec)
계좌 단위(Executor.guard): 하루 수익 목표·손실 한도(원) → 신규 매수 중지 + 보유 전량 매도, 시간 일괄청산(flatten_at)
계좌 단위 한도는 실제 계좌 흐름에서만 의미가 있어 백테스트(종목별 거래)에는 반영되지 않는다.

우선순위(config priority) — 장 마감 후 계획(plan, ktrade run)과 영웅문 조건식(hts, hts watch)이 같은 계좌를 쓸 때:
  plan  = 계획 우선(기본): 오늘 계획 종목은 조건식이 사지 않고, 계획 몫(plan_reserve자리)은 동시 보유·하루 매수 한도에서 남겨 둔다
  hts   = 선착순: 먼저 신호가 온 쪽이 한도를 쓴다 (같은 종목은 이미 가진 쪽만)
  split = 나눠 쓰기: 출처마다 동시 보유·하루 매수 한도를 따로 (split.plan / split.hts, 계좌 전체 한도도 같이 지킴)
"""
from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_KO = {"plan": "계획", "hts": "조건식"}
PRIORITY_MODES = {"plan": "계획 우선", "hts": "선착순 (조건식이 먼저 오면 먼저)", "split": "나눠 쓰기"}


@dataclass
class Risk:
    starting_cash: float
    max_position_pct: float = 2.0
    max_concurrent_positions: int = 5
    intraday_stop_pct: float | None = None
    slippage_pct: float = 0.5
    max_new_orders_per_day: int = 10
    order_amount: float | None = None
    buy_window: tuple[str, str] | None = None
    daily_profit_target: float | None = None
    daily_loss_limit: float | None = None
    flatten_at: str | None = None
    order_timeout_sec: float | None = None
    priority: dict = field(default_factory=dict)

    @staticmethod
    def from_config(cfg: dict, starting_cash: float) -> "Risk":
        r = cfg["risk"]
        win = r.get("buy_window")
        return Risk(starting_cash=starting_cash, max_position_pct=float(r["max_position_pct"]),
                    max_concurrent_positions=int(r["max_concurrent_positions"]),
                    intraday_stop_pct=r.get("intraday_stop_pct"), slippage_pct=float(r["order_slippage_pct"]),
                    max_new_orders_per_day=int(r["max_new_orders_per_day"]),
                    order_amount=r.get("order_amount"), buy_window=tuple(win) if win else None,
                    daily_profit_target=r.get("daily_profit_target"), daily_loss_limit=r.get("daily_loss_limit"),
                    flatten_at=r.get("flatten_at"), order_timeout_sec=r.get("order_timeout_sec"),
                    priority=dict(cfg.get("priority") or {}))

    def buy_qty(self, cash: float, price: float) -> int:
        """종목당 목표 금액 = order_amount(원) 또는 시작 총자본 × max_position_pct% (살수록 줄어드는 현금 기준이 아님),
        지금 현금 한도 안에서. 분할매수면 이 수량을 차수로 나눈다."""
        if price <= 0:
            return 0
        budget = float(self.order_amount) if self.order_amount else self.starting_cash * self.max_position_pct / 100
        return int(min(budget, cash) // price)

    def in_buy_window(self, hhmm: str) -> bool:
        return not self.buy_window or self.buy_window[0] <= hhmm <= self.buy_window[1]

    def can_buy(self, store, broker, today: str, hhmm: str | None = None, source: str = "plan", symbol: str = "",
                plan_info=None) -> str:
        """막아야 하면 사유, 괜찮으면 빈 문자열. source = 'plan' | 'hts' (우선순위 규칙에 씀)."""
        if hhmm and not self.in_buy_window(hhmm):
            return f"매수 시간대 {self.buy_window[0]}~{self.buy_window[1]} 밖"
        held = len(broker.positions())
        if held >= self.max_concurrent_positions:
            return f"동시 보유 한도 {held}/{self.max_concurrent_positions}"
        n = store.query("SELECT COUNT(*) AS n FROM decisions WHERE action = 'BUY' AND substr(timestamp, 1, 10) = ?",
                        (today,))["n"].iloc[0]
        if n >= self.max_new_orders_per_day:
            return f"하루 신규 매수 한도 {n}/{self.max_new_orders_per_day}"
        return self.priority_block(store, today, source, symbol, held, int(n), plan_info)

    def priority_block(self, store, today: str, source: str, symbol: str, held: int, bought: int, plan_info=None) -> str:
        p = self.priority or {}
        mode = p.get("mode") or "plan"
        if mode == "hts":
            return ""
        hts_like = "strategy LIKE 'hts:%'"
        mine = hts_like if source == "hts" else "NOT " + hts_like

        def counts(where: str) -> tuple[int, int]:
            h = store.query(f"SELECT COUNT(DISTINCT symbol) AS n FROM plan_trades WHERE status IN ('submitted','open','closing') "
                            f"AND {where}")["n"].iloc[0]
            b = store.query(f"SELECT COUNT(*) AS n FROM decisions WHERE action = 'BUY' AND substr(timestamp, 1, 10) = ? "
                            f"AND {where}", (today,))["n"].iloc[0]
            return int(h), int(b)

        if mode == "split":
            lim = (p.get("split") or {}).get(source) or {}
            h, b = counts(mine)
            if lim.get("positions") is not None and h >= int(lim["positions"]):
                return f"{SOURCE_KO[source]} 몫 동시 보유 {h}/{lim['positions']} (나눠 쓰기)"
            if lim.get("orders") is not None and b >= int(lim["orders"]):
                return f"{SOURCE_KO[source]} 몫 하루 매수 {b}/{lim['orders']} (나눠 쓰기)"
            return ""
        if source != "hts" or plan_info is None:          # 계획 우선: 조건식만 양보한다
            return ""
        syms, n_plans = plan_info(today)
        if symbol and symbol in syms:
            return "계획 우선 — 오늘 장 마감 후 계획에 있는 종목"
        reserve = min(int(p.get("plan_reserve") or 0), int(n_plans))
        if reserve <= 0:
            return ""
        plan_held, plan_bought = counts("NOT " + hts_like)
        if held + max(0, reserve - plan_held) >= self.max_concurrent_positions:
            return f"계획 우선 — 동시 보유 {held}/{self.max_concurrent_positions} 중 계획 몫 {reserve}자리 남겨 둠"
        if bought + max(0, reserve - plan_bought) >= self.max_new_orders_per_day:
            return f"계획 우선 — 하루 신규 매수 {bought}/{self.max_new_orders_per_day} 중 계획 몫 {reserve}건 남겨 둠"
        return ""
