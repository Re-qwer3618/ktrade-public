# ktrade 매매조건 작성 규격 (AI에게 붙여 넣는 문서)

> **쓰는 법**: Claude·Gemini·ChatGPT 같은 AI 채팅에 이 문서 전체를 붙여 넣고, 맨 아래 "요청 양식"을 채워 보내세요.
> AI가 `strategy.yaml`과 `strategy.py` 두 파일을 써 주면 `~/.ktrade/strategies/<조건 id>/`에 저장하고 `ktrade test <조건 id>`를 실행합니다
> (검사 + 백테스트 + 이전 실험과 비교). 직접 작성해도 같은 규격입니다.
> ktrade는 키움증권 REST API로 만든 **매매기법 실험 엔진**입니다. 과거 데이터로 조건의 확률을 재 볼 뿐, 실전 수익을 보장하지 않습니다.

---

## AI에게: 당신이 할 일

당신은 ktrade용 조건 플러그인을 작성한다. 종류는 두 가지이고, 요청 양식에 어느 쪽인지 적혀 있다.
- **매매조건 (kind: condition)** — 장 마감 후 하루 한 번 종목 선정 + 다음 날 매수 타이밍. 아래 '매매조건' 절.
- **장중 조건 (kind: intraday)** — 장중에 '지금 사도 되나' 판단. 문서 끝 '장중 조건' 절.

아래는 매매조건 설명이다. 매매조건 = **종목 선정 + 매수 타이밍** 두 가지뿐이다.
**손절·이익실현·보유기간은 쓰지 않는다** — 엔진이 사용자 설정(exits)으로 처리하고, 실전과 백테스트에서 똑같이 적용한다.

출력은 두 파일뿐이다.
1. `strategy.yaml` — manifest
2. `strategy.py` — `ktrade.sdk.Condition`을 상속한 클래스 하나, `select(ctx) -> list[Pick]` 구현

지켜야 할 것:
- import는 `ktrade.sdk`, `pandas`, `numpy`, 파이썬 표준 라이브러리의 계산용 모듈(math, statistics, datetime 등)만.
  `ktrade.broker`·`ktrade.engine`·`ktrade.data`·`kiwoom_client`·`sqlite3` import 금지(검사에서 오류).
  네트워크(requests, httpx, urllib), 외부 명령(subprocess), 파일 쓰기는 쓰지 않는다 — 백테스트와 실전이 같게 돌아야 한다.
- 데이터는 `ctx.data`(읽기 전용, 기준일까지만)로만 본다. 미래 정보를 쓰려 하지 않는다.
- 손절가·목표가·매도 조건·매수 수량·비중을 만들지 않는다 (엔진·사용자 설정 몫).
- 숫자 기준(기간, 배수, %)은 `default_params`에 두어 사용자가 바꿀 수 있게 한다.
- 한국어 주석으로 '종목 선정'과 '매수 타이밍' 부분을 표시한다.
- 조건이 수익을 낸다고 주장하지 않는다. `ktrade test`로 확인하라고 안내한다.

## 실행 흐름 (엔진이 하는 일)

1. 장 마감 후 하루 한 번 `select(ctx)` 호출 (ctx.asof = 그날, 그날까지의 데이터).
2. 돌려준 `Pick`마다 엔진이 손절·목표(exits 설정)를 붙여 계획을 만들고 검사한다.
3. 다음 거래일: `Pick.entry` 조건이 맞으면 매수 → 엔진 규칙으로 손절·익절·추적 손절·보유기간 매도.
4. 백테스트(`ktrade test`)는 과거 거래일마다 1~3을 똑같이 돌려 승률(95% 구간)·평균 수익·시장 대비·앞뒤 기간 일관성을 낸다.

## strategy.yaml

```yaml
id: my_condition         # 영문 소문자·숫자·_ 만, 폴더 이름과 같게
version: v1              # 조건을 바꾸면 v2, v3 … (실험 기록이 버전별로 쌓임)
kind: condition
entry: strategy.py:MyCondition
description: 한 줄 설명
author: 작성자
params: {}               # 비워 두면 strategy.py의 default_params
# exits:                 # (선택) 이 조건만 손절·익절을 다르게 — 코드가 아니라 설정
#   stop: {type: pct, value: 5}            # pct | atr(value=배수, period) | low(value=N일, buffer_pct) | none
#   take_profit: {type: r, value: 2}       # pct | r(손절폭 배수) | none
#   trailing: {activate_pct: 5, trail_pct: 4}
#   max_hold_days: 10
#   stop_check: close                      # close(15:10 이후 판단) | intraday(닿는 즉시)
#   scale_in:  [{pct: 50}, {pct: 50, drop_pct: 3}]   # 분할매수 (첫 매수가 -3%에서 나머지)
#   scale_out: [{pct: 50, gain_pct: 3}]               # 분할익절 (평균가 +3%에서 절반)
```

