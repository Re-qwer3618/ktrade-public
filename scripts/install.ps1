# ktrade 설치 (Windows PowerShell 5.1+)
#
#   powershell -ExecutionPolicy Bypass -File install.ps1                      # 최신 릴리스 설치
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Source .\ktrade-<버전>-py3-none-any.whl   # 받은 wheel 파일로
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Source git+https://github.com/Re-qwer3618/ktrade-public   # 개발 중인 최신 코드 (git 필요)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -NoDashboard -NoShortcut   # 대시보드·바로가기 없이 (명령만)
#
# 하는 일: uv(파이썬 도구 관리자)가 없으면 공식 설치 스크립트로 설치 → uv로 ktrade 설치(자체 파이썬 3.12 환경, 시스템 파이썬을 건드리지 않음,
#         대시보드 포함) → ktrade init(~/.ktrade 설정·키 파일) → 바탕화면·시작 메뉴 바로가기(대시보드·명령창·폴더) → ktrade doctor --offline.
#         관리자 권한·git 필요 없음.

param(
    [string]$Source = "",
    [switch]$Dashboard,       # 예전 옵션 (지금은 기본으로 대시보드 포함)
    [switch]$NoDashboard,     # 대시보드 구성 요소(streamlit) 없이 가볍게
    [switch]$NoShortcut,      # 바탕화면·시작 메뉴 바로가기를 만들지 않음
    [switch]$NoShellUpdate    # PATH(사용자 환경변수)를 고치지 않음 — 시험 설치용
)
$ErrorActionPreference = "Stop"
$Repo = "Re-qwer3618/ktrade-public"

function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

if (-not (Have "uv")) {
    Write-Host "uv가 없어 설치합니다 (https://astral.sh/uv)..." -ForegroundColor Cyan
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Have "uv")) { throw "uv 설치 후에도 찾지 못했습니다. 새 PowerShell 창에서 다시 실행하세요." }
}

if (-not $Source) {
    Write-Host "최신 릴리스 확인: https://github.com/$Repo/releases" -ForegroundColor Cyan
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "ktrade-install" }
    $whl = $rel.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
    if (-not $whl) { throw "릴리스 $($rel.tag_name)에 wheel 파일이 없습니다." }
    $Source = $whl.browser_download_url
}

$pkg = $Source
if (-not $NoDashboard) {
    if ($Source -like "git+*" -or $Source -like "http*") { $pkg = "ktrade[dashboard] @ $Source" } else { $pkg = "$Source[dashboard]" }
}
Write-Host "ktrade 설치: $pkg" -ForegroundColor Cyan
uv tool install --python 3.12 --force "$pkg"
if (-not $NoShellUpdate) { uv tool update-shell | Out-Null }
$bin = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { "$env:USERPROFILE\.local\bin" }
$env:Path = "$bin;$env:Path"

ktrade init
if (-not $NoShortcut) {
    Write-Host ""
    ktrade shortcut install
}
Write-Host ""
ktrade doctor --offline
Write-Host ""
Write-Host "다음 단계" -ForegroundColor Green
Write-Host "  1. 키움 앱키를 %USERPROFILE%\.ktrade\.env 에 적기 (KIWOOM_APP_KEY, KIWOOM_APP_SECRET)"
Write-Host "  2. ktrade doctor                     (키움 연결 확인, 조회만)"
Write-Host "  3. config.yaml 의 collect 에 받을 종목·기간 → ktrade collect --dry-run → ktrade collect"
Write-Host "  4. ktrade new my_condition → strategy.py 작성 → ktrade test my_condition"
Write-Host "  5. ktrade docs                       (작성 가이드·AI용 규격)"
if (-not $NoShortcut) { Write-Host "바탕화면의 'ktrade 명령창'을 열면 바로 ktrade 명령을 쓸 수 있고, 'ktrade 대시보드'는 더블클릭으로 열립니다." }
Write-Host "새 터미널을 열어야 ktrade 명령이 잡힐 수 있습니다."
