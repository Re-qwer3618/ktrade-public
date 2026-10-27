# 매매조건 작성·실험 가이드

ktrade는 **나만의 매매기법을 실험하는 엔진**입니다(키움증권 REST API 기반). 역할은 이렇게 나뉩니다.

| 내가 작성 (템플릿) | 엔진이 함 (설정으로 고름) |
|---|---|
| **종목 선정** — 장 마감 후 어떤 종목을 고를지 | **손절** — 매수가 -%, ATR 배수, N일 최저가, 없음 |
| **매수 타이밍** — 다음 거래일에 어떤 가격 조건에서 살지 | **이익실현** — 매수가 +%, 손절폭 배수(R), 추적 손절, 없음 |
| | **보유기간**, 손절 판단 시점(장 후반 / 즉시) |
| | 실행(모의투자·실전), 기록, **백테스트·실험 비교** |

매매조건을 바꾸거나 더할 때마다 `ktrade test`로 같은 엔진 규칙 아래에서 과거 확률을 재고, 이전 실험과 비교합니다.
과거 확률은 미래 수익을 보장하지 않습니다. 이 엔진은 기법을 다듬는 실험 도구입니다.

## 넣는 곳과 순서

```
~/.ktrade/                          (Windows: C:\Users\<이름>\.ktrade)
  config.yaml                       ← 엔진 공통 손절·익절(exits), 켤 매매조건(strategies.enabled)
  strategies/
    my_condition/                   ← 매매조건 하나 = 폴더 하나 (폴더 이름 = 조건 id)
      strategy.yaml                 ←   id·version·params, (선택) 이 조건만 쓸 exits
      strategy.py                   ←   Condition.select() — 종목 선정 + 매수 타이밍
  experiments/my_condition/         ← ktrade test 할 때마다 쌓이는 실험 기록(지표 json + 거래 csv)
  docs/                             ← 이 가이드·AI용 규격 사본
```

| 단계 | 명령 |
|---|---|
| 0. 데이터 | `config.yaml`의 `collect`에 받을 종목·시장·기간 → `ktrade collect` → `ktrade data`로 범위 확인 |
| 1. 뼈대 | `ktrade new my_condition` |
| 2. 작성 | `strategy.py`의 '종목 선정'·'매수 타이밍' 두 곳 — 직접 또는 AI (아래) |
| 3. 실험 | `ktrade test my_condition` = 검사 + 백테스트 + 직전 실험과 비교. 기간·대상 종목은 `config.yaml`의 `backtest` 또는 `--start --end --symbols` (DB에 있는 범위 안에서만) |
| 4. 고치고 다시 | 조건·파라미터·청산 중 무엇을 바꾸든 `ktrade test` → `ktrade experiments my_condition`으로 이력 |
| 5. 켜기 | `config.yaml` → `strategies.enabled: [{id: my_condition}]` (지금 설정으로 실험한 기록이 있어야 실행됨) |
| 6. 운영 | `broker: paper` → 키움 모의투자 → (본인 판단) 실전. `ktrade report`로 실제 결과 확인 |

### 직접 작성 / AI로 작성

- **직접**: 아래 설명과 형식 견본 2개(`example_breakout` 돌파 확인 매수, `example_pullback` 눌림 매수 — 기본으로 꺼져 있고
  수익을 노린 조건이 아닙니다)를 보고 씁니다.
- **AI**(Claude·Gemini·ChatGPT 등 본인 계정): `~/.ktrade/docs/LLM_CONTEXT.md`를 통째로 붙여 넣고 맨 아래 요청 양식을 채워 보내면
  두 파일을 써 줍니다. `ktrade test` 오류는 출력 그대로 AI에게 보여 주고 고치게 하세요. 어느 쪽이든 결과 책임은 작성자에게 있습니다.

## strategy.py — 매매조건 템플릿

```python
from ktrade.sdk import Condition, Context, Entry, Pick

class MyCondition(Condition):
    default_params = {"lookback": 20, "max_plans": 5}

    def select(self, ctx: Context) -> list[Pick]:
        bars = ctx.data.daily_all(bars=60)             # 전 종목 최근 일봉 (기준일까지만)
        picks = []
        for symbol, g in bars.groupby("symbol"):
            ...                                        # ① 종목 선정
            picks.append(Pick(
                symbol=symbol,
                entry=Entry.confirm(close, chase_pct=5),   # ② 매수 타이밍
                reason="왜 골랐는지",
            ))
        return picks
```

### 매수 타이밍 (Entry) — 실행일 = 기준일 다음 거래일

| 도우미 | 뜻 |
|---|---|
| `Entry.confirm(ref, chase_pct=5, after="15:10")` | 시가 ≥ ref 이고 15:10 이후에도 ref 위 ~ ref×(1+chase%) 이하면 매수 (돌파 유지 확인) |
| `Entry.at_open(ref, gap_pct=3)` | 시가가 ref ±gap% 안이면 장 시작에 매수 |
| `Entry.pullback(low, high)` | 그날 중 가격이 (low, high] 구간으로 내려오면 매수 |
| `Entry(min_open, min_price, max_price, after)` | 직접 지정 |

