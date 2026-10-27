"""키움 REST API 주문 (kiwoom-client). is_mock: true면 모의투자 서버(mockapi.kiwoom.com).

주문은 현재가 ± slippage_pct 지정가(매수는 위, 매도는 아래) — 지정가가 시장가보다 유리하면 거래소가 더 좋은 호가로
체결하므로 사실상 범위 제한 시장가다. 보유는 계좌평가(kt00018)에서 읽는다(모의투자에서 체결잔고 kt00005 미제공).
"""
from __future__ import annotations

from kiwoom_client import KiwoomAPI, round_to_tick, to_dataframe
from kiwoom_client.base import KiwoomAPIError

from ktrade.broker.base import Broker

_CASH = ["ord_alow_amt", "entr", "dnca_tot_amt"]
_DEPOSIT = ["entr", "dnca_tot_amt"]


def _field(d: dict, keys: list[str]) -> float:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return float(d[k])
    raise KeyError(f"응답에 {keys} 중 아무것도 없음: {list(d)[:20]}")


class KiwoomBroker(Broker):
    def __init__(self, cfg: dict):
        s = cfg["_secrets"]
        if not s["app_key"] or not s["app_secret"]:
            raise RuntimeError("KIWOOM_APP_KEY / KIWOOM_APP_SECRET가 없습니다 — 대시보드 로그인 화면 또는 ktrade login")
        self.api = KiwoomAPI(app_key=s["app_key"], app_secret=s["app_secret"], is_mock=cfg["kiwoom"].get("is_mock", True))

    def _deposit(self) -> dict:
        raw = self.api.account.deposit_detail(qry_tp="2")
        return raw if isinstance(raw, dict) else to_dataframe(raw).to_dict("records")[0]

    def cash(self) -> float:
        return _field(self._deposit(), _CASH)

    def total_deposit(self) -> float:
        return _field(self._deposit(), _DEPOSIT)

    def positions(self) -> dict[str, dict]:
        raw = self.api.account.account_evaluation(qry_tp="1", dmst_stex_tp="01")
        out = {}
        for it in raw.get("stk_acnt_evlt_prst", []) or []:
            sym = str(it.get("stk_cd", "")).lstrip("A")
            qty = int(float(it.get("rmnd_qty") or 0))
            if sym and qty > 0:
                avg = it.get("pur_pric")
                out[sym] = {"qty": qty, "avg_price": abs(float(avg)) if avg not in (None, "") else None}
        return out

    def place_order(self, symbol: str, side: str, qty: int, price: float, slippage_pct: float = 0.5) -> dict:
        factor = 1 + slippage_pct / 100 if side == "BUY" else 1 - slippage_pct / 100
        px = int(round_to_tick(price * factor))
        try:
            fn = self.api.order.buy_order if side == "BUY" else self.api.order.sell_order
            res = fn(dmst_stex_tp="KRX", stk_cd=symbol, ord_qty=str(qty), trde_tp="0", ord_uv=str(px))
            no = res.get("ord_no") if isinstance(res, dict) else None
            return {"status": "SUBMITTED", "order_price": px, "order_no": no, "response": res}
        except KiwoomAPIError as e:
            return {"status": "REJECTED", "reason": f"{e.code}: {e.message}"}

    def cancel(self, symbol: str, order_no: str | None) -> dict:
        """kt10003 취소 — cncl_qty 0 = 남은 수량 전부."""
        if not order_no:
            return {"status": "NOOP", "reason": "주문번호 없음"}
        try:
            res = self.api.order.cancel_order(dmst_stex_tp="KRX", orig_ord_no=str(order_no), stk_cd=symbol, cncl_qty="0")
            return {"status": "CANCELLED", "response": res}
        except KiwoomAPIError as e:
            return {"status": "FAILED", "reason": f"{e.code}: {e.message}"}
