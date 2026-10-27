"""영웅문 조건식 목록·1회 검색 — 키움 대신 가짜 웹소켓."""
from __future__ import annotations

import asyncio

import pytest

from ktrade import cli
from ktrade.data import hts
from ktrade.data.hts import HtsClient, HtsCondition, HtsError


class FakeWS:
    """send()된 요청마다 미리 정한 응답을 trnm 콜백으로 돌려준다. reply(payload) → dict | None(무응답)."""

    def __init__(self, reply):
        self.reply, self.sent, self.cb = reply, [], {}
        self.connected = self.closed = False

    def on_trnm(self, trnm, fn):
        self.cb.setdefault(trnm, []).append(fn)

    async def connect(self):
        self.connected = True

    async def listen(self):
        while not self.closed:
            await asyncio.sleep(0.01)

    async def disconnect(self):
        self.closed = True

    async def send(self, payload):
        self.sent.append(payload)
        msg = self.reply(payload)
        if msg is not None:
            async def later():
                await asyncio.sleep(0)
                for fn in self.cb.get(msg.get("trnm"), []):
                    await fn(msg)
            asyncio.get_running_loop().create_task(later())


LIST = {"trnm": "CNSRLST", "return_code": 0, "return_msg": "",
        "data": [["0", "매수총량_200"], ["6", "01_오전스켈핑"], ["15", "스켈핑 전략-1"], ["7", "02_오후종목"]]}


def row(code, name, price, rate, vol):
    return {"9001": code, "302": name, "10": f"{price:09d}", "25": "2", "11": "000000092", "12": f"{rate:08d}",
            "13": f"{vol:010d}", "16": "000001950", "17": "000002190", "18": "000001902"}


def client(reply, timeout=1.0):
    ws = FakeWS(reply)
    return HtsClient({"_secrets": {}, "kiwoom": {}}, ws_factory=lambda cfg: ws, timeout=timeout), ws


def test_list_parses_and_closes():
    c, ws = client(lambda p: LIST)
    conds = c.list()
    assert conds[1] == HtsCondition("6", "01_오전스켈핑") and len(conds) == 4
    assert ws.sent == [{"trnm": "CNSRLST"}] and ws.closed


def test_list_accepts_dict_rows():
    assert hts.parse_list({"data": [{"seq": "3 ", "name": " 점검용 "}]}) == [HtsCondition("3", "점검용")]


def test_find_by_seq_name_and_unique_part():
    conds = hts.parse_list(LIST)
    assert hts.find(conds, "6").name == "01_오전스켈핑"
    assert hts.find(conds, "02_오후종목").seq == "7"
    assert hts.find(conds, "오후").seq == "7"
    with pytest.raises(HtsError, match="여럿"):
        hts.find(conds, "스켈핑")
    with pytest.raises(HtsError, match="없습니다"):
        hts.find(conds, "없는조건")


def test_scan_paginates_and_parses():
    pages = {"": {"trnm": "CNSRREQ", "seq": "6  ", "return_code": 0, "cont_yn": "Y", "next_key": "K1",
                  "data": [row("A001510", "SK증권", 2080, 463, 12278678), row("Q500001", "어떤ETN", 10050, -120, 500)]},
             "K1": {"trnm": "CNSRREQ", "seq": "6", "return_code": 0, "cont_yn": "N", "next_key": "",
                    "data": [row("A005870", "휴니드", 4830, -133, 3271525), row("A001510", "SK증권", 2080, 463, 12278678)]}}

    def reply(p):
        return LIST if p["trnm"] == "CNSRLST" else pages[p["next_key"]]
    c, ws = client(reply)
    cond, df = c.scan("오전")
    assert cond.seq == "6"
    assert [s["trnm"] for s in ws.sent] == ["CNSRLST", "CNSRREQ", "CNSRREQ"]
    assert ws.sent[1]["search_type"] == "0" and ws.sent[2]["cont_yn"] == "Y" and ws.sent[2]["next_key"] == "K1"
    assert list(df.code) == ["001510", "500001", "005870"]            # 접두어 제거, 중복 제거
    sk = df.iloc[0]
    assert sk["price"] == 2080 and sk["rate"] == pytest.approx(4.63) and sk["volume"] == 12278678 and sk["kind"] == "주식"
    assert df.iloc[1]["kind"] == "ETN" and df.iloc[1]["rate"] == pytest.approx(-1.20)


def test_scan_error_code_and_timeout():
    c, _ = client(lambda p: LIST if p["trnm"] == "CNSRLST" else {"trnm": "CNSRREQ", "return_code": 100, "return_msg": "조건 없음"})
    with pytest.raises(HtsError, match="return_code 100"):
        c.scan("0")
    c, ws = client(lambda p: None, timeout=0.2)
    with pytest.raises(HtsError, match="응답이 없습니다"):
        c.list()
    assert ws.closed


def test_no_keys_message():
    with pytest.raises(HtsError, match="앱키"):
        hts._default_factory({"_secrets": {"app_key": "", "app_secret": ""}, "kiwoom": {}})


def test_cli_list_and_scan(cfg, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)

    def reply(p):
        if p["trnm"] == "CNSRLST":
            return LIST
        return {"trnm": "CNSRREQ", "return_code": 0, "data": [row("A001510", "SK증권", 2080, 463, 12278678)]}
    ws = FakeWS(reply)
    monkeypatch.setattr(hts, "_default_factory", lambda c: ws)
    cli.main(["hts", "list"])
    out = capsys.readouterr().out
    assert "조건식 4개" in out and "01_오전스켈핑" in out
    f = tmp_path / "r.csv"
    cli.main(["hts", "scan", "6", "--csv", str(f)])
    out = capsys.readouterr().out
    assert "[6] 01_오전스켈핑 — 1종목" in out and "SK증권" in out and "+4.63%" in out and f.exists()
    with pytest.raises(SystemExit):
        cli.main(["hts", "scan"])
