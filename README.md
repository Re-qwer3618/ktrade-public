# ktrade

**나만의 매매기법을 만들기 위한 실험 엔진** — 키움증권 REST API로 만들었습니다.

내가 생각한 매매조건(종목 선정·매수 타이밍)이 과거에 **얼마나 자주, 얼마나 맞았는지** 같은 규칙으로 재 보고,
조건을 바꾸거나 더할 때마다 다시 재서 비교합니다. 마음에 드는 조건은 키움 모의투자(또는 본인 판단으로 실전)로 그대로 돌려 봅니다.

> ⚠️ **수익을 보장하지 않습니다.** 과거 확률은 미래 결과가 아니며, 이 프로그램은 투자 조언이 아닙니다. 기본은 가상 체결(paper)·
> 키움 모의투자이고, 실전 계좌 사용·조건 선택·손실은 모두 사용자 책임입니다.
>
> **키움증권과 무관한 개인 오픈소스 프로젝트입니다** (키움증권이 만들거나 승인한 프로그램이 아님). 키움 REST API는 각자 본인 계좌·본인 앱키로
> 키움증권 약관에 따라 신청해 씁니다. 자세한 건 [면책 고지](DISCLAIMER.md).

**엔진만 들어 있습니다.** 매매조건은 들어 있지 않고(동봉 예제 2개는 작성 형식을 보여 주는 견본, 기본으로 꺼져 있음),
백테스트 결과도 없습니다. 내 매매조건 + 내가 받은 데이터(DB)가 있으면 그때 백테스트가 돌아갑니다.

## 역할

| 내가 작성 (템플릿 하나) | 엔진이 함 |
|---|---|
| **종목 선정** — 장 마감 후 어떤 종목을 고를지 | **손절·이익실현·추적 손절·보유기간** (설정으로 고름) |
| **매수 타이밍** — 다음 거래일 어떤 가격 조건에서 살지 | 데이터 수집, 계획 검사, 주문 실행, 기록 |
| | **백테스트 + 실험 기록·비교** (조건을 바꿀 때마다) |
| **받을 데이터 범위** — 종목·시장·기간 (설정) | 키움 REST API로 받아 DB에 쌓기(이어받기) |

```
~/.ktrade/strategies/<조건 id>/strategy.yaml + strategy.py   ← 매매조건 (직접 또는 Claude·Gemini 등 AI로 작성)
~/.ktrade/config.yaml  collect / backtest                    ← 받을 데이터 범위, 백테스트 기간·대상 종목
~/.ktrade/config.yaml  exits / strategies.enabled            ← 엔진 손절·익절 설정, 켤 조건
~/.ktrade/data/market.db                                     ← 키움에서 받은 시세 (ktrade data로 범위 확인)
~/.ktrade/experiments/<조건 id>/                              ← ktrade test 할 때마다 쌓이는 실험 기록
```

## 설치

