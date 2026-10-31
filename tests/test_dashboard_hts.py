"""대시보드 '영웅문 조건식' 탭이 신호·분봉·매매 기록으로 오류 없이 그려지는지 (streamlit AppTest)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from ktrade import config as C  # noqa: E402
from ktrade.data.store import Store  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "ktrade" / "dashboard" / "app.py"


def test_hts_tab_renders(home, monkeypatch):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text('broker: paper\nhts_conditions:\n  - {name: "단타전략-2", mode: trade}\n', encoding="utf-8")
    store = Store(C.paths(C.load())["db"])
    store.add_signal("단타전략-2", "15", "005930", "I", "BUY", "매수 100주", 71000.0, "091500", "trade", "p1", ts="2026-10-26T09:15:20")
    store.add_signal("단타전략-2", "15", "000660", "I", "SKIP", "가격 범위 밖", 300000.0, "091600", "trade", ts="2026-10-26T09:16:10")
    import pandas as pd
    store.upsert_minute("005930", pd.DataFrame([{"ts": f"2026-10-26 09:{m:02d}:00", "open": 71000, "high": 71500 + m * 100,
                                                 "low": 70900, "close": 71200, "volume": 10} for m in range(16, 40)]))
    tid = store.add_plan_trade({"plan_id": "p1", "symbol": "005930"}, "hts:단타전략-2", "hts", 10, 71000, "2026-10-26T09:15:20")
    store.update_plan_trade(tid, status="closed", entry_price=71000, exit_price=73130, exit_date="2026-10-26", exit_reason="목표가")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    tabs = [t.label for t in at.tabs]
    assert "🔎 조건검색 · 차트" in tabs
    text = " ".join(str(m.value) for m in at.markdown) + " ".join(str(s.value) for s in at.subheader)
    assert "단타전략-2" in text
    metrics = {m.label: m.value for m in at.metric}
    assert metrics.get("편입 (종목·묶음)") == "2 · 2" and metrics.get("매수") == "1"