## SDK

```python
from ktrade.sdk import Condition, Context, Entry, Pick
```

### Context
- `ctx.asof: str` — 기준일 "YYYY-MM-DD"
- `ctx.data` — 아래 메서드 (모두 기준일까지만)
- `ctx.params: dict` (= `self.params`), `ctx.log.info("...")`

### ctx.data
| 메서드 | 반환 |
|---|---|
| `daily(symbol, bars=300)` | DataFrame, index=날짜(DatetimeIndex), 열 `open high low close volume` (수정주가, 오래된 → 최근) |
| `daily_all(bars=300)` | 전 종목 긴 표: 열 `symbol date open high low close volume` (`date`는 "YYYY-MM-DD" 문자열), symbol·date 순 |
| `index(market="코스피", bars=300)` | 지수 일봉 ("코스피"/"코스닥") |
| `universe()` | `code name market market_name state last_price list_count order_warning audit_info up_name up_size` — market: "코스피"/"코스닥", market_name: "거래소"/"코스닥"/"ETF"/…, state에 "관리종목"·"거래정지" 등, last_price 전일 종가(원)·list_count 상장주식수(시가총액 = 둘의 곱)·order_warning "0" 없음/"2"~"5" 투자유의 — 종목 목록을 받은 날 기준(예전 목록이면 빈 값) |
| `minute(symbol, day)` | 그날 1분봉 (수집된 종목만) |
| `trading_days(n)` | 최근 n 거래일 |

`daily_all(bars=N)`은 전 종목 × N일이라 N을 필요한 만큼만(보통 30~260). 백테스트는 거래일마다 select()를 부른다.
"전 종목"은 사용자가 받아 둔 DB의 종목이다(수집 범위는 사용자가 정함). 백테스트에서 대상 종목을 좁히면 `daily_all`·`universe`·`daily`에
그 종목만 보이므로, 조건 코드에 종목코드를 하드코딩하지 말고 주어진 데이터 안에서 고르게 쓴다.

### Pick (돌려주는 것)
```python
Pick(
    symbol="005930",                 # 6자리 종목코드
    entry=Entry.confirm(70000),      # 매수 타이밍 (아래 셋 중 하나 권장)
    name="삼성전자", market="코스피",
    rank=1,                          # 작을수록 우선 (하루 계획 수 한도 안에서 위부터)
    reason="왜 골랐는지 한 문장",
    meta={"거래량배수": 1.8},         # 숫자 근거 (선택) — 나중에 결과를 이 값별로 나눠 볼 수 있음
)
```

### Entry (매수 타이밍) — 실행일 = 기준일 다음 거래일
| 도우미 | 뜻 | 백테스트 체결 |
|---|---|---|
| `Entry.confirm(ref, chase_pct=5, after="15:10")` | 시가 ≥ ref 이고 15:10 이후에도 ref 초과 ~ ref×(1+chase%) 이하이면 매수 (돌파 유지 확인) | 그날 종가 |
| `Entry.at_open(ref, gap_pct=3)` | 시가가 ref ±gap% 안이면 09:00부터 그 범위에서 매수 | 그날 시가 |
| `Entry.pullback(low, high)` | 그날 중 현재가가 (low, high] 구간으로 오면 매수 (시가 조건 없음) | 구간 가격 |

직접 만들 수도 있다: `Entry(min_open, min_price, max_price, after="HH:MM")` — 시가 ≥ min_open 이고 after 이후 현재가가 (min_price, max_price]이면 매수.

엔진 검사: 가격 > 0, `min_price < max_price`, 가격이 기준일 종가 ±30% 안, symbol 6자리, 하루 계획 수 한도, 다른 조건과 종목 중복(우선순위 높은 쪽).

## 완성 예제

