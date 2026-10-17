# 변경 기록

## 0.4.3 — 2026-10-17

- 바로가기: 한국어가 아닌 Windows(영문 등)에서 한글 이름 바로가기를 저장하지 못하던 문제 수정(영문 임시 이름으로 저장 후 바꿈), PowerShell 호출을 UTF-16 인코딩으로
- 'ktrade 명령창' 첫 화면을 ktrade intro로 (점검 + 자주 쓰는 명령)

## 0.4.2 — 2026-10-17

- 바로가기: `ktrade shortcut install|remove|show` — 'ktrade 대시보드'(더블클릭으로 브라우저에 열림), 'ktrade 명령창', 'ktrade 폴더'
  (Windows 바탕화면 + 시작 메뉴, macOS 바탕화면 .command, Linux 앱 메뉴 .desktop). 설치 스크립트가 자동으로 만듦(`-NoShortcut` / `NO_SHORTCUT=1`로 끔)
- 설치 스크립트가 대시보드를 기본으로 함께 설치(`-NoDashboard` / `NO_DASHBOARD=1`로 가볍게)
- `ktrade dashboard --open`: 브라우저로 열기, 이미 떠 있으면 그 창만 열기, 오류 시 창이 바로 닫히지 않게 대기. `--port`

## 0.4.1 — 2026-10-17

