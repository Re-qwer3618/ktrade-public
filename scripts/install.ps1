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

# 외부 프로그램(uv·ktrade)은 실패해도 PowerShell 5.1이 스크립트를 멈추지 않는다 — 종료 코드를 직접 확인해 바로 멈춘다.
# (예전엔 uv 설치 실패 뒤에도 ktrade init까지 진행해 "'ktrade' 용어가 인식되지 않습니다"라는 엉뚱한 오류로 끝났다)
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what 실패 (종료 코드 $LASTEXITCODE) — 위 오류 메시지를 확인하세요." } }

if (-not (Have "uv")) {
    Write-Host "uv가 없어 설치합니다 (https://astral.sh/uv)..." -ForegroundColor Cyan
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Have "uv")) { throw "uv 설치 후에도 찾지 못했습니다. 새 PowerShell 창에서 다시 실행하세요." }
}

# ktrade는 uv가 받아 오는 자체 파이썬 3.12에서 돈다. 이 PC의 uv 설정(환경변수 또는 uv.toml)이
#  - 파이썬 다운로드를 막거나("Python downloads are set to 'never'")
#  - PC에 깔린 파이썬만 쓰게 해 두면("Python preference is set to 'only system'")
# 설치가 실패하므로, 이 설치 과정에서만 uv 관리 파이썬을 쓰고 받도록 한다 (이 창의 환경변수만 바꾸며, PC 설정 자체는 그대로 둔다).
# 환경변수는 uv.toml 설정보다 우선하고, 아래 uv 명령에는 --python-preference도 직접 넘긴다.
$env:UV_PYTHON_DOWNLOADS = "automatic"
$env:UV_PYTHON_PREFERENCE = "managed"

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
# 파이썬 3.12를 먼저 받고, '실제 폴더'의 python.exe 경로를 직접 넘긴다.
# 최신 uv는 cpython-3.12.x 폴더 옆에 cpython-3.12-... 정션(폴더 바로가기)을 만들고 `--python 3.12`로 만든 환경이 그 정션을
# 거쳐 파이썬을 찾는다. 회사 PC 등에서 Windows가 사용자가 만든 정션 통과를 막으면(os error 448 "신뢰할 수 없는 탑재 지점")
# 설치도 실행도 실패한다. 실제 폴더 경로를 주면 ktrade 환경이 정션을 쓰지 않는다 (uv 0.13.1로 확인).
# --no-bin: ~/.local/bin에 python3.12.exe를 만들지 않음 (이미 있는 사용자 파이썬 바로가기를 건드리지 않게).
function Find-ManagedPython312 {
    $dir = (uv python dir 2>$null | Select-Object -Last 1)
    if (-not $dir -or -not (Test-Path $dir)) { return $null }
    $best = Get-ChildItem $dir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^cpython-3\.12\.\d+-windows-[^-]+-none$' -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -and (Test-Path (Join-Path $_.FullName "python.exe")) } |
        Sort-Object { [int]($_.Name -replace '^cpython-3\.12\.(\d+)-.*$', '$1') } -Descending | Select-Object -First 1
    if ($best) { return (Join-Path $best.FullName "python.exe") }
    return $null
}
uv python install 3.12 --no-bin   # 정션 확인 단계에서 실패해도(448) 파이썬 자체는 받아져 있을 수 있어 종료 코드는 보지 않는다
$py = Find-ManagedPython312
if ($py) {
    Write-Host "파이썬: $py" -ForegroundColor Cyan
    uv tool install --python "$py" --force "$pkg"
} else {
    uv tool install --python-preference managed --python 3.12 --force "$pkg"
}
if ($LASTEXITCODE -ne 0) {
    # 파이썬 3.12 다운로드가 막힌 경우(회사망 등) — 이 PC에 이미 있는 파이썬 3.11~3.13으로 다시 시도
    Write-Host "파이썬 3.12로 설치하지 못했습니다. 이 PC에 있는 파이썬(3.11~3.13)으로 다시 시도합니다..." -ForegroundColor Yellow
    uv tool install --python-preference system --python ">=3.11,<3.14" --force "$pkg"
    Check "ktrade 설치(uv tool install)"
}
if (-not $NoShellUpdate) { uv tool update-shell | Out-Null }
$bin = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { "$env:USERPROFILE\.local\bin" }
$env:Path = "$bin;$env:Path"
if (-not (Test-Path (Join-Path $bin "ktrade.exe"))) { throw "ktrade 설치가 끝났지만 $bin\ktrade.exe 가 없습니다." }

ktrade init
Check "ktrade init"
if (-not $NoShortcut) {
    Write-Host ""
    ktrade shortcut install
}
Write-Host ""
ktrade doctor --offline
Write-Host ""
Write-Host "다음 단계" -ForegroundColor Green
Write-Host "  1. ktrade dashboard --open           (대시보드: 키움 로그인 또는 키 없이 '체험 모드')"
Write-Host "  2. ktrade doctor                     (키움 연결 확인, 조회만 — 명령창에서는 ktrade login 으로 키 입력)"
Write-Host "  3. config.yaml 의 collect 에 받을 종목·기간 → ktrade collect --dry-run → ktrade collect"
Write-Host "  4. ktrade new my_condition → strategy.py 작성 → ktrade test my_condition"
Write-Host "  5. ktrade docs                       (작성 가이드·AI용 규격)"
if (-not $NoShortcut) { Write-Host "바탕화면의 'ktrade 명령창'을 열면 바로 ktrade 명령을 쓸 수 있고, 'ktrade 대시보드'는 더블클릭으로 열립니다." }
Write-Host "새 터미널을 열어야 ktrade 명령이 잡힐 수 있습니다."