```python
"""20일 최고 종가를 과열 없이 돌파한 종목을 다음 날 확인 후 산다 (예시 — 검증된 조건 아님)."""
from ktrade.sdk import Condition, Context, Entry, Pick


class Breakout(Condition):
    default_params = {"lookback": 20, "min_price": 2000, "min_avg_value": 5e8, "max_vol_ratio": 4.0,
                      "chase_pct": 5, "max_plans": 5}

    def select(self, ctx: Context) -> list[Pick]:
        p = self.params
        n = int(p["lookback"])
        bars = ctx.data.daily_all(bars=n + 2)
        uni = ctx.data.universe()
        # 관리종목·거래정지 제외, 주식만
        uni = uni[uni["market_name"].isin(["거래소", "코스닥"]) & ~uni["state"].fillna("").str.contains("관리종목|거래정지")]
        info = uni.set_index("code")
        picks = []
        for sym, g in bars.groupby("symbol"):
            if sym not in info.index or len(g) < n + 1 or g["date"].iloc[-1] != ctx.asof:
                continue
            close, vol = float(g["close"].iloc[-1]), float(g["volume"].iloc[-1])
            prev = g.iloc[-(n + 1):-1]
            if close < p["min_price"] or (prev["close"] * prev["volume"]).mean() < p["min_avg_value"]:
                continue
            prev_high, avg_vol = float(prev["close"].max()), float(prev["volume"].mean())
            # ── 종목 선정: 종가로 n일 신고가 돌파 + 과열(평균의 4배 이상) 거래량 제외 ──
            if close <= prev_high or avg_vol <= 0 or vol / avg_vol >= p["max_vol_ratio"]:
                continue
            picks.append(Pick(
                symbol=sym, name=str(info.at[sym, "name"]), market=str(info.at[sym, "market"]),
                # ── 매수 타이밍: 다음 날 돌파 유지 확인 (시가·15:10 이후 모두 오늘 종가 위, +5%까지) ──
                entry=Entry.confirm(close, chase_pct=p["chase_pct"], after="15:10"),
                reason=f"{ctx.asof} 종가 {close:,.0f}원이 {n}일 최고 종가 {prev_high:,.0f}원 돌파",
                meta={"extension_pct": round((close / prev_high - 1) * 100, 2)},
            ))
        picks.sort(key=lambda x: x.meta["extension_pct"])  # 덜 추격한 순 — 우선순위 기준은 조건마다 정한다
        for i, pk in enumerate(picks, 1):
            pk.rank = i
        return picks[: int(p["max_plans"])]
```

## 요청 양식 (사용자가 채워서 보냄)

```
위 ktrade 매매조건 규격대로 strategy.yaml, strategy.py 두 파일을 써 줘. 손절·익절은 쓰지 마 (엔진 설정 몫).

- 조건 id: (예: box_breakout)
- 종목 선정: (예: 최근 60일 박스권 상단을 종가로 넘고, 거래량이 20일 평균의 1.5~4배, 주가 2천원 이상, 20일 평균 거래대금 5억 이상)
- 제외할 종목: (예: 관리종목, 거래정지, 최근 5일 30% 이상 급등)
- 매수 타이밍: (예: 다음 날 시가가 돌파일 종가 이상이고 15시 10분 이후에도 그 위면, 돌파일 종가 +5%까지 / 또는 다음 날 시가 / 또는 20일선 근처로 눌리면)
- 하루 최대 후보 수: (예: 5)
- 우선순위: (예: 거래대금 큰 순)
- (선택) 이 조건만 쓸 손절·익절: (예: 손절 박스 하단 = 60일 최저가 -1%, 익절 손절폭의 2배) → strategy.yaml의 exits로
```

받은 뒤: `ktrade test <id>` → 오류가 나면 출력 그대로 AI에게 보여 주고 고치게 → 통과하면 결과(승률·95% 구간·앞뒤 기간 일관성)를 보고
`~/.ktrade/config.yaml` `strategies.enabled`에 추가 → 먼저 paper/모의투자로 운영. 조건을 바꿀 때마다 `ktrade test`를 다시 돌린다.

---

## 장중 조건 (kind: intraday)