- 첫 공개 배포 (https://github.com/Re-qwer3618/ktrade-public), 라이선스 **MIT**
- 면책 고지 `DISCLAIMER.md` (키움증권과 무관한 개인 프로젝트, 투자 조언 아님, 수익 보장 없음)
- 설치 스크립트가 git 없이 최신 릴리스 wheel을 받아 설치, PowerShell·sh 한 줄 설치

## 0.4.0 — 2026-10-17

자기 매매기법을 실험하는 엔진으로 정리 — **사용자는 매매조건(종목 선정 + 매수 타이밍)만 쓰고, 손절·익절·보유기간은 엔진이 맡는다.**
수익을 보장하지 않습니다. 과거 데이터에서 그 조건이 얼마나 자주 맞았는지 실험하는 도구입니다.

- 매매조건 템플릿 `ktrade.sdk.Condition`: `select(ctx) -> list[Pick]`, 매수 타이밍은 `Entry.confirm / at_open / pullback`
- 엔진 청산 `exits`: 손절(pct·atr·low), 익절(pct·r배수), 트레일링, 최대 보유일, 장중/종가 손절 — 실매매·백테스트가 같은 함수, 매수가 기준 계산
  - `config.yaml` 기본값 → `strategy.yaml`의 `exits` → `strategies.enabled[].exits` 순으로 덮어씀. 조건 코드를 안 고치고 청산만 바꿔 실험 가능
- `ktrade test <조건>`: 백테스트 + 실험 기록(`~/.ktrade/experiments/`) + 직전 실험과 비교. 승률 95% 구간(Wilson), PF, 최대 낙폭,
  연속 손실, 지수 대비, 앞 70%·뒤 30% 기간 일관성, 청산 사유·연도별 분해. 같은 날 손절·익절이 모두 닿으면 손절로 셈(보수적)
- `ktrade experiments <조건>`, `ktrade compare <조건...>`: 실험 이력·조건 간 비교
- `strategies.require_backtest: true`(기본): 지금 코드·파라미터·청산 설정으로 실험한 기록이 없는 조건은 계획을 만들지 않음
- `ktrade new <id>`가 Condition 템플릿 생성, `ktrade strategies`가 백테스트 상태 표시
- 외부 계획 파일의 `stop`·`target`은 선택(비우면 엔진 exits로 채움, `use_engine_exits`로 항상 엔진 값)
- 예제 조건 `example_breakout` v2(돌파 확인 매수), `example_pullback`(눌림 매수, ATR 손절·2R 익절)
- 문서: `CONDITION_GUIDE.md`(STRATEGY_GUIDE 대체), LLM_CONTEXT·README·PLAN_SCHEMA·OPERATIONS 갱신
- **데이터 범위를 직접 정함** (`config.yaml collect`, `ktrade collect` 옵션은 그 실행에서만): 종목 지정(`symbols`·`symbols_file`) 또는 시장 전체,
  `exclude`, 기간 `start`~`end`(또는 `years`), 1분봉 종목 `minute.symbols`. 이어받기 + 시작일을 앞당기면 앞부분 채움. `--dry-run`으로 범위만 확인
- `ktrade data`: DB에 받아 둔 범위(종목 수·기간·지수·분봉, `--symbols`·`--all`로 종목별)
- **백테스트 기간·대상 종목** (`config.yaml backtest.start·end·years·symbols`, `--start --end --symbols --symbols-file`): DB 범위 밖은 맞춰 자르고 알려 줌,
  대상 종목은 실험 기록에 남고 직전 실험과 비교할 때 '대상 종목' 변경으로 표시
- 엔진만 배포: 예제 조건은 형식 견본으로 동봉만 하고 기본으로 켜지 않음(`strategies.enabled: []`), doctor는 켠 조건이 없으면 안내만
- Windows에서 출력을 파일로 보낼 때 cp949 때문에 죽던 문제 수정
- **호환 깨짐**: 0.3 이하의 PlanStrategy·SignalStrategy 폴더는 건너뛰고 옮기는 법을 알려 줌(손절·익절 코드는 지우고 exits 설정으로)

## 0.3.0 — 2026-10-17

- 배포 범위 확정: **엔진만** 배포, 종목 선정·매매 전략은 사용자가 직접(손으로 또는 본인 AI로) 작성해 넣는다. 엔진에 AI 기능은 넣지 않음
- `ktrade validate <전략>`: 형식·금지 import(주문·저장소·수집기)·네트워크/외부 명령 경고·최근 거래일 실행 결과·엔진 검사·소요 시간
- `ktrade docs`, `ktrade init`이 전략 작성 가이드·AI용 규격을 `~/.ktrade/docs`에, 전략 폴더 안내를 `~/.ktrade/strategies/README.md`에 둔다
- `docs/LLM_CONTEXT.md`: Claude·Gemini·ChatGPT에 붙여 넣는 자체 완결 규격 + 요청 양식 (예제 코드는 테스트로 실제 동작 확인)
- 전략 작성 가이드에 '넣는 곳과 순서', 직접 작성 / AI 작성 두 경로

## 0.2.0 — 2026-10-17

- `ktrade daily`: 장 마감 후 한 번에 수집(증분) → 계획 → 계획·보유 종목 1분봉, 단계별 결과를 `logs/daily_<날짜>.json`에
- `ktrade report`: 전략·버전별 성적 — 실행 기록 + 계획 자체 결과(체결 여부 무관, 일봉 근사)
- `ktrade doctor`: 설치·키·키움 연결(조회만)·데이터 신선도·전략·자동 실행 점검
- `ktrade schedule install|remove|show [--dry-run]`: Windows 작업 스케줄러 / cron 안내
- `ktrade dashboard` (선택 설치 `ktrade[dashboard]`): 보기 전용, 이 PC에서만
- 1분봉 수집 (ka10080), `python -m ktrade` 실행 지원
- 설치 스크립트 `scripts/install.ps1`, `scripts/install.sh`, 문서 INSTALL·OPERATIONS, CI(Windows·Ubuntu × Python 3.11·3.13)

## 0.1.0 — 2026-10-17

- 엔진과 전략 플러그인 분리: 전략 SDK(PlanStrategy·SignalStrategy, 기준일까지만 보이는 DataView), 전략 폴더·entry point 탐색
- 계획 검사(guard), 계획 파일 schema 1 + JSON Schema, 외부 계획 파일 연결
- 실행: paper / 키움 모의투자(실전은 --live-confirm), 시세판·휴장 판단, 여러 전략 기록
- 백테스트(실전과 같은 전략 코드·규칙, 비용 반영), 예제 전략 2개, CLI
