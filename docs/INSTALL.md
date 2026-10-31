# 설치 가이드

## 준비물

| 항목 | 설명 |
|---|---|
| PC | Windows 10/11 (권장), macOS, Linux. 장중에 켜 두어야 함 |
| 키움증권 계좌 | 본인 명의. 모의투자로 시작하려면 키움 모의투자 신청 |
| 키움 REST API 앱키 | 키움 OpenAPI 사이트에서 본인 계좌로 발급 (앱키·시크릿키). 모의투자용 키를 따로 받는 경우가 있으니 사이트 안내를 따르세요 |

키는 본인만 씁니다. 다른 사람에게 주거나 저장소에 올리지 마세요.

## 1. 설치

**Windows — 설치 프로그램 (처음이라면 이것)**: [ktrade-setup.exe](https://github.com/Re-qwer3618/ktrade-public/releases/latest/download/ktrade-setup.exe)를 받아 더블클릭.
관리자 권한 없이 설치되며, 아래 스크립트와 같은 일을 합니다(설치 중 검은 창에 진행 상황이 나오고, 인터넷 연결 필요).
대시보드·바로가기는 설치 화면에서 끌 수 있습니다. "Windows의 PC 보호" 창이 뜨면 **추가 정보 → 실행**.
실패하면 로그 `%LOCALAPPDATA%\ktrade-setup\install.log`를 확인하세요. 제거는 설정 → 앱 → ktrade
(자동 실행·바로가기·프로그램을 지우고 `~/.ktrade`는 남김). 무인 설치: `ktrade-setup.exe /VERYSILENT`.

**Windows** — PowerShell에서 한 줄:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Re-qwer3618/ktrade-public/main/scripts/install.ps1 | iex"
```

또는 [릴리스 페이지](https://github.com/Re-qwer3618/ktrade-public/releases/latest)에서 `install.ps1`을 받아:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1             # 최신 릴리스 (대시보드·바로가기 포함)
powershell -ExecutionPolicy Bypass -File install.ps1 -NoDashboard -NoShortcut   # 명령만 가볍게
powershell -ExecutionPolicy Bypass -File install.ps1 -Source .\ktrade-<버전>-py3-none-any.whl   # 받은 wheel 파일로
```

**macOS / Linux**:

```bash
curl -LsSf https://raw.githubusercontent.com/Re-qwer3618/ktrade-public/main/scripts/install.sh | sh
NO_DASHBOARD=1 NO_SHORTCUT=1 sh install.sh      # 받은 install.sh로, 명령만 가볍게
```

스크립트가 하는 일: [uv](https://docs.astral.sh/uv/) 설치(없을 때) → ktrade 설치(자체 파이썬 3.12 환경, 시스템 파이썬을 건드리지 않음)
→ `ktrade init` → 바로가기 → `ktrade doctor --offline`. 관리자 권한은 필요 없습니다.

바로가기 (Windows: 바탕화면 + 시작 메뉴 'ktrade' 폴더, macOS: 바탕화면 `.command`, Linux: 앱 메뉴 `.desktop`):

- **ktrade 대시보드**: 더블클릭하면 대시보드를 띄우고 브라우저로 엽니다(`ktrade dashboard --open`). 뜬 창을 닫으면 꺼집니다.
- **ktrade 명령창**: `ktrade` 명령을 바로 쓸 수 있는 터미널.
- **ktrade 폴더**: `~/.ktrade` (설정·키·데이터·매매조건).

실행 파일 이름은 `ktrade`(Windows `%USERPROFILE%\.local\bin\ktrade.exe`). 아이콘을 더블클릭하는 프로그램이 아니라 터미널 명령이며,
설치 직후엔 새 터미널을 열어야 잡힙니다. 바로가기는 `ktrade shortcut install|remove|show`로 다시 만들거나 지웁니다.

직접 설치: [uv](https://docs.astral.sh/uv/) 설치 후 `uv tool install --python 3.12 <릴리스의 .whl 주소 또는 파일>` → `ktrade init`.
개발 중인 최신 코드는 `uv tool install --python 3.12 "git+https://github.com/Re-qwer3618/ktrade-public"` (git 필요).

## 2. 로그인 (키 넣기)

바탕화면의 **ktrade 대시보드**를 열면(`ktrade dashboard --open`) 처음 화면에서 고릅니다.

- **키움 로그인**: 앱키·시크릿키를 넣고 키 종류(모의투자/실전투자)를 고른 뒤 **연결 확인 후 저장**.
  삼성전자 현재가를 한 번 조회해 확인하고(주문 없음), 키는 이 PC의 Windows 자격 증명 관리자(macOS 키체인, Linux Secret Service)에
  저장됩니다. 그런 저장소가 없는 환경에서만 `~/.ktrade/credentials.json`에 둡니다. 로그인하면 바로 대시보드가 열리고,
  차트 데이터는 받지 않아도 됩니다(백테스트·계획에 쓸 때만 📥 데이터 탭에서).
- **백테스트**: 키움 로그인만으로 모두 씁니다 — 내 매매조건·장중 조건 실험, '계획에 쓸 매매조건 켜기'까지 (0.9.3부터 별도 로그인 없음).

명령창에서 하려면:

```bash
ktrade login           # 앱키·시크릿키 입력 (모의투자 키). 실전 키는 ktrade login --real
ktrade doctor          # ✔ 키움 API 연결 이 나오면 성공 (조회만 하고 주문은 안 함)
ktrade logout          # 저장한 키 지우기
```

예전 방식인 `~/.ktrade/.env`(`KIWOOM_APP_KEY=…`, `KIWOOM_APP_SECRET=…`)와 환경변수도 계속 읽으며, 있으면 그쪽이 우선합니다.

## 3. 데이터 받기 (범위는 직접 정함)

`~/.ktrade/config.yaml`의 `collect`에 받을 종목(`symbols`, 비우면 `market_names` 시장 전체)·기간(`start`·`end`, 또는 `years`)을 적습니다.
시장 전체일 때는 뺄 종류(`exclude_kinds`: ETF·ETN·스팩·리츠·우선주), 전일 종가(`price`)·시가총액(`market_cap`, 억 원) 범위로 좁히고,
분봉(`minute`)·틱(`tick`)도 받을 수 있습니다. 대시보드 **📥 데이터** 탭에서 같은 설정을 고르고 대상 종목을 미리 볼 수 있습니다.

```bash
ktrade collect --dry-run                              # 받을 범위만 확인
ktrade collect --symbols 005930,000660 --years 5      # 몇 종목만 먼저 (몇 초)
ktrade collect --limit 100                            # 시장 전체 중 100종목 (몇 분)
ktrade collect                                        # config대로 (전 종목이면 처음엔 몇 시간, 이후엔 이어받기라 몇 분)
ktrade data                                           # 받아 둔 범위·종류별 용량 (~/.ktrade/data/market/)
ktrade data-dir D:\ktrade-data                        # 저장 위치를 다른 드라이브로 (기존 데이터도 옮김, 대시보드 📥 데이터 탭에서도 가능)
```

## 4. 연습 → 모의투자

1. `config.yaml`의 기본은 `broker: paper` — 실제 주문 없이 가상 체결로 돌아갑니다(시세는 키움에서 받음).
2. 내 매매조건을 만들어(`ktrade new <조건>`) 실험하고(`ktrade test <조건>`), `strategies.enabled`에 켜서 paper로 며칠 돌려 봅니다(`ktrade run`).
3. 키움 모의투자 계좌로: `broker: kiwoom`, `kiwoom.is_mock: true`.
4. 실전(`is_mock: false`)은 충분한 검증 뒤 본인 판단으로만. `ktrade run --live-confirm` 없이는 시작하지 않습니다.

## 5. 자동 실행

```bash
ktrade schedule install --dry-run   # 등록될 내용 미리 보기
ktrade schedule install             # Windows: 작업 스케줄러 ktrade-daily(평일 16:30)·ktrade-run(평일 08:50)
```

PC가 로그온해 있어야 실행됩니다. 절전 모드에 들어가지 않게 전원 설정을 확인하세요. 운영은 [OPERATIONS.md](OPERATIONS.md).

## 업데이트·삭제

```bash
uv tool upgrade ktrade          # 업데이트 (설정·데이터·내 전략은 ~/.ktrade에 그대로)
ktrade schedule remove          # 자동 실행 해제
ktrade shortcut remove          # 바로가기 지우기
uv tool uninstall ktrade        # 프로그램 삭제 (~/.ktrade는 직접 지우기 전까지 남음)
```
