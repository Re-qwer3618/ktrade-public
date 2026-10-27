# 변경 기록

## 0.6.0 — 2026-10-27

- 영웅문 조건검색식 1a: `ktrade hts list`(조건식 목록), `ktrade hts scan "<이름|번호>" [--csv] [--limit]`(지금 걸린 종목, 주문 없음) —
  키움 REST 웹소켓 CNSRLST / CNSRREQ(일반), 연속조회, 이름 일부로 찾기(여럿이면 후보 안내), 응답 없음·오류 코드 안내. 모의서버에서 확인
- 영웅문 조건식 1b·1c·1d: `ktrade hts watch` — 장중 실시간 편입·이탈(웹소켓 하나에 조건검색+체결가, 재연결 시 재등록,
  동시 등록 10개 한도·0.5초 간격·무응답 재시도), 판단(매수 시간대·가격·하루 한도·재매수·즉시/N초 확인·추격 한도)을 신호 일지에.
  `mode: trade`인 조건식은 매매(paper·키움 모의만, 실전은 막힘) — 매수·손절·익절·추적 손절·당일 청산은 기존 Executor·엔진 청산,
  조건식 기본 청산은 단타용(-2% 즉시 손절, +3% 익절, +2% 뒤 -1% 추적, 당일 15:15 청산), 이탈 매도(exit_on_remove)
- `ktrade hts signals`(신호 일지 요약), `ktrade hts report [--collect]`(성적표: 모든 편입·매수 신호를 1분봉으로 '그때 샀다면' + 실제 매매),
  대시보드 '🔎 영웅문 조건식' 탭(설정·날짜별 신호·성적·실제 매매, 1분봉 받기 버튼). 신호만 있어도 대시보드가 열림
- 조건식 성적표: 같은 종목이 몇 초마다 편입·이탈을 되풀이하면 편입을 수백 번으로 세던 문제 — 이탈 뒤 `hts.episode_gap_min`(기본 10분)
  안 재편입은 같은 편입(묶음)으로, 묶음마다 첫 편입가로 '그때 샀다면' 계산. 신호 일지 원자료는 그대로
- 분할매수·분할익절 (`exits.scale_in` / `exits.scale_out`): 차수·비율·간격. 손절·목표·추적 손절은 평균 매수가 기준.
  백테스트(일봉)·조건식 성적표(1분봉)·실매매가 같은 규칙(`exits.PositionSim`)을 씀. 설정 검사(비율 합, 간격 순서, 손절이 마지막 분할매수보다 가까우면 오류)
- 계좌 한도 (`risk`): 종목당 매수 금액 `order_amount`, 신규 매수 시간대 `buy_window`, 하루 수익 목표 `daily_profit_target`·손실 한도
  `daily_loss_limit`(닿으면 그날 신규 매수 중지 + 보유 전량 매도), 시간 일괄청산 `flatten_at`, 미체결 자동 취소 `order_timeout_sec`
  (키움 kt10003 잔량 취소, 체결된 만큼만 기록). 계좌 단위 한도는 백테스트에 반영되지 않음
- 실행 기록: 종목당 주문 하나만 걸어 두고(pending) 차수별 매수(fills)·매도(sells)를 남김, 평균 매수가·평균 매도가. 늦은 체결·수동 매매는 계좌 수량에 맞춤.
  예전 DB는 열을 자동 추가(예전 보유 기록은 한 차수로 읽음)
