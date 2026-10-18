#!/usr/bin/env sh
# ktrade 설치 (macOS / Linux)
#
#   sh install.sh                                  # 최신 릴리스
#   sh install.sh ./ktrade-<버전>-py3-none-any.whl  # 받은 wheel 파일로
#   sh install.sh git+https://github.com/Re-qwer3618/ktrade-public   # 개발 중인 최신 코드 (git 필요)
#   NO_DASHBOARD=1 NO_SHORTCUT=1 sh install.sh     # 대시보드·바로가기 없이 (명령만)
#
# uv(파이썬 도구 관리자)가 없으면 공식 설치 스크립트로 설치 → ktrade 설치(자체 파이썬 3.12, 대시보드 포함) → ktrade init
# → 바로가기(macOS 바탕화면 .command / Linux 앱 메뉴 .desktop) → doctor.
set -e
REPO="Re-qwer3618/ktrade-public"
SRC="${1:-}"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv가 없어 설치합니다 (https://astral.sh/uv)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ -z "$SRC" ]; then
  echo "최신 릴리스 확인: https://github.com/$REPO/releases"
  SRC=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | grep -o '"browser_download_url": *"[^"]*\.whl"' | head -n 1 | sed 's/.*"\(http[^"]*\)"/\1/')
  [ -n "$SRC" ] || { echo "최신 릴리스에서 wheel 파일을 찾지 못했습니다"; exit 1; }
fi
PKG="$SRC"
if [ -z "$NO_DASHBOARD" ]; then
  case "$SRC" in git+*|http*) PKG="ktrade[dashboard] @ $SRC" ;; *) PKG="$SRC[dashboard]" ;; esac
fi
echo "ktrade 설치: $PKG"
# uv 설정이 파이썬 다운로드를 막아 둔 PC("Python downloads are set to 'never'")에서도 되도록 이 설치에서만 허용
export UV_PYTHON_DOWNLOADS=automatic
uv tool install --python 3.12 --force "$PKG" || {
  echo "파이썬 3.12로 설치하지 못했습니다. 이 PC에 있는 파이썬(3.11~3.13)으로 다시 시도합니다..."
  uv tool install --python ">=3.11,<3.14" --force "$PKG"
}
export PATH="$HOME/.local/bin:$PATH"
ktrade init
[ -n "$NO_SHORTCUT" ] || ktrade shortcut install || true
ktrade doctor --offline || true
cat <<'EOT'

다음 단계
  1. ktrade dashboard --open           (대시보드: 키움 로그인 또는 키 없이 '체험 모드')
  2. ktrade doctor                     (키움 연결 확인, 조회만 — 명령창에서는 ktrade login 으로 키 입력)
  3. config.yaml 의 collect 에 받을 종목·기간 → ktrade collect --dry-run → ktrade collect
  4. ktrade new my_condition → strategy.py 작성 → ktrade test my_condition
  5. ktrade docs                       (작성 가이드·AI용 규격)
EOT
