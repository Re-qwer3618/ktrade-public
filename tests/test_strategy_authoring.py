"""매매조건 작성 경로 (직접·AI 공통): 넣는 곳, 검사(validate), 문서 예제가 실제로 도는지, test 명령의 실험 비교."""
from __future__ import annotations

import re
from pathlib import Path

from ktrade import cli
from ktrade.config import paths
from ktrade.engine import experiments
from ktrade.engine import validate as V

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _write(home: Path, sid: str, code: str, cls: str = "S", extra: str = "") -> Path:
    d = home / "strategies" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "strategy.yaml").write_text(f"id: {sid}\nversion: v1\nkind: condition\nentry: strategy.py:{cls}\n{extra}", encoding="utf-8")
    (d / "strategy.py").write_text(code, encoding="utf-8")
    return d


def test_init_creates_guide_and_condition_readme(home, capsys):
    cli.main(["init"])
    assert (home / "strategies" / "README.md").exists() and (home / "experiments").is_dir()
    assert (home / "docs" / "LLM_CONTEXT.md").exists() and (home / "docs" / "CONDITION_GUIDE.md").exists()
    out = capsys.readouterr().out
    assert "ktrade new" in out and "ktrade test" in out
    assert "exits:" in (home / "config.yaml").read_text(encoding="utf-8")


def test_validate_example_passes(market, cfg):
    store, days = market
    rep = V.validate(cfg, "example_breakout", asof=days[59], params={"min_avg_value": 0})
    assert rep.ok, rep.render()
    assert any("후보 1개, 엔진 검사 통과 1개" in n for n in rep.notes), rep.render()
    assert any("청산(엔진)" in n for n in rep.notes)


def test_validate_catches_forbidden_and_bad_output(market, cfg, home):
    store, days = market
    _write(home, "bad_import", "from ktrade.broker.kiwoom import KiwoomBroker\nfrom ktrade.sdk import Condition\n"
                               "class S(Condition):\n    def select(self, ctx):\n        return []\n")
    rep = V.validate(cfg, "bad_import", asof=days[59])
    assert not rep.ok and any("ktrade.broker" in e for e in rep.errors)

    _write(home, "returns_plan_dict", "from ktrade.sdk import Condition\nclass S(Condition):\n"
                                      "    def select(self, ctx):\n        return [{'symbol': '005930', 'stop': 1}]\n")
    rep = V.validate(cfg, "returns_plan_dict", asof=days[59])
    assert not rep.ok and any("Pick 목록" in e for e in rep.errors)

    _write(home, "crashes", "from ktrade.sdk import Condition\nclass S(Condition):\n"
                            "    def select(self, ctx):\n        return 1 / 0\n")
    rep = V.validate(cfg, "crashes", asof=days[59])
    assert not rep.ok and any("ZeroDivisionError" in e for e in rep.errors)

    _write(home, "net", "import requests\nfrom ktrade.sdk import Condition\nclass S(Condition):\n"
                        "    def select(self, ctx):\n        return []\n")
    rep = V.validate(cfg, "net", asof=days[59])
    assert any("네트워크" in w for w in rep.warnings)

    _write(home, "bad_exits", "from ktrade.sdk import Condition\nclass S(Condition):\n    def select(self, ctx):\n        return []\n",
           extra="exits:\n  stop: {type: magic}\n")
    rep = V.validate(cfg, "bad_exits", asof=days[59])
    assert not rep.ok and any("stop.type" in e for e in rep.errors)


def test_validate_unknown_condition_says_where(cfg):
    rep = V.validate(cfg, "nope")
    assert not rep.ok and "strategies" in rep.errors[0]