- 긴급 정지·중지 요청(설정값 `trading.halt` / `process.stop.*`) — 다음 단계 운영 화면에서 씀. 오늘 손익(`trading.pnl`)·조건식 감시 상태(`hts.heartbeat`) 기록
- 실험 기록이 같은 초에 두 번 저장되면 순서가 뒤바뀌던 문제 수정
- 대시보드 **🕹️ 운영** 탭 (첫 탭): 상단 상태(모드·엔진·오늘 손익·긴급 정지/해제), 왼쪽 설정 5단계(조건식 → 익절·손절·추적 → 분할
  → 리스크 한도 → 시작·중지, 키움 조건식 목록 불러오기), 오른쪽 편입 종목·보유·미체결·체결 + 실시간 로그(3초 갱신, 오늘 기록이 없으면
  마지막 기록일). 저장은 `config.yaml`의 hts_conditions·risk 블록만 바꿔 쓰고 나머지 줄·주석은 그대로(`config.update_sections`, 백업 .bak).
  시작은 `ktrade hts watch`를 별도 프로세스로(로그 `~/.ktrade/logs/hts_watch_*.log`), 중지는 요청을 남겨 엔진이 5초 안에 스스로 끝냄
- 예전(차수 기록 전) 매매 기록도 체결 내역·오늘 손익에 매수·매도 한 번씩으로 읽음

## 0.5.3 — 2026-10-18

- 설치(Windows): "os error 448 — 경로에 신뢰할 수 없는 탑재 지점이 포함" 오류 수정. 최신 uv는 파이썬 폴더 옆에 정션(폴더 바로가기)을
  만들고 `--python 3.12`로 만든 환경이 그 정션을 거치는데, 회사 PC 등 Windows가 사용자 정션 통과를 막으면 설치·실행이 실패한다.
  이제 파이썬을 먼저 받고 **실제 폴더의 python.exe 경로**로 ktrade 환경을 만들어 정션을 쓰지 않음 (uv 0.13.1로 확인).
  `~/.local/bin`에 python3.12.exe를 만들지 않음(`--no-bin`)

## 0.5.2 — 2026-10-18

- 설치: uv 설정이 PC에 깔린 파이썬만 쓰게 해 둔 PC("Python preference is set to 'only system'")에서 실패하던 문제 —
  설치 과정에서만 uv 관리 파이썬을 쓰고 받도록 함(`UV_PYTHON_PREFERENCE=managed`, `--python-preference managed`, PC 설정은 그대로).
  환경변수로 막은 경우와 uv.toml로 막은 경우 모두 확인

## 0.5.1 — 2026-10-18

- 설치: uv 설정이 파이썬 다운로드를 막아 둔 PC("Python downloads are set to 'never'")에서 설치가 실패하던 문제 —
  설치 과정에서만 다운로드를 허용(`UV_PYTHON_DOWNLOADS=automatic`, PC 설정은 그대로). 3.12를 못 받으면 PC에 있는 파이썬 3.11~3.13으로 재시도
- 설치(Windows): uv 단계가 실패해도 다음 단계로 넘어가 "'ktrade' 용어가 인식되지 않습니다"로 끝나던 문제 — 실패한 단계에서 바로 멈추고 원인을 보여 줌
- 설치 프로그램 실패 화면: "인터넷 연결 확인" 한 줄 대신 실제 원인(error 줄)·흔한 원인·로그 위치 안내

## 0.5.0 — 2026-10-18

- 대시보드 첫 화면: **키움 로그인**(앱키·시크릿키 입력 → 연결 확인 → OS 자격 증명 저장소에 저장, `.env`를 직접 만들 필요 없음)과
  **체험 모드**(키 없이 가상 종목·가상 시세로 예제 매매조건 백테스트·계획·매매 기록·성적·차트를 둘러보기, `~/.ktrade/demo/`)
- 로그인 뒤 DB가 비어 있으면 대시보드에서 몇 종목 시세 받기
- 대시보드 새 탭: **매매조건 실험**(백테스트 실행·결과·실험 기록), **종목 차트**(이동평균선), **사용법**
- `ktrade login [--real] [--no-check]` / `ktrade logout`. `ktrade init`은 더 이상 빈 `.env`를 만들지 않음 (기존 `.env`·환경변수는 계속 읽고 우선)
- Windows 설치 프로그램 `ktrade-setup.exe` (릴리스마다 자동 첨부), 마지막 화면에서 대시보드 열기
- 의존성 추가: keyring

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
