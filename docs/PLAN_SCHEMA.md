# 계획 파일 규격 (schema 1)

매매조건과 엔진 사이의 약속입니다. 파이썬 매매조건(`Condition.select`)의 결과에 엔진이 손절·익절을 붙여 이 파일을 만들고,
다른 언어·프로그램은 이 파일을 직접 만들어 `external_plans` 폴더에 두면 됩니다. 손절·목표(`stop`·`target`)를 비워 두면 엔진 `exits` 설정으로 채웁니다. JSON Schema: [`schemas/plan_file.schema.json`](../schemas/plan_file.schema.json)

파일 이름: `plans_<기준일>.json` 또는 `<기준일>.json` (기준일 = 판단에 쓴 마지막 일봉 날짜, YYYY-MM-DD).
엔진은 **기준일이 '오늘 이전 마지막 거래일'인 계획만** 실행합니다 — 계획 생성이 하루 빠지면 묵은 계획으로 매매하지 않습니다.

```json
{
  "schema": 1,
  "asof": "2026-10-16",
  "strategy": "my_strategy",
  "version": "v1",
  "status": "experimental",
  "generated_at": "2026-10-16T18:10:46",
  "plans": [
    {
      "plan_id": "my_strategy-v1-20261016-005930",
      "symbol": "005930",
      "name": "삼성전자",
      "market": "코스피",
      "entry": {"min_open": 70000, "min_price": 70000, "max_price": 75000, "after": "15:10"},
      "stop": 65000,
      "target": 77000,
      "max_hold_days": 10,
      "rank": 1,
      "basis": "선별 사유 (사람이 읽는 문장)",
      "meta": {}
    }
  ],
  "skipped": {}
}
```

| 필드 | 필수 | 뜻 |
|---|---|---|
| `schema` | ✔ | 1 |
| `asof` | ✔ | 기준일 |
| `strategy`, `version` | ✔ | 매매조건 id·버전 — 기록·성적 비교의 단위 |
| `plans[].symbol` | ✔ | 6자리 종목코드 |
| `plans[].entry.min_open` | ✔ | 실행일 시가가 이 값 이상이어야 매수 |
| `plans[].entry.min_price`, `max_price` | ✔ | `after` 이후 현재가가 (min, max] 안이면 매수 |
| `plans[].entry.after` | | 매수 판단 시작 시각 HH:MM (기본 15:10) |
| `plans[].stop` | | 손절가 (비우면 엔진 exits로 채움, `use_engine_exits: true`면 항상 엔진 값) |
| `plans[].target` | | 목표가 (위와 같음) |
| `plans[].exits` | | 엔진 청산 설정 사본 (엔진이 만든 파일에 들어감 — 실제 손절·목표는 매수가 기준으로 다시 계산) |
| `plans[].max_hold_days` | | 매수일을 1일째로 세어 이 거래일째 15:15 이후 매도 (기본 20) |
| `plans[].rank` | | 작을수록 우선 |
| `plans[].plan_id` | | 없으면 엔진이 `<strategy>-<version>-<asof>-<symbol>`로 만듦 |
| `plans[].basis` | | 선별 사유 |

엔진 검사(통과 못 하면 실행 안 함, `_rejected.json`에 사유): 가격 양수, `min_price < max_price`, (있으면) `stop < min_price`, `target > min_price`,
가격이 기준일 종가 ±30% 안, `after` 형식, `max_hold_days` 1~120, 전략당 하루 계획 수 한도, 다른 전략과 종목 중복(우선순위 높은 쪽).