### ctx.data (읽기 전용, 기준일까지)

`daily(symbol)`, `daily_all(bars)`, `index("코스피"|"코스닥")`, `universe()`, `minute(symbol, day)`, `trading_days(n)` —
자세한 열 구성은 [LLM_CONTEXT.md](LLM_CONTEXT.md)의 SDK 표.

조건이 할 수 없는 것: 주문·계좌 조회·데이터 쓰기·네트워크(검사에서 오류/경고). 손절가·목표가도 만들지 않습니다.

## 손절·이익실현 (엔진 설정)

`config.yaml`의 `exits`가 모든 조건의 기본이고, 조건별로 `strategy.yaml`의 `exits`(또는 `enabled[].exits`)로 덮어씁니다.

```yaml
exits:
  stop: {type: pct, value: 7}          # pct | atr {value: 배수, period: 14} | low {value: N일, buffer_pct: 1} | none
  take_profit: {type: pct, value: 10}  # pct | r {value: 손절폭 배수} | none
  trailing: {activate_pct: 5, trail_pct: 4}   # +5% 이후 최고가에서 -4%면 매도 (null이면 끔)
  max_hold_days: 10
  stop_check: close                    # close: 15:10 이후에만 손절 판단 / intraday: 닿는 즉시
  scale_in:  [{pct: 50}, {pct: 30, drop_pct: 3}, {pct: 20, drop_pct: 6}]   # 분할매수 (선택, 비율 합 100)
  scale_out: [{pct: 50, gain_pct: 3}, {pct: 30, gain_pct: 6}]              # 분할익절 (선택, 합 100 이하)
```

- 손절·목표는 **실제 매수가 기준**으로 계산합니다(ATR·N일 최저가는 계획 시점 값). 분할매수로 평균 매수가가 바뀌면 같이 바뀝니다.
- 분할매수: 첫 차수는 바로 사고, 다음 차수는 **첫 매수가** 대비 `drop_pct`% 떨어지면 삽니다. 손절 %는 마지막 차수 간격보다 멀어야 합니다.
- 분할익절: **평균 매수가** 대비 `+gain_pct`%에서 체결된 수량의 `pct`%를 팝니다. 나머지는 목표가·추적 손절·보유기간으로 정리됩니다.
- 실전과 백테스트가 같은 계산을 씁니다. 백테스트에서 같은 날 손절과 목표가 둘 다 닿으면 손절로 봅니다(보수적).
  한 봉 안의 순서: 분할매수 체결 → 손절·추적 손절 → 분할익절 → 목표가 → 보유기간 만료.
- 매매조건 코드는 그대로 두고 청산만 바꿔 실험할 수 있습니다:
  `ktrade test my_condition --exits '{"stop": {"type": "atr", "value": 2}, "take_profit": {"type": "r", "value": 2}}'`

## 실험 결과 읽는 법 (`ktrade test`)

| 항목 | 뜻 |
|---|---|
| 후보 → 매수 → 청산 | 조건이 고른 수, 매수 타이밍이 맞아 산 수(발동률), 끝난 거래 수 |
| 승률 (95% 구간) | 이긴 거래 비율과 그 불확실성. 거래가 적으면 구간이 넓습니다 — 30건 미만은 참고만 |
| 평균·중앙값·이익/손실 평균·PF | 기대 수익의 크기. PF(이익 합 ÷ 손실 합) 1 미만이면 비용 뒤 손해 |
| 시장 대비 | 같은 기간 지수 수익률을 뺀 값 — 시장이 올라서 번 것인지 구분 |
| 앞 기간 / 뒤 기간 | 기간을 7:3으로 나눠 둘 다 같은 방향인지 — 한 시기에만 맞는 조건 걸러내기 |
| 청산 사유별·연도별 | 손절로 끝난 게 많은지, 특정 해에만 좋았는지 |
| 직전 실험 대비 | 무엇이 바뀌었고(코드·파라미터·청산) 지표가 얼마나 달라졌는지 |

조건을 여러 번 고쳐 같은 기간에 맞추다 보면 그 기간에만 맞게 됩니다(과최적화). 뒤 기간 결과, 다른 기간(`--start/--end`)으로도 확인하세요.
상장폐지 종목이 데이터에 없으면 결과가 실제보다 좋게 나옵니다(생존편향).

## 여러 조건 함께 쓰기

`strategies.enabled`에 여러 개를 적으면 모두 실행됩니다. 같은 종목을 둘이 고르면 위에 있는 조건이 가져가고,
비중·동시 보유 한도(`risk`)는 모든 조건 합계입니다. `ktrade compare cond_a cond_b`로 최근 실험을 나란히 봅니다.

## pip 패키지로 배포 (선택)

```toml
[project.entry-points."ktrade.strategies"]
my_condition = "my_pkg.condition:MyCondition"   # 클래스에 id·version 클래스 속성 필요
```

## 파이썬이 아닌 프로그램

[계획 파일 규격](PLAN_SCHEMA.md)에 맞춘 JSON을 `external_plans` 폴더에 쓰면 됩니다. 손절·목표가 비어 있으면 엔진 exits로 채웁니다.
