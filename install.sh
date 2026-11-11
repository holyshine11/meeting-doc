#!/bin/sh
# BLUE NOTE 회의록 도구 설치 (macOS, Linux)
#   curl -fsSL https://raw.githubusercontent.com/holyshine11/meeting-doc/main/install.sh | sh
# 다시 실행하면 최신 코드로 업데이트한다. 회의 기록(data/), 받아 둔 모델(models/), config.toml은 그대로 둔다.
set -eu

REPO="${MEETING_DOC_REPO:-holyshine11/meeting-doc}"
BRANCH="${MEETING_DOC_BRANCH:-main}"
DIR="${MEETING_DOC_DIR:-$HOME/meeting-doc}"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
fail() { printf '\n\033[1;31m설치를 멈춥니다.\033[0m %s\n' "$1" >&2; exit 1; }

# 1. 이 PC에서 쓸 수 있는지 먼저 본다. 음성 인식에 쓰는 onnxruntime이 아래 환경만 지원한다.
say "PC 환경 확인"
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Darwin)
    [ "$ARCH" = "arm64" ] || fail "Apple Silicon(M1 이상) Mac에서만 쓸 수 있습니다. 이 Mac은 Intel($ARCH)입니다."
    MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
    [ "$MAJOR" -ge 14 ] || fail "macOS 14(Sonoma) 이상이 필요합니다. 지금은 $(sw_vers -productVersion)입니다. 시스템 설정 > 일반 > 소프트웨어 업데이트에서 올려 주세요."
    ;;
  Linux)
    case "$ARCH" in x86_64|aarch64) ;; *) fail "지원하지 않는 CPU입니다: $ARCH" ;; esac
    ;;
  *) fail "Windows는 PowerShell에서 README의 Windows 설치 명령을 쓰세요." ;;
esac
case "$DIR" in *" "*) fail "설치 경로에 공백이 있습니다: $DIR  MEETING_DOC_DIR=/공백없는/경로 를 앞에 붙여 다시 실행하세요." ;; esac
command -v curl >/dev/null 2>&1 || fail "curl이 없습니다."
echo "$OS $ARCH, 설치 위치 : $DIR"

# 2. uv: 파이썬과 필요한 라이브러리를 이 프로젝트 안에만 깔아 준다. 이미 쓰던 파이썬은 건드리지 않는다.
say "uv 확인"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv가 없어 설치합니다."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  NEW_UV=1
fi
command -v uv >/dev/null 2>&1 || fail "uv 설치에 실패했습니다. 터미널을 새로 열고 다시 실행해 보세요."
uv --version

# 3. 코드 받기. git이 있으면 git으로, 없으면 압축 파일로 받는다.
#    macOS의 git은 개발 도구가 없으면 설치 창을 띄우므로 xcode-select로 먼저 확인한다.
say "코드 받기"
has_git() {
  command -v git >/dev/null 2>&1 || return 1
  [ "$OS" != "Darwin" ] || xcode-select -p >/dev/null 2>&1
}
if [ -d "$DIR/.git" ] && has_git; then
  git -C "$DIR" pull --ff-only || fail "$DIR 에서 고친 파일이 있어 업데이트하지 못했습니다. 'git -C $DIR status'로 확인하세요."
elif [ ! -e "$DIR" ] && has_git; then
  git clone --depth 1 -b "$BRANCH" "https://github.com/$REPO.git" "$DIR"
else
  TMP="$(mktemp -d)"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$TMP/src.tgz" || fail "코드를 내려받지 못했습니다. 인터넷 연결을 확인하세요."
  mkdir -p "$DIR"
  if [ -f "$DIR/config.toml" ]; then
    tar -xzf "$TMP/src.tgz" -C "$DIR" --strip-components 1 --exclude '*/config.toml'
  else
    tar -xzf "$TMP/src.tgz" -C "$DIR" --strip-components 1
  fi
  rm -rf "$TMP"
fi

# 4. 파이썬과 라이브러리 설치. .python-version에 적힌 버전을 uv가 알아서 받는다.
say "파이썬과 라이브러리 설치 (처음에는 몇 분 걸립니다)"
cd "$DIR"
uv sync || fail "라이브러리 설치에 실패했습니다. 인터넷 연결을 확인하고 같은 명령을 다시 실행하세요."
uv run python -c "import faster_whisper, sherpa_onnx, av, docx" || fail "라이브러리 확인에 실패했습니다. 'cd $DIR && uv sync --reinstall' 후 다시 실행하세요."

# 5. Ollama(회의록을 쓰는 AI 실행기). 없으면 화면에서 다시 안내하므로 여기서 실패해도 멈추지 않는다.
say "Ollama 확인"
if command -v ollama >/dev/null 2>&1 || [ -d /Applications/Ollama.app ]; then
  echo "Ollama가 이미 있습니다."
elif [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
  brew install --cask ollama-app || echo "Ollama를 설치하지 못했습니다. 프로그램을 켜면 화면에서 설치 방법을 다시 안내합니다."
else
  echo "Ollama가 없습니다. 프로그램을 켜면 화면에서 설치 방법을 안내합니다."
fi

say "설치 완료"
[ -z "${NEW_UV:-}" ] || echo "uv를 새로 설치했습니다. 이 터미널을 닫고 새 터미널을 여세요."
cat <<EOF

  실행하는 법 (매번 이렇게 켭니다)
    cd $DIR
    uv run python app.py

  브라우저가 자동으로 열립니다. 처음에는 화면 안내를 따라 AI 모델을 받으세요.
EOF