장중에 **"지금 이 종목을 사도 되나"** 만 판단한다. 종목은 엔진이 넘겨준다(조건검색 편입 종목, 또는 장 마감 후 매매조건이 고른 감시 목록).
쓰이는 곳 두 가지 — 같은 코드를 그대로 쓴다:
1. **조건검색 편입 확인**: 영웅문 조건식이 종목을 편입하면, 사기 전에 이 조건으로 한 번 더 확인 (`ctx.event == "insert"`, `ctx.insert_price` = 편입가)
2. **감시 목록 장중 신호**: 매매조건이 고른 종목을 다음 날 장중에 지켜보다 이 조건이 맞으면 매수 (`ctx.event == "watch"`). 매매조건의 Entry는 무시된다

데이터: 실전은 **키움 실시간 데이터**(현재가·오늘 1분봉·일봉 조회), 백테스트는 받아 둔 1분봉. 미래 데이터는 없다.

```yaml
id: my_intraday
version: v1
kind: intraday
entry: strategy.py:MyIntraday
description: 한 줄 설명
params: {}
```

```python
from ktrade.sdk import Intraday, Signal


class MyIntraday(Intraday):
    default_params = {"start": "09:05", "end": "14:30", "lookback": 5, "vol_mult": 2.0}

    def check(self, ctx):
        p = self.params
        m = ctx.minutes                      # 오늘 1분봉(지금까지), 열 open high low close volume, 인덱스 시각
        # ── 시간대 ──
        if not (p["start"] <= ctx.now.strftime("%H:%M") <= p["end"]) or len(m) < p["lookback"] + 1:
            return None
        # ── 판단 ──
        prev = m.iloc[-(p["lookback"] + 1):-1]
        vwap = ctx.vwap()
        if vwap and ctx.price > vwap and ctx.price > prev["high"].max() \
                and m["volume"].iloc[-1] >= prev["volume"].mean() * p["vol_mult"]:
            return Signal.yes(f"VWAP {vwap:,.0f} 위 · {p['lookback']}분 고가 돌파", max_price=round(ctx.price * 1.01, 2))
        return None
```

ctx (IntradayContext):
| 이름 | 뜻 |
|---|---|
| `ctx.price` | 지금 가격 (실전 키움 현재가 / 백테스트 그 분봉 종가) |
| `ctx.minutes` | 오늘 1분봉 DataFrame (지금까지만, 오래된 → 최근) |
| `ctx.daily` | 어제까지 일봉 DataFrame (최근 300개, 수정주가) |
| `ctx.now` | 지금 시각 (datetime) |
| `ctx.vwap()` | 오늘 거래량 가중 평균가 (분봉 없으면 None) |
| `ctx.day_open` · `ctx.day_high` · `ctx.prev_close` | 오늘 시가·고가, 어제 종가 |
| `ctx.event` · `ctx.insert_price` | "insert"(편입 확인) 또는 "watch"(감시 목록) · 편입가 |
| `ctx.symbol` · `ctx.name` · `ctx.params` | 종목코드·이름·파라미터 |

돌려주는 값: 살 때 `Signal.yes("이유", max_price=추격 상한)`, 아니면 `None`.
지킬 것: 매매조건과 같음(import 제한, 손실제한·이익실현 코드 금지). **가볍게** — 장중에 종목마다 자주 부른다(분봉 수백 개 계산 정도까지).
분봉이 적은 장 초반(`len(ctx.minutes)`가 작을 때)은 None을 돌려준다. 숫자 기준은 `default_params`에.

## 요청 양식 — 장중 조건 (사용자가 채워서 보냄)

```
위 ktrade 규격의 '장중 조건(kind: intraday)'으로 strategy.yaml, strategy.py 두 파일을 써 줘. 손실제한·이익실현 코드는 쓰지 마.

- 조건 id: (예: vwap_pullback)
- 쓰는 곳: (조건검색 편입 확인 / 감시 목록 장중 신호 / 둘 다)
- 매수 판단: (예: VWAP 위에 있고, 직전 10분 고가를 거래량 2배 이상으로 돌파하면. 편입 확인일 때는 편입가보다 1% 넘게 오른 상태면 사지 않음)
- 시간대: (예: 09:05 ~ 14:30)
- 추격 상한: (예: 신호 가격 +1%)
```

받은 뒤: `~/.ktrade/strategies/<id>/`에 저장 → `ktrade validate <id>` → 대시보드 자동매매 탭 조건식의 **파이썬 확인**, 또는 백테스트 정식판
'계획에 쓸 매매조건'의 **장중 조건**으로 붙인다. 성적은 백테스트 정식판 '장중 조건' 탭(조건식 편입 + 파이썬 확인)과 '실험'(매매조건 + 장중 조건).
