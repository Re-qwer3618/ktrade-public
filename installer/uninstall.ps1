# ktrade-setup.exe 제거 시 실행 — 자동 실행·바로가기·프로그램을 지운다.
# 설정·키·데이터·내 매매조건(%USERPROFILE%\.ktrade)과 uv는 그대로 둔다.

$ErrorActionPreference = "Continue"
$bin = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { "$env:USERPROFILE\.local\bin" }
$env:Path = "$bin;$env:Path"

if (Get-Command ktrade -ErrorAction SilentlyContinue) {
    ktrade schedule remove
    ktrade shortcut remove
}
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv tool uninstall ktrade
}
exit 0