자세한 건 [설치 가이드](docs/INSTALL.md). 설치 스크립트가 [uv](https://docs.astral.sh/uv/)와 자체 파이썬 환경까지 준비하고
[최신 릴리스](https://github.com/Re-qwer3618/ktrade-public/releases/latest)를 설치합니다(관리자 권한·git·파이썬 미리 설치 필요 없음).

**Windows — 가장 쉬운 방법**: [ktrade-setup.exe 내려받기](https://github.com/Re-qwer3618/ktrade-public/releases/latest/download/ktrade-setup.exe) → 더블클릭 → '다음'만 누르면 됩니다.
설치 중 인터넷에서 필요한 것을 받으며, 마지막 화면에서 대시보드를 바로 열 수 있습니다.
(서명되지 않은 개인 프로젝트라 "Windows의 PC 보호" 창이 뜨면 **추가 정보 → 실행**을 누르세요.)
지우기: 설정 → 앱 → ktrade → 제거 (설정·데이터 폴더 `~/.ktrade`는 남음).

Windows (PowerShell 한 줄):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Re-qwer3618/ktrade-public/main/scripts/install.ps1 | iex"
```

macOS / Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/Re-qwer3618/ktrade-public/main/scripts/install.sh | sh
```

설치가 끝나면 바탕화면과 시작 메뉴에 바로가기 3개가 생깁니다.

| 바로가기 | 하는 일 |
|---|---|
| **ktrade 대시보드** | 더블클릭 → 대시보드(로그인·운영·매매조건 실험·계획·매매 기록·성적·차트)를 브라우저로 엶. 뜬 창을 닫으면 꺼짐 |
| **ktrade 명령창** | `ktrade` 명령을 바로 쓸 수 있는 터미널 (점검 결과를 먼저 보여 줌) |
| **ktrade 폴더** | 설정·키·데이터·내 매매조건 폴더(`~/.ktrade`) |

실행 파일 이름은 `ktrade`입니다(Windows: `%USERPROFILE%\.local\bin\ktrade.exe`). 바로가기를 다시 만들거나 지우려면 `ktrade shortcut install` / `remove`.

**처음 화면 (대시보드)**

- **🔑 키움 로그인** — 본인 키움 REST API 앱키·시크릿키를 입력하면 연결을 확인하고 이 PC의 Windows 자격 증명 관리자
  (macOS 키체인)에 저장합니다. 파일을 따로 만들 필요가 없습니다. 이어서 대시보드에서 몇 종목 시세를 받아 바로 실험할 수 있습니다.
  명령창에서는 `ktrade login` (실전 키는 `--real`), 지우기는 `ktrade logout`.
- **🧪 체험 모드** — 키 없이 가상 종목·가상 시세로 모든 화면(예제 매매조건 백테스트·계획·매매 기록·성적·차트)을 둘러봅니다.
  어떤 프로그램인지 보고 계속 쓸지 정할 때. 숫자는 가상이라 의미가 없습니다.

(예전 방식인 `~/.ktrade/.env`·환경변수 `KIWOOM_APP_KEY`/`KIWOOM_APP_SECRET`도 계속 읽으며, 있으면 그쪽이 우선합니다.)

**🕹️ 운영 탭 (영웅문 조건식 자동매매)**

- 위: 모드(paper·모의·실전), 조건식 엔진·`ktrade run` 동작 여부, 오늘 손익(실현·평가), **긴급 정지**(오늘 신규 매수 중지 + 보유 전량 매도)
- 왼쪽 설정 5단계: ① 조건식(영웅문 목록 불러오기, 매매/관찰, 매수 시간·하루 한도·확인 초·추격 한도·이탈 매도) → ② 익절·손절·추적 손절
  → ③ 분할매수·분할익절 → ④ 리스크 한도(종목당 금액, 동시 보유, 하루 수익 목표·손실 한도, 미체결 취소, 매수 시간대, 시간 일괄청산)
  → 저장(`config.yaml`, 바꾸기 전 파일은 `config.yaml.bak`) → ⑤ 시작·중지
- 오른쪽: 편입 종목 · 보유 · 미체결 · 체결, 실시간 로그 (3초마다 갱신)
- 대시보드가 직접 주문하지는 않습니다. **시작**은 `ktrade hts watch`를 따로 띄우고(대시보드를 닫아도 계속), 주문은 그 엔진이 냅니다.
  조건식 매매는 paper·키움 모의투자에서만 주문하고 실전 계좌는 막혀 있습니다.

## 1. 데이터 범위 정하고 받기

무엇을 얼마나 받을지는 직접 정합니다. `config.yaml`의 `collect`가 기본이고(`ktrade daily`도 이대로 매일 이어받음),
`ktrade collect` 옵션은 그 실행에서만 덮어씁니다.

```yaml
collect:
  symbols: [005930, 000660]    # 종목 직접 지정 — 비면 market_names 시장 전체
  symbols_file: null           # 또는 종목 파일 (한 줄에 하나)
  market_names: [거래소, 코스닥] # ETF도 쓰려면 ETF 추가
  exclude: []                  # 빼고 받을 종목
  start: 2020-01-01            # 비면 오늘부터 years년 전
  end: null                    # 비면 오늘까지
  minute: {symbols: [], start: null}   # 1분봉도 받을 종목 (키움이 주는 범위까지)
```

```bash
ktrade collect --dry-run                                    # 받을 범위만 확인
ktrade collect                                              # config대로 받기 (이미 있는 구간은 건너뜀)
ktrade collect --symbols 005930,000660 --start 2018-01-02   # 이번만 이 종목·기간
ktrade collect --markets 거래소,코스닥,ETF --years 5 --limit 100
ktrade data                                                 # DB에 받아 둔 범위 (종목 수·기간·분봉)
ktrade data --symbols 005930                                # 종목별 범위
```

시작일을 앞당기면 모자란 앞부분을 채웁니다. 전 종목 몇 년치는 처음 몇 시간 걸립니다(다른 DB가 있으면 `ktrade import-db`).

## 2. 매매조건 쓰고 백테스트하기

백테스트 = **내 매매조건 + DB**. 기간·대상 종목은 `config.yaml`의 `backtest`(또는 명령 옵션)로 정하고, DB에 있는 범위 안에서만 돕니다.

```yaml
backtest:
  start: null        # 비면 DB 마지막 거래일부터 years년 전
  end: null          # 비면 DB 마지막 거래일
  years: 3
  symbols: []        # 이 종목만 대상으로 — 비면 DB에 있는 전 종목
```

```bash
ktrade new my_condition           # 매매조건 뼈대 — strategy.py의 '종목 선정'·'매수 타이밍' 두 곳만 고친다
ktrade test my_condition          # 검사 + 백테스트 + 직전 실험과 비교 (조건을 바꿀 때마다)
ktrade test my_condition --start 2021-01-04 --end 2023-12-29 --symbols 005930,000660   # 이번만 이 기간·종목
ktrade experiments my_condition   # 이 조건의 실험 이력
ktrade compare my_condition my_condition2       # 여러 조건 나란히
ktrade test my_condition --exits '{"stop": {"type": "atr", "value": 2}, "take_profit": {"type": "r", "value": 2}}'
                                  # 조건은 그대로, 엔진 손절·익절만 바꿔 실험
```

`ktrade test`가 보여 주는 것: 후보 → 매수(발동률) → 청산 건수, **승률과 95% 구간**, 평균·중앙값·이익/손실 평균·PF, 시장 대비,
**앞 기간/뒤 기간 일관성**, 청산 사유별·연도별, 직전 실험에서 무엇이 바뀌었고 지표가 얼마나 달라졌는지.
작성법은 [매매조건 가이드](docs/CONDITION_GUIDE.md), AI로 쓰려면 [LLM_CONTEXT.md](docs/LLM_CONTEXT.md)를 AI 채팅에 붙여 넣으세요
(설치 후 `~/.ktrade/docs/`에도 있음).

## 3. 손절·이익실현 (엔진 설정)

```yaml
# ~/.ktrade/config.yaml — 모든 조건 공통. 조건별로는 strategy.yaml의 exits로 덮어쓰기
exits:
  stop: {type: pct, value: 7}          # pct | atr | low(N일 최저가) | none
  take_profit: {type: pct, value: 10}  # pct | r(손절폭 배수) | none
  trailing: {activate_pct: null, trail_pct: null}
  max_hold_days: 10
  stop_check: close                    # close(15:10 이후 판단) | intraday(닿는 즉시)
  scale_in:  [{pct: 50}, {pct: 50, drop_pct: 3}]   # 분할매수 (선택) — 첫 매수가 -3%에서 나머지 절반
  scale_out: [{pct: 50, gain_pct: 3}]               # 분할익절 (선택) — 평균가 +3%에서 절반
```

실전과 백테스트(일봉)·조건식 성적표(1분봉)가 같은 계산(`exits.PositionSim`, 평균 매수가 기준)을 씁니다.

계좌 단위 한도(`risk`): 종목당 매수 금액 `order_amount`(원), 신규 매수 시간대 `buy_window`, 하루 수익 목표 `daily_profit_target`·
손실 한도 `daily_loss_limit`(원 — 닿으면 그날 신규 매수 중지 + 보유 전량 매도), 시간 일괄청산 `flatten_at`, 미체결 자동 취소
`order_timeout_sec`. 계좌 단위 한도는 실제 계좌 흐름에서만 의미가 있어 백테스트(종목별 거래)에는 반영되지 않습니다.

## 4. 모의투자로 돌려 보기

```bash
# config.yaml strategies.enabled 에 조건 추가 (지금 설정으로 ktrade test 한 기록이 있어야 실행됨)
ktrade daily                      # 장 마감 후: 수집 → 다음 거래일 계획
ktrade run                        # 장중: 계획대로 매수, 엔진 규칙대로 매도 (기본 paper, 키움 모의는 broker: kiwoom)
ktrade schedule install           # 평일 자동 실행 (Windows 작업 스케줄러 16:30 daily · 08:50 run)
ktrade report                     # 실제 결과 (매매조건·버전별)
ktrade status · ktrade doctor · ktrade dashboard
```

운영·문제 해결은 [운영 가이드](docs/OPERATIONS.md). 실전 계좌(`kiwoom.is_mock: false`)는 `ktrade run --live-confirm`으로만 시작합니다.

## 5. 영웅문 조건검색식 (미리보기)

영웅문4 '조건검색(0150)'에서 만들어 저장한 조건식을 ktrade에서 바로 볼 수 있습니다 (조회만, 주문 없음).

```bash
ktrade hts list                       # 내 조건식 목록 (번호·이름)
ktrade hts scan "01_오전스켈핑"        # 지금 그 조건식에 걸린 종목 — 이름 일부나 번호도 됨
ktrade hts scan 41 --csv result.csv   # 결과를 CSV로
```

- 조건식은 키움 서버가 계산하므로 과거 백테스트는 할 수 없습니다. 다음 단계에서 장중 편입 신호를 쌓아 성적을 매기는 **신호 일지**와
  조건식 편입 자동매매(모의)가 붙습니다.
- 키움 웹소켓 실시간 세션은 계정당 하나라, 같은 키로 실시간 시세·조건검색을 쓰는 다른 프로그램이 떠 있으면 서로 끊길 수 있습니다.
- 모의투자 키로도 영웅문에 저장한 조건식이 그대로 보입니다.

## 주요 설정

| 키 | 기본 | 뜻 |
|---|---|---|
| `broker` | `paper` | `paper`(가상 체결) / `kiwoom` |
| `kiwoom.is_mock` | `true` | 키움 모의투자 서버 |
| `exits.*` | 손절 -7%, 익절 +10%, 10일 | 엔진 손절·익절 (위) |
| `collect.symbols` / `symbols_file` / `market_names` | 없음 / 없음 / 거래소·코스닥 | 받을 종목 (지정이 없으면 시장 전체) |
| `collect.start` / `end` / `years` | 없음 / 없음 / 3 | 받을 기간 |
| `collect.minute.symbols` | 없음 | 1분봉도 받을 종목 |
| `backtest.start` / `end` / `years` / `symbols` | 없음 / 없음 / 3 / 없음 | 백테스트 기간·대상 종목 (없으면 DB 전 종목) |
| `backtest.cost_pct` / `oos_ratio` | 0.25 / 0.3 | 왕복 비용, 확인용 뒤 기간 비율 |
| `strategies.enabled` | 없음 | 켤 내 조건(위에 있을수록 우선), 조건별 `params`·`exits` |
| `strategies.require_backtest` | `true` | 지금 설정으로 실험한 기록이 없는 조건은 실행 안 함 |
| `risk.max_position_pct` / `max_concurrent_positions` | 2.0 / 5 | 종목당 비중, 동시 보유 한도 (조건 합계) |
| `risk.order_amount` / `buy_window` | 없음 / 없음 | 종목당 매수 금액(원, 정하면 비중 대신), 신규 매수 시간대 |
| `risk.daily_profit_target` / `daily_loss_limit` | 없음 / 없음 | 하루 수익 목표·손실 한도(원) — 닿으면 그날 매수 중지 + 전량 매도 |
| `risk.flatten_at` / `order_timeout_sec` | 없음 / 없음 | 시간 일괄청산(예: "15:15"), 미체결 자동 취소(초) |

## 알아둘 것

- 매매조건 코드(`strategy.py`)는 일반 파이썬 코드입니다. 믿을 수 있는 조건만 넣으세요 — `ktrade test`가 주문·저장소 접근 같은 금지 사항을 잡지만 샌드박스는 아닙니다.
- 백테스트는 일봉 근사입니다(같은 날 손절·목표가 둘 다 닿으면 손절로 봄). 상장폐지 종목이 데이터에 없으면 생존편향으로 좋게 나옵니다.
  같은 기간에 조건을 계속 맞추면 그 기간에만 맞게 됩니다 — 뒤 기간·다른 기간으로도 확인하세요.
- 다른 사람에게 본인의 매매 신호·조건을 제공하는 것은 투자자문·일임 관련 규제 대상일 수 있습니다.

## 개발

```bash
uv sync --extra dev
uv run pytest
```

라이선스: [MIT](LICENSE) · [면책 고지](DISCLAIMER.md) · 변경 기록: [CHANGELOG.md](CHANGELOG.md) · 버그·제안: [Issues](https://github.com/Re-qwer3618/ktrade-public/issues)