def test_llm_context_example_is_a_working_condition(market, cfg, home):
    """AI에게 주는 문서의 예제 코드가 그대로 엔진에서 돈다 (문서와 코드가 어긋나지 않게)."""
    store, days = market
    text = (DOCS / "LLM_CONTEXT.md").read_text(encoding="utf-8")
    code = re.search(r"## 완성 예제.*?```python\n(.*?)```", text, re.S).group(1)
    cls = re.search(r"class (\w+)\(Condition\)", code).group(1)
    assert cls
    (home / "strategies").mkdir(parents=True, exist_ok=True)
    (home / "strategies" / "doc_example.py").write_text(code, encoding="utf-8")   # 문서대로 파일 하나
    rep = V.validate(cfg, "doc_example", asof=days[59], params={"min_avg_value": 0})
    assert rep.ok, rep.render()
    assert any("엔진 검사 통과 1개" in n for n in rep.notes), rep.render()


def test_test_command_records_and_compares(market, cfg, home, capsys, monkeypatch):
    """조건을 바꿀 때마다 ktrade test → 실험이 쌓이고 직전 실험과 비교된다."""
    store, days = market
    import ktrade.cli as C
    monkeypatch.setattr(C, "_cfg", lambda: cfg)
    cli.main(["test", "example_breakout", "--start", days[21], "--end", days[-1]])
    out1 = capsys.readouterr().out
    assert "첫 실험" in out1 and "승률" in out1
    cli.main(["test", "example_breakout", "--start", days[21], "--end", days[-1], "--exits", '{"take_profit": {"type": "pct", "value": 5}}'])
    out2 = capsys.readouterr().out
    assert "바뀐 것: 청산 설정" in out2
    assert len(experiments.history(cfg, "example_breakout")) == 2
    cli.main(["experiments", "example_breakout"])
    assert "승률95%" in capsys.readouterr().out


def test_docs_command_copies(home, capsys):
    cli.main(["docs"])
    out = capsys.readouterr().out
    assert "LLM_CONTEXT.md" in out and (paths({"_home": str(home)})["home"] / "docs" / "LLM_CONTEXT.md").exists()


SINGLE = """\"\"\"한 파일 조건.\"\"\"
from ktrade.sdk import Condition, Context, Entry, Pick


class One(Condition):
    version = "v3"
    description = "파일 하나짜리"
    exits = {"stop": {"type": "pct", "value": 4}}
    default_params = {"n": 1}

    def select(self, ctx: Context) -> list[Pick]:
        return []
"""


def test_single_file_condition_is_discovered_and_loaded(cfg, home):
    from ktrade.engine import loader
    (home / "strategies").mkdir(parents=True, exist_ok=True)
    (home / "strategies" / "one_file.py").write_text(SINGLE, encoding="utf-8")
    (home / "strategies" / "_helper.py").write_text("X = 1\n", encoding="utf-8")       # _로 시작하면 조건 아님
    found = loader.discover(cfg)
    info = found["one_file"]
    assert (info.version, info.description, info.entry) == ("v3", "파일 하나짜리", "one_file.py:One")
    assert info.exits == {"stop": {"type": "pct", "value": 4}} and "_helper" not in found
    assert loader.load(info, {"n": 2}).params == {"n": 2}
    before = info.code_hash
    (home / "strategies" / "one_file.py").write_text(SINGLE.replace('"v3"', '"v4"'), encoding="utf-8")
    assert loader.discover(cfg)["one_file"].code_hash != before


def test_new_writes_single_file_into_first_strategies_dir(cfg, home, tmp_path, monkeypatch, capsys):
    from ktrade.engine import loader
    import ktrade.cli as C
    monkeypatch.setenv("KT_TEST_ROOT", str(tmp_path))
    cfg["strategies"]["dirs"] = ["${KT_TEST_ROOT}/mine"]
    monkeypatch.setattr(C, "_cfg", lambda: cfg)
    cli.main(["new", "my_cond"])
    f = tmp_path / "mine" / "my_cond.py"
    assert f.exists() and str(f) in capsys.readouterr().out
    info = loader.discover(cfg)["my_cond"]
    assert info.source == str(f) and info.version == "v1"
    assert not any("불러오기 실패" in e for e in V.validate(cfg, "my_cond").errors)
    cli.main(["new", "old_style", "--folder"])
    assert (tmp_path / "mine" / "old_style" / "strategy.yaml").exists()
    assert "old_style" in loader.discover(cfg)
