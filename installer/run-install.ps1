# ktrade-setup.exe가 부르는 설치 래퍼 — scripts\install.ps1을 실행하고 로그를 남긴 뒤 성공 여부를 종료 코드로 알린다.
#   0 = 성공, 1 = 실패 (로그: %LOCALAPPDATA%\ktrade-setup\install.log)

param(
    [switch]$NoDashboard,
    [switch]$NoShortcut,
    [switch]$Silent           # 무인 설치(/VERYSILENT): 실패해도 Enter를 기다리지 않음
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $env:LOCALAPPDATA "ktrade-setup"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "install.log"
try { $Host.UI.RawUI.WindowTitle = "ktrade 설치 중... (창을 닫지 마세요)" } catch {}
Start-Transcript -Path $log -Force | Out-Null

$ok = $false
try {
    $opts = @{}
    if ($NoDashboard) { $opts.NoDashboard = $true }
    if ($NoShortcut) { $opts.NoShortcut = $true }
    & (Join-Path $here "install.ps1") @opts
    $bin = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { "$env:USERPROFILE\.local\bin" }
    if (-not (Test-Path (Join-Path $bin "ktrade.exe"))) { throw "설치가 끝났지만 $bin\ktrade.exe 를 찾지 못했습니다." }
    $ok = $true
} catch {
    Write-Host ""
    Write-Host "설치 실패: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "위에 나온 빨간 error 줄이 실제 원인입니다. 흔한 경우:" -ForegroundColor Yellow
    Write-Host "  - 인터넷 연결이 없거나 회사망이 github.com / astral.sh 다운로드를 막음" -ForegroundColor Yellow
    Write-Host "  - 백신이 uv.exe 또는 %USERPROFILE%\.local\bin 을 막음" -ForegroundColor Yellow
    Write-Host "로그: $log  (문의할 때 이 파일을 함께 보내 주세요)" -ForegroundColor Yellow
}
Stop-Transcript | Out-Null

if ($ok) {
    Write-Host ""
    Write-Host "설치 완료! 이 창은 잠시 후 닫힙니다." -ForegroundColor Green
    Start-Sleep -Seconds 3
    exit 0
}
if (-not $Silent) { Read-Host "Enter를 누르면 창을 닫습니다" | Out-Null }
exit 1
