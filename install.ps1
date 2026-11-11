# BLUE NOTE 회의록 도구 설치 (Windows)
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/holyshine11/meeting-doc/main/install.ps1 | iex"
# 다시 실행하면 최신 코드로 업데이트한다. 회의 기록(data\), 받아 둔 모델(models\), config.toml은 그대로 둔다.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # 진행 막대가 Invoke-WebRequest를 몇 배 느리게 만든다
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Repo = if ($env:MEETING_DOC_REPO) { $env:MEETING_DOC_REPO } else { 'holyshine11/meeting-doc' }
$Branch = if ($env:MEETING_DOC_BRANCH) { $env:MEETING_DOC_BRANCH } else { 'main' }

function Say($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n설치를 멈춥니다. $text" -ForegroundColor Red; throw $text }

# 1. 이 PC에서 쓸 수 있는지 먼저 본다.
Say 'PC 환경 확인'
if (-not [Environment]::Is64BitOperatingSystem) { Fail '64비트 Windows에서만 쓸 수 있습니다.' }
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { Fail 'ARM 프로세서 Windows는 지원하지 않습니다. x64(Intel, AMD) PC에서 설치하세요.' }

# 설치 경로에 한글이나 공백이 있으면 음성 인식 모델을 읽지 못하는 일이 있다. 그럴 때는 C:\meeting-doc에 둔다.
if ($env:MEETING_DOC_DIR) { $Dir = $env:MEETING_DOC_DIR }
elseif ($env:USERPROFILE -match '^[A-Za-z0-9:\\_.-]+$') { $Dir = Join-Path $env:USERPROFILE 'meeting-doc' }
else { $Dir = 'C:\meeting-doc' }
if ($Dir -notmatch '^[A-Za-z0-9:\\_.-]+$') { Fail "설치 경로에는 영문, 숫자만 쓰세요: $Dir" }
Write-Host "설치 위치 : $Dir"

# 2. uv: 파이썬과 필요한 라이브러리를 이 프로젝트 안에만 깔아 준다. 이미 쓰던 파이썬은 건드리지 않는다.
Say 'uv 확인'
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host 'uv가 없어 설치합니다.'
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
  $NewUv = $true
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv 설치에 실패했습니다. PowerShell을 새로 열고 다시 실행해 보세요.' }
uv --version

# 3. 코드 받기. git이 있으면 git으로, 없으면 zip으로 받는다.
Say '코드 받기'
$HasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if ((Test-Path "$Dir\.git") -and $HasGit) {
  git -C $Dir pull --ff-only
  if ($LASTEXITCODE) { Fail "$Dir 에서 고친 파일이 있어 업데이트하지 못했습니다. 'git -C $Dir status'로 확인하세요." }
} elseif (-not (Test-Path $Dir) -and $HasGit) {
  git clone --depth 1 -b $Branch "https://github.com/$Repo.git" $Dir
  if ($LASTEXITCODE) { Fail '코드를 받지 못했습니다. 인터넷 연결을 확인하세요.' }
} else {
  $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("meeting-doc-" + [guid]::NewGuid())
  New-Item -ItemType Directory $Tmp | Out-Null
  try {
    Invoke-WebRequest "https://codeload.github.com/$Repo/zip/refs/heads/$Branch" -OutFile "$Tmp\src.zip" -UseBasicParsing
  } catch { Fail '코드를 내려받지 못했습니다. 인터넷 연결을 확인하세요.' }
  Expand-Archive "$Tmp\src.zip" $Tmp
  $Src = Get-ChildItem $Tmp -Directory | Select-Object -First 1
  New-Item -ItemType Directory -Force $Dir | Out-Null
  if (Test-Path "$Dir\config.toml") { Remove-Item "$($Src.FullName)\config.toml" }  # 고쳐 둔 설정을 지키기
  Copy-Item "$($Src.FullName)\*" $Dir -Recurse -Force
  Remove-Item $Tmp -Recurse -Force
}

# 4. 파이썬과 라이브러리 설치. .python-version에 적힌 버전을 uv가 알아서 받는다.
Say '파이썬과 라이브러리 설치 (처음에는 몇 분 걸립니다)'
Set-Location $Dir
uv sync
if ($LASTEXITCODE) { Fail '라이브러리 설치에 실패했습니다. 인터넷 연결을 확인하고 같은 명령을 다시 실행하세요.' }
uv run python -c "import faster_whisper, sherpa_onnx, av, docx"
if ($LASTEXITCODE) {
  Fail ("라이브러리 확인에 실패했습니다. 'Microsoft Visual C++ 재배포 가능 패키지'를 설치한 뒤 다시 실행하세요: " +
        'https://aka.ms/vs/17/release/vc_redist.x64.exe')
}

# 5. Ollama(회의록을 쓰는 AI 실행기). 없으면 화면에서 다시 안내하므로 여기서 실패해도 멈추지 않는다.
Say 'Ollama 확인'
$OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if ((Get-Command ollama -ErrorAction SilentlyContinue) -or (Test-Path $OllamaExe)) {
  Write-Host 'Ollama가 이미 있습니다.'
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
  if ($LASTEXITCODE) { Write-Host 'Ollama를 설치하지 못했습니다. 프로그램을 켜면 화면에서 설치 방법을 다시 안내합니다.' }
} else {
  Write-Host 'Ollama가 없습니다. 프로그램을 켜면 화면에서 설치 방법을 안내합니다.'
}

Say '설치 완료'
if ($NewUv) { Write-Host 'uv를 새로 설치했습니다. 이 PowerShell 창을 닫고 새 창을 여세요.' }
Write-Host @"

  실행하는 법 (매번 이렇게 켭니다)
    cd $Dir
    uv run python app.py

  브라우저가 자동으로 열립니다. 처음에는 화면 안내를 따라 AI 모델을 받으세요.
"@
