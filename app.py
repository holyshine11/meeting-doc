"""meeting-doc 서버. 실행: uv run python app.py"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import tomllib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from calendar import monthrange
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
CFG = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
HOST, PORT = CFG["server"]["host"], CFG["server"]["port"]
LIST = CFG["list"]
DATA = (ROOT / CFG["paths"]["data_dir"]).resolve()
MODELS = (ROOT / CFG["paths"]["models_dir"]).resolve()
# 모델뿐 아니라 다운로드 캐시와 로그도 프로젝트 폴더에 둔다. 기본 위치(~/.cache/huggingface)가
# 쓸 수 없는 경로이면 다운로드가 "File exists (os error 17)"로 실패한다.
os.environ.setdefault("HF_HOME", str(MODELS))
MAX_UPLOAD = CFG["upload"]["max_mb"] * 1024 * 1024
CHUNK = CFG["upload"]["chunk_bytes"]

STATUS_RECORDING, STATUS_PROCESSING, STATUS_DONE, STATUS_ERROR = "recording", "processing", "done", "error"
# 폴더 이름에 쓸 수 없는 글자(Windows 기준 포함)와 제어 문자
UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FOLDER_TITLE_MAX = 60
RATE = 16000  # Whisper와 화자 구분 모델이 받는 샘플레이트
AUDIO_NAME = "audio.ogg"
CLEAN_NAME = "audio-clean.ogg"  # 소음을 줄인 듣기용 사본. 받아쓰기와 화자 구분은 원래 녹음으로 한다.
PART_GLOB = "part-*.ogg"  # 이어서 녹음한 조각. 처리할 때 audio.ogg 뒤에 붙이고 지운다.
TEXT_NAME = "회의 내용.txt"  # 사람이 읽는 회의 내용
MINUTES_NAME = "회의록.md"  # LLM이 쓴 요약과 회의록
NOTE_TAGS = ("논의", "결정", "할 일", "숫자", "미결")
NOTE_PROMPT = (
    "다음은 회의를 자동으로 받아쓴 내용의 일부다. 받아쓰기 오류가 많고 화자 구분도 정확하지 않다. "
    "[시:분] 줄은 그 아래 발언이 나온 시각이다. 문맥으로 뜻을 바로잡아 읽고 아래 분류마다 한국어 메모를 써라.\n"
    "- 한 항목은 한 문장이다. '~함', '~예정', '~필요'처럼 끝내고, 끝에 그 말이 나온 시각 하나를 (시:분)으로 붙인다.\n"
    "- 누가 말했는지가 아니라 무슨 내용이 오갔는지를 쓴다. 사람 이름은 [할 일]의 담당에만 쓴다. "
    "'참석자 3' 같은 번호는 이름이 아니므로 쓰지 않는다.\n"
    "- 뜻을 알 수 없는 낱말은 그대로 옮기지 말고 그 항목을 뺀다.\n"
    "- 인사, 잡담, 맞장구, 되풀이한 말은 뺀다. 받아쓴 내용에 없는 것은 지어내지 않는다.\n"
    "[논의] 안건별로 오간 내용\n"
    "[결정] 참석자들이 합의하거나 확정한 것만. 한 사람의 제안이나 의견은 [논의]에 둔다\n"
    "[할 일] 누가 무엇을 언제까지 하기로 했는지\n"
    "[숫자] 금액, 수량, 날짜, 기간. 받아쓴 표기 그대로 적는다. 말하지 않은 연도, 달, 단위를 채워 넣지 않는다. "
    "단위가 분명하지 않으면 '단위 확인 필요'를 붙인다\n"
    "[미결] 결론이 안 났거나 따로 확인하기로 한 것"
)
# 회의록은 한 번에 다 쓰게 하지 않는다. 작은 모델은 메모를 그대로 이어 붙이므로 부분마다 따로 묻고 항목 수는 코드에서 자른다.
STYLE = (
    "\n쓰는 법\n"
    "- 모든 항목은 '- '로 시작하는 한 문장이다. '~함', '~하기로 함', '~예정', '~필요'처럼 끝내고 마침표는 찍지 않는다.\n"
    "- 사람 이름을 주어로 쓰지 않는다. 누가 말했는지가 아니라 무엇이 논의됐는지를 쓴다.\n"
    "- 한 항목에는 한 가지 내용만 쓰고 60자를 넘기지 않는다. 여러 내용을 '및'으로 이어 붙이지 않는다.\n"
    "- '~을 논의함', '~에 대해 이야기함'으로 끝내지 않는다. 어떤 요청, 문제, 방안이 나왔는지 내용 자체를 쓴다. "
    "나쁜 예는 '교육 일정 변경에 대해 논의함', 좋은 예는 '강사 사정으로 교육을 한 주 미루자는 요청이 나옴'이다.\n"
    "- 항목을 '주제 : 설명' 꼴로 시작하지 않는다. 괄호 안에 영어를 덧붙이지 않는다. 굵은 글씨를 쓰지 않는다.\n"
    "- 메모에 없는 내용은 쓰지 않는다. 금액, 기간, 수량은 메모의 숫자 그대로 쓴다.\n"
    "- 항목 끝에는 메모에 있던 시각 하나를 (시:분)으로 남긴다.\n"
    "- 설명이나 인사말 없이 항목만 출력한다."
)
OUTLINE_PROMPT = (
    "다음은 한 회의의 메모다. 이 회의에서 다룬 안건을 {low}~{high}개로 묶어 안건 이름만 한 줄에 하나씩 써라. "
    "이름은 15자 안팎의 명사구로 쓰고, 서로 겹치지 않게, 회의에서 다룬 순서대로 쓴다. 번호, 기호, 설명 없이 이름만 출력한다."
)
AGENDA_PROMPT = (
    "다음은 한 회의의 메모다. 이 회의의 안건은 {names}이다. 이 가운데 '{name}' 안건에 해당하는 내용만 골라 "
    "중요한 순서로 2~{limit}개 항목으로 간추려라. 비슷한 내용은 한 항목으로 합치고 자잘한 것은 버린다. "
    "다른 안건에 더 어울리는 내용은 쓰지 않는다. 이 안건에서 정해진 것이 있으면 함께 쓴다. 해당하는 내용이 없으면 '없음'이라고만 쓴다." + STYLE
)
DECISION_PROMPT = (
    "다음은 한 회의에서 결정됐다고 적어 둔 메모다. 참석자들이 합의하거나 확정한 것만 남겨 다시 써라. "
    "같은 결정은 하나로 합친다. 한 사람의 제안, 의견, 계획, 예상은 뺀다. 남는 것이 없으면 '- 없음'이라고 쓴다." + STYLE
)
TODO_PROMPT = (
    "다음은 한 회의에서 나온 할 일 메모다. 회의 뒤에 누군가 실제로 해야 하는 일만 골라 Markdown 표로 써라. "
    "머리행은 '| 할 일 | 담당 | 기한 | 시각 |'이다. 같은 일은 하나로 합친다. "
    "할 일은 '과제 우선순위 정리 후 전달'처럼 짧은 명사형으로 쓴다. 나중에 이야기하자는 식의 막연한 약속과 '가이드에 따른 이행'처럼 무엇을 하는지 드러나지 않는 일은 뺀다. "
    "담당은 메모에 이름이 나온 사람만 쓰고 여러 명이면 쉼표로 나열하며 모르면 '미정'이다. "
    "기한은 회의 날짜를 기준으로 날짜를 셈해 'YYYY-MM-DD'로, 기간이면 'YYYY-MM-DD ~ YYYY-MM-DD'로 쓴다. "
    "기한이 안 나왔거나 날짜로 바꿀 수 없으면 '미정'이다. 시각은 메모의 시:분이다. "
    "설명 없이 표만 출력한다."
)
OPEN_PROMPT = (
    "다음은 한 회의에서 결론이 안 났거나 따로 확인하기로 한 것을 적어 둔 메모다. 같은 것은 하나로 합쳐 다시 써라. "
    "무엇이 정해지지 않았는지 또는 무엇을 확인해야 하는지가 드러나게 쓴다. 남는 것이 없으면 '- 없음'이라고 쓴다." + STYLE
)
SUMMARY_PROMPT = (
    "다음은 한 회의의 안건별 논의와 결정 사항이다. 회의에 오지 않은 사람이 이것만 읽어도 무엇을 다뤘고 "
    "무엇이 정해졌는지 알 수 있게 3~{limit}개 항목으로 요약하라. 금액과 기간처럼 중요한 숫자는 넣는다. 시각은 붙이지 않는다."
    + STYLE.replace("- 항목 끝에는 메모에 있던 시각 하나를 (시:분)으로 남긴다.\n", "")
)
MINUTES_HEADS = ("## 회의 요약 내용", "## 안건별 논의", "## 결정 사항", "## 실행 항목", "## 미결 및 확인 필요")
TAG_RE = re.compile(r"^[\s*#-]*\[(" + "|".join(NOTE_TAGS) + r")\]")
COLON_RE = re.compile(r"(?<=[가-힣)])[ \t]*:[ \t]*")  # 한글 뒤 쌍점. 시각(10:30)은 숫자 뒤라 해당 없다
ENGLISH_PAREN_RE = re.compile(r"(?<=[가-힣])\s*\([A-Za-z][A-Za-z .&/-]*\)")  # 한글 뒤에 덧붙인 (English)
TIMES_RE = re.compile(r"\s*\(\d{1,2}:\d\d(?:[,\s]+\d{1,2}:\d\d)*\)")  # 항목 끝의 (시:분). 화면의 TIMES와 같은 꼴
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # Word 문서에 넣을 수 없는 제어 문자
EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
TITLE_MAX = 100
ROW_MAX = 2000  # 회의 내용 한 줄을 고칠 때 받을 수 있는 글자 수
# 브라우저가 다른 사이트에서 이 서버로 요청을 보내거나 DNS rebinding으로 접근하는 것을 막는다.
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
LOCAL_ORIGINS = {f"http://{h}" for h in LOCAL_HOSTS}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# 전사와 화자 구분은 한 번에 하나. 동시에 여러 건을 돌려야 하면 worker 수를 설정으로 뺀다.
jobs = ThreadPoolExecutor(max_workers=1)
_model = None
_embedder = None
# 화면에서 오는 수정(이름, 합치기, 문장 고치기)은 한 번에 하나만. 둘이 같은 파일을 읽고 덮어써 한쪽이 사라지는 것을 막는다.
edit_lock = threading.Lock()
_model_lock = threading.Lock()


def is_local(method: str, headers) -> bool:
    origin = headers.get("origin")
    if headers.get("host") not in LOCAL_HOSTS or headers.get("sec-fetch-site", "none") not in ("same-origin", "none"):
        return False  # 다른 사이트에 심은 <audio>나 <img>로 회의 파일을 불러가는 것도 막는다
    # 읽기 요청은 Origin이 없을 수 있다. 바꾸는 요청은 우리 화면에서 온 것만 받는다.
    return origin in LOCAL_ORIGINS or (origin is None and method in ("GET", "HEAD"))


@app.middleware("http")
async def local_only(request: Request, call_next):
    if not is_local(request.method, request.headers):
        return JSONResponse({"detail": "로컬 요청만 허용합니다."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            stt = CFG["stt"]
            _model = WhisperModel(
                stt["model"], device=stt["device"], compute_type=stt["compute_type"], download_root=str(MODELS)
            )
        return _model


def new_id(title: str, day: str | None = None, keep: str = "") -> str:
    """회의 폴더 이름을 정한다. 예: 20261111-주간 회의. 같은 이름이 있으면 뒤에 -2, -3을 붙인다. keep은 지금 쓰는 폴더 이름이라 겹쳐도 된다."""
    safe = " ".join(UNSAFE_RE.sub(" ", title).split())[:FOLDER_TITLE_MAX].strip(" .") or "회의"
    base = f"{day or datetime.now().strftime('%Y%m%d')}-{safe}"
    name, n = base, 1
    while name != keep and (DATA / name).exists():
        n += 1
        name = f"{base}-{n}"
    return name


def is_meeting(mid: str) -> bool:
    # data 폴더에 실제로 있는 폴더 이름과 같을 때만 회의로 본다. 경로 조각이 섞인 값은 여기서 걸러진다.
    return mid in os.listdir(DATA) and (DATA / mid / "meta.json").is_file()


def meeting_dir(mid: str) -> Path:
    if not is_meeting(mid):
        raise HTTPException(404, "회의를 찾을 수 없습니다.")
    return DATA / mid


def read_meta(d: Path) -> dict:
    # id는 언제나 폴더 이름이다. Finder에서 폴더 이름을 바꿔도 그대로 열린다.
    return {**json.loads((d / "meta.json").read_text(encoding="utf-8")), "id": d.name}


def write_meta(d: Path, meta: dict) -> None:
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / "meta.json")  # 쓰는 도중 죽어도 meta.json이 반쪽으로 남지 않는다


def read_transcript(d: Path) -> list[dict]:
    f = d / "transcript.jsonl"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.is_file() else []


def write_rows(d: Path, rows: list[dict]) -> None:
    tmp = d / "transcript.jsonl.tmp"
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, d / "transcript.jsonl")


def speaker_name(meta: dict, row: dict) -> str:
    if not row.get("speaker"):
        return "화자 미확인"
    return meta.get("speakers", {}).get(str(row["speaker"]), f"참석자 {row['speaker']}")


def write_text(d: Path, meta: dict) -> None:
    """사람이 바로 읽을 수 있는 회의 내용 파일을 만든다. 내용이나 참석자 이름이 바뀔 때마다 다시 쓴다."""
    lines = [meta["title"], meta["created"].replace("T", " "), ""]
    for r in read_transcript(d):
        t = int(r["start"])
        lines.append(f"[{t // 3600:02}:{t // 60 % 60:02}:{t % 60:02}] {speaker_name(meta, r)}: {r['text']}")
    tmp = d / (TEXT_NAME + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, d / TEXT_NAME)


def open_folder(d: Path) -> None:
    """회의 폴더를 Finder(Windows는 탐색기)로 연다. 경로는 서버가 정한 회의 폴더뿐이다."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(d)], check=True)
    elif sys.platform == "win32":
        os.startfile(d)
    else:
        subprocess.run(["xdg-open", str(d)], check=True)


class OpusWriter:
    """16kHz 모노 float32 조각을 받아 Opus 파일에 이어 쓴다. 원본보다 훨씬 작다(24kbps면 1시간에 약 11MB)."""

    def __init__(self, path: Path):
        import av

        self.av = av
        self.out = av.open(str(path), "w", format="ogg")
        self.stream = self.out.add_stream("libopus", rate=RATE, layout="mono")
        self.stream.bit_rate = CFG["audio"]["bitrate"]
        self.samples = 0

    def write(self, samples) -> None:
        import numpy as np

        frame = self.av.AudioFrame.from_ndarray(np.ascontiguousarray(samples[None, :]), format="flt", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.samples
        self.samples += len(samples)
        self.out.mux(self.stream.encode(frame))

    def close(self) -> None:
        self.out.mux(self.stream.encode(None))
        self.out.close()


def write_opus(samples, path: Path) -> None:
    writer = OpusWriter(path)
    for i in range(0, len(samples), RATE):
        writer.write(samples[i : i + RATE])
    writer.close()


def fetch(url: str, sha256: str, dest: Path, member: str | None = None) -> Path:
    """모델 파일을 한 번만 받는다. 해시가 설정값과 다르면 쓰지 않는다."""
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url) as res, tmp.open("wb") as out:
        shutil.copyfileobj(res, out)
    if hashlib.sha256(tmp.read_bytes()).hexdigest() != sha256:
        tmp.unlink()
        raise RuntimeError(f"모델 파일 검증 실패: {url}")
    if member:  # tar 묶음에서 모델 파일 하나만 꺼낸다
        with tarfile.open(tmp) as tar:
            data = tar.extractfile(member).read()
        tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest


def find_turns(samples, count: int | None, on_progress=None) -> list[tuple]:
    """누가 언제 말했는지 (시작, 끝, 화자번호) 목록으로 돌려준다. count가 없으면 화자 수를 자동으로 정한다."""
    import sherpa_onnx

    c = CFG["diarization"]
    folder = MODELS / "diarization"
    seg = fetch(c["segmentation_url"], c["segmentation_sha256"], folder / "segmentation.onnx", c["segmentation_member"])
    emb = fetch(c["embedding_url"], c["embedding_sha256"], folder / "embedding.onnx")
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg))
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=count or -1, threshold=c["threshold"]),
        min_duration_on=c["min_duration_on"],
        min_duration_off=c["min_duration_off"],
    )
    if not cfg.validate():
        raise RuntimeError("화자 구분 설정이 올바르지 않습니다.")
    callback = (lambda done, total: on_progress(done / total) or 0) if on_progress else None
    result = sherpa_onnx.OfflineSpeakerDiarization(cfg).process(samples, callback=callback)
    return [(t.start, t.end, t.speaker) for t in result.sort_by_start_time()]


def embed_voice(samples):
    """목소리 특징 벡터(길이 1)를 돌려준다."""
    import numpy as np
    import sherpa_onnx

    global _embedder
    if _embedder is None:
        c = CFG["diarization"]
        emb = fetch(c["embedding_url"], c["embedding_sha256"], MODELS / "diarization" / "embedding.onnx")
        _embedder = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb)))
    stream = _embedder.create_stream()
    stream.accept_waveform(sample_rate=RATE, waveform=samples)
    stream.input_finished()
    v = np.array(_embedder.compute(stream))
    return v / np.linalg.norm(v)


def merge_turns(samples, turns: list[tuple], count: int | None) -> list[tuple]:
    """같은 사람이 여러 화자 번호로 쪼개진 것을 목소리 특징으로 다시 합친다.

    화자 구분 모델은 긴 회의에서 한 사람을 수십 개 번호로 나누곤 한다. 번호마다 목소리 특징의 평균을 내고,
    평균끼리 가까운 번호를 가까운 순서대로 합친다. count가 있으면 그 수 이하가 될 때까지 더 합친다.
    조금만 말한 번호는 닮은 사람이 있으면 그 사람에게 붙이고, 없으면 따로 둔다(회의에서 몇 마디만 한 참석자).
    특징을 잴 만큼 길게 말한 적이 없는 번호는 화자 미확인(None)으로 둔다.
    """
    import numpy as np

    c = CFG["diarization"]
    sums, dur = {}, {}
    for start, end, sp in turns:
        dur[sp] = dur.get(sp, 0) + end - start
        cut = min(end, start + c["embed_max_sec"])
        if cut - start >= c["embed_min_sec"]:
            # 길게 말한 구간일수록 특징이 믿을 만하므로 길이만큼 무게를 준다.
            sums[sp] = sums.get(sp, 0) + embed_voice(samples[int(start * RATE) : int(cut * RATE)]) * (cut - start)
    alias = {sp: None for sp in dur if sp not in sums}  # 원래 번호 -> 합쳐 들어간 번호

    def distance(a, b) -> float:
        return 1 - float(sums[a] @ sums[b]) / float(np.linalg.norm(sums[a]) * np.linalg.norm(sums[b]))

    def absorb(a, b) -> None:
        sums[a] = sums[a] + sums.pop(b)
        dur[a] += dur.pop(b)
        alias[b] = a

    # 합칠 때마다 모든 쌍의 거리를 다시 잰다. 번호가 수백 개를 넘어 느리면 거리 행렬을 고쳐 쓰게 바꾼다.
    def closest() -> tuple:
        return min((distance(a, b), a, b) for a in sums for b in sums if a < b)

    while len(sums) > 1 and (near := closest())[0] < c["merge_threshold"]:
        absorb(*sorted(near[1:], key=dur.get, reverse=True))
    big = [sp for sp in sums if dur[sp] >= c["min_speaker_sec"]] or list(sums)
    for sp in [sp for sp in sums if sp not in big]:
        near = min((distance(sp, b), b) for b in big)
        if near[0] < c["small_merge_threshold"]:  # 닮은 사람이 없으면 조금만 말한 다른 참석자로 보고 그대로 둔다
            absorb(near[1], sp)
    while count and len(sums) > count:
        # 수를 맞출 때는 조금만 말한 번호부터 가장 닮은 사람에게 붙인다. 그래도 많으면 가까운 쌍을 합친다.
        small = min(sums, key=dur.get)
        if dur[small] < c["min_speaker_sec"]:
            absorb(min((distance(small, b), b) for b in sums if b != small)[1], small)
        else:
            absorb(*sorted(closest()[1:], key=dur.get, reverse=True))

    def final(sp):
        while sp in alias:
            sp = alias[sp]
        return sp

    return [(start, end, final(sp)) for start, end, sp in turns]


def denoise(samples):
    """공간 소음을 줄인 소리를 돌려준다. 길이는 그대로다."""
    import numpy as np
    import sherpa_onnx

    c = CFG["denoise"]
    model = fetch(c["model_url"], c["model_sha256"], MODELS / "denoise" / "gtcrn.onnx")
    cfg = sherpa_onnx.OfflineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=str(model)))
    )
    if not cfg.validate():
        raise RuntimeError("소음 제거 설정이 올바르지 않습니다.")
    # 웅웅거리는 저음은 말소리 아래 대역이라 먼저 걸러 낸다(4차 버터워스 꼴의 완만한 차단).
    # 녹음 전체를 한 번에 FFT 한다. 몇 시간짜리에서 메모리가 모자라면 구간별 필터로 바꾼다.
    freq = np.fft.rfftfreq(len(samples), 1 / RATE)
    out = np.fft.irfft(np.fft.rfft(samples) / np.sqrt(1 + (c["highpass_hz"] / np.maximum(freq, 1e-3)) ** 8), len(samples)).astype(np.float32)
    denoiser = sherpa_onnx.OfflineSpeechDenoiser(cfg)
    for _ in range(c["passes"]):
        got = np.asarray(denoiser.run(out, RATE).samples, dtype=np.float32)
        # 모델이 끝을 조금 잘라 돌려준다. 재생 시각이 회의 내용과 어긋나지 않게 길이를 맞춘다.
        out = np.pad(got, (0, max(len(samples) - len(got), 0)))[: len(samples)]
    return out


def write_clean(d: Path, meta: dict, samples) -> None:
    """듣기용으로 소음을 줄인 사본을 만든다. 실패해도 회의 처리는 실패로 치지 않는다."""
    if not CFG["denoise"]["enabled"] or (d / CLEAN_NAME).is_file():
        return
    meta.update(stage="듣기용 소음 줄이는 중", progress=0)
    write_meta(d, meta)
    try:
        tmp = d / "audio-clean.tmp"
        write_opus(denoise(samples), tmp)
        os.replace(tmp, d / CLEAN_NAME)
        meta.pop("denoise_error", None)
    except Exception as e:
        meta["denoise_error"] = f"소음을 줄인 사본을 만들지 못했습니다. ({type(e).__name__}: {e})"


def assign_speakers(rows: list[dict], turns: list[tuple]) -> list[dict]:
    """단어마다 가장 많이 겹치는(안 겹치면 가장 가까운) 화자를 붙이고, 같은 Whisper 구간 안에서 화자가 바뀌면 줄을 나눈다."""
    numbers: dict = {}  # 화자 번호는 처음 말한 순서대로 1부터
    out: list[dict] = []
    for row in rows:
        for w in row["words"]:
            # 단어마다 모든 화자 구간을 훑는다. 몇 시간짜리 회의에서 느리면 구간을 시간순으로 따라가게 바꾼다.
            overlap, best = max(((min(w[1], t[1]) - max(w[0], t[0]), t) for t in turns), key=lambda x: x[0], default=(None, None))
            # 겹치는 화자 구간이 없고 가까운 구간도 멀면 억지로 붙이지 않고 미확인(None)으로 둔다.
            known = best is not None and best[2] is not None and overlap > -CFG["diarization"]["max_gap"]
            speaker = numbers.setdefault(best[2], len(numbers) + 1) if known else None
            if out and out[-1]["seg"] == row["seg"] and out[-1]["speaker"] == speaker:
                out[-1]["words"].append(w)
            else:
                out.append({"seg": row["seg"], "speaker": speaker, "words": [w]})
    for r in out:
        r.update(start=r["words"][0][0], end=r["words"][-1][1], text="".join(w[2] for w in r["words"]).strip())
    return out


def carry_names(old: list[dict], new: list[dict], names: dict) -> dict:
    """화자를 다시 구분해 번호가 바뀌어도 붙여 둔 이름을 잃지 않게, 그 사람의 말을 가장 많이 이어받은 새 번호로 이름을 옮긴다."""
    seconds: dict = {}  # (옛 번호, 새 번호) -> 겹친 시간. 두 목록은 같은 단어를 같은 순서로 담고 있다.
    for was, (now, w) in zip((r.get("speaker") for r in old for _ in r["words"]), ((r["speaker"], w) for r in new for w in r["words"])):
        if now and str(was) in names:
            seconds[was, now] = seconds.get((was, now), 0) + w[1] - w[0]
    best: dict = {}  # 새 번호 -> (겹친 시간, 이름). 옛 번호마다 가장 많이 겹친 새 번호 하나에만 이름을 건다.
    for was in {k[0] for k in seconds}:
        sec, now = max((v, k[1]) for k, v in seconds.items() if k[0] == was)
        best[now] = max(best.get(now, (0, "")), (sec, names[str(was)]))
    return {str(now): name for now, (_, name) in best.items()}


def label_speakers(d: Path, meta: dict, samples, count: int | None) -> None:
    meta.update(stage="화자 구분 중", progress=0)
    write_meta(d, meta)

    def on_progress(p: float) -> None:
        meta["progress"] = round(p, 3)
        write_meta(d, meta)

    # 모델에는 화자 수를 주지 않는다. 일단 잘게 나누게 두고, 목소리 특징으로 합치는 단계에서 수를 맞춘다.
    old = read_transcript(d)
    # 화자 구분 모델이 도는 동안에는 멈추지 못하고 끝난 뒤에 멈춘다. 1시간 회의에 1분쯤 걸린다.
    turns = find_turns(samples, None, on_progress)
    tick(d, meta)
    rows = assign_speakers(old, merge_turns(samples, turns, count))
    write_rows(d, rows)
    meta["speakers"] = carry_names(old, rows, meta.get("speakers", {}))
    write_clean(d, meta, samples)


class Stopped(Exception):
    """화면에서 처리를 중지했다."""


stopping: set[str] = set()  # 중지를 누른 회의 id. 작업이 다음 단계로 넘어갈 때 보고 멈춘다


def tick(d: Path, meta: dict) -> None:
    """진행 상황을 적는다. 중지를 눌렀으면 여기서 작업을 끝낸다."""
    if d.name in stopping:
        raise Stopped
    write_meta(d, meta)


def run_job(d: Path, work) -> None:
    """작업 하나를 돌리고 결과를 meta에 남긴다. work(d, meta)는 meta를 고쳐 쓴다."""
    meta = read_meta(d)
    try:
        work(d, meta)
        write_text(d, meta)
        meta.update(status=STATUS_DONE, progress=1)
    except Stopped:
        # 받아쓰던 것은 버린다. 앞서 받아쓴 회의 내용이 있으면 그대로 완료 상태로 돌아간다.
        (d / "transcript.new").unlink(missing_ok=True)
        meta = {**read_meta(d), "progress": 1}
        if read_transcript(d):
            meta["status"] = STATUS_DONE
        else:
            meta.update(status=STATUS_ERROR, error="처리를 중지했습니다. '처음부터 다시 받아쓰기'를 누르면 남은 녹음으로 다시 처리합니다.")
    except Exception as e:  # 디코딩할 수 없는 파일도 여기로 온다
        meta.update(status=STATUS_ERROR, error=f"{type(e).__name__}: {e}")
    stopping.discard(d.name)
    meta.pop("stage", None)
    write_meta(d, meta)


def load_audio(path: Path):
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=RATE)


def process_upload(d: Path, meta: dict) -> None:
    upload = d / meta["upload"]
    samples = load_audio(upload)
    if not len(samples):
        raise ValueError("소리가 들어 있지 않은 파일입니다.")
    write_opus(samples, d / AUDIO_NAME)
    # 가벼운 사본이 끝까지 풀리는지 확인한 뒤에만 올린 파일을 지운다. 실패하면 올린 파일이 폴더에 남는다.
    if abs(len(load_audio(d / AUDIO_NAME)) - len(samples)) > RATE:
        raise RuntimeError("녹음 압축본의 길이가 원본과 다릅니다.")
    upload.unlink()
    del meta["upload"]
    transcribe(d, meta, samples, meta.get("count"))


def process_recording(d: Path, meta: dict) -> None:
    """녹음을 받아쓴다. 이어서 녹음한 조각(part-*.ogg)이 있으면 먼저 원래 녹음 뒤에 붙인다."""
    import numpy as np

    parts = sorted(d.glob(PART_GLOB))
    if parts:
        (d / CLEAN_NAME).unlink(missing_ok=True)  # 녹음이 길어지므로 듣기용 사본도 다시 만든다
    samples = np.concatenate([load_audio(p) for p in [d / AUDIO_NAME, *parts]])
    if parts:
        # 이어 붙일 때마다 전체를 다시 압축하고 처음부터 다시 받아쓴다. 긴 회의에서 느리면 새 조각만 처리하게 바꾼다.
        tmp = d / "audio.tmp"
        write_opus(samples, tmp)
        os.replace(tmp, d / AUDIO_NAME)
        for p in parts:
            p.unlink()
    transcribe(d, meta, samples, meta.get("count"))


def transcribe(d: Path, meta: dict, samples, count: int | None) -> None:
    duration = len(samples) / RATE
    meta.update(audio=AUDIO_NAME, duration=round(duration, 1), stage="회의 내용 받아쓰는 중")
    write_meta(d, meta)
    segments, _ = get_model().transcribe(samples, language=CFG["stt"]["language"], vad_filter=True, word_timestamps=True)
    # 새로 받아쓴 내용은 따로 모았다가 끝까지 성공했을 때만 바꿔 넣는다. 도중에 실패하면 앞서 고쳐 둔 회의 내용이 남는다.
    new = d / "transcript.new"
    with new.open("w", encoding="utf-8") as out:
        for i, s in enumerate(segments):
            words = [[round(w.start, 2), round(w.end, 2), w.word] for w in s.words or []]
            words = words or [[round(s.start, 2), round(s.end, 2), s.text]]
            row = {"seg": i, "start": words[0][0], "end": words[-1][1], "text": s.text.strip(), "words": words}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            meta["progress"] = round(min(s.end / duration, 1), 3)
            tick(d, meta)
    os.replace(new, d / "transcript.jsonl")
    meta.pop("speakers", None)  # 처음부터 다시 받아쓴 내용에는 예전 화자 번호가 없다
    label_speakers(d, meta, samples, count)


def relabel(d: Path, meta: dict, count: int | None) -> None:
    label_speakers(d, meta, load_audio(d / meta["audio"]), count)


# 회의록 작업은 한 번에 하나(jobs worker 1)라 지금 쓰는 모델을 전역 하나로 둔다. worker를 늘리면 작업마다 넘긴다.
model_now = None  # None이면 config.toml의 [llm] model


def ollama_models() -> list[str] | None:
    """이 PC의 Ollama에 받아 둔 모델. Ollama가 없거나 꺼져 있으면 None."""
    try:
        with urllib.request.urlopen(CFG["llm"]["url"] + "/api/tags", timeout=CFG["llm"]["list_timeout_sec"]) as res:
            return sorted(m["name"] for m in json.loads(res.read())["models"])
    except (OSError, ValueError, KeyError):
        return None


def local_models() -> list[str]:
    """화면에서 고를 수 있는 모델: 이 PC의 Ollama에 받아 둔 것. 기본 모델이 맨 앞이다."""
    first = CFG["llm"]["model"]
    names = ollama_models() or []  # 비었으면 기본 모델만 보여 주고, 눌렀을 때 오류로 알린다
    if names and first not in names:
        return names  # 가벼운 모델만 받아 둔 PC. 없는 기본 모델을 맨 앞에 두면 그대로 눌렀을 때 실패한다
    return [first] + [n for n in names if n != first]


# 첫 화면 준비 안내. 받는 작업은 한 번에 하나씩, 진행 상황은 여기에 둔다.
setup_jobs = {}  # 이름 -> {"done": 받은 바이트, "total": 전체 바이트, "error": 오류 문구}
_whisper_ready = False


def whisper_ready() -> bool:
    """음성 인식 모델을 이미 받아 두었는지. 받았으면 다시 묻지 않는다."""
    global _whisper_ready
    if not _whisper_ready:
        from faster_whisper.utils import download_model

        try:
            download_model(CFG["stt"]["model"], cache_dir=str(MODELS), local_files_only=True)
            _whisper_ready = True
        except Exception:  # 받아 두지 않았으면 huggingface_hub가 여러 종류의 오류를 낸다
            pass
    return _whisper_ready


def pull_llm(name: str) -> None:
    """Ollama에 모델을 받게 하고 진행률을 setup_jobs에 적는다."""
    job = setup_jobs[name]
    req = urllib.request.Request(
        CFG["llm"]["url"] + "/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=CFG["llm"]["timeout_sec"]) as res:
            for line in res:
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                if msg.get("total"):
                    job.update(done=msg.get("completed", 0), total=msg["total"])
        del setup_jobs[name]
    except Exception as e:  # 네트워크가 끊기거나 디스크가 모자라면 화면에 그대로 알린다
        job["error"] = f"모델을 받지 못했습니다: {e}"


def pull_whisper() -> None:
    from faster_whisper.utils import download_model

    try:
        download_model(CFG["stt"]["model"], cache_dir=str(MODELS))
        del setup_jobs["whisper"]
    except Exception as e:
        setup_jobs["whisper"]["error"] = f"음성 인식 모델을 받지 못했습니다: {e}"


def ask_llm(system: str, user: str) -> str:
    """로컬 Ollama에 한 번 묻는다."""
    c = CFG["llm"]
    body = {
        "model": model_now or c["model"],
        "stream": False,
        "think": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"num_ctx": c["num_ctx"], "temperature": c["temperature"]},
    }
    req = urllib.request.Request(
        c["url"] + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=c["timeout_sec"]) as res:
        text = json.loads(res.read())["message"]["content"].strip()
    if not text:
        raise RuntimeError("모델이 빈 답을 돌려주었습니다.")
    return text


def split_chunks(lines: list[str], size: int) -> list[str]:
    """줄을 끊지 않고 size 글자 안팎의 덩어리로 묶는다."""
    chunks, now, length = [], [], 0
    for line in lines:
        if now and length + len(line) > size:
            chunks.append("\n".join(now))
            now, length = [], 0
        now.append(line)
        length += len(line) + 1
    return chunks + (["\n".join(now)] if now else [])


def minutes_lines(d: Path, meta: dict) -> list[str]:
    """LLM에 줄 회의 내용. 분이 바뀔 때마다 [시:분] 줄을 넣어 메모가 시각을 달 수 있게 한다."""
    lines, minute = [], -1
    for r in read_transcript(d):
        now = int(r["start"]) // 60
        if now != minute:
            minute = now
            lines.append(f"[{now // 60:02}:{now % 60:02}]")
        lines.append(f"{speaker_name(meta, r)}: {r['text']}")
    return lines


def write_minutes(d: Path, meta: dict) -> None:
    """회의 내용을 덩어리마다 메모로 줄이고, 메모를 모아 회의록을 쓴다."""
    notes = split_chunks(minutes_lines(d, meta), CFG["llm"]["chunk_chars"])
    total = len(notes) + CFG["minutes"]["agendas"][1] + CFG["minutes"]["extra_calls"]  # 진행률 어림. 안건 수는 써 봐야 안다
    done = 0

    def step() -> None:
        nonlocal done
        done += 1
        meta["progress"] = round(min(done / total, 1), 3)
        tick(d, meta)

    for i, chunk in enumerate(notes):
        notes[i] = ask_llm(NOTE_PROMPT, chunk)
        step()
    # 메모 전체를 안건마다 다시 읽힌다. 몇 시간짜리 회의라 메모가 num_ctx를 넘으면 메모를 한 단계 더 줄인다.
    text = tidy(compose_minutes(meta["title"], notes, step, meta["created"][:10]))
    tmp = d / (MINUTES_NAME + ".tmp")
    tmp.write_text(minutes_head(meta) + f"{text}\n", encoding="utf-8")
    os.replace(tmp, d / MINUTES_NAME)


def sort_notes(notes: list[str]) -> dict[str, str]:
    """덩어리마다 쓴 메모를 [논의], [결정] 같은 분류별로 모은다."""
    got: dict[str, list[str]] = {t: [] for t in NOTE_TAGS}
    for note in notes:
        tag = NOTE_TAGS[0]
        for line in note.splitlines():
            head = TAG_RE.match(line)
            if head:
                tag = head[1]
            elif line.strip("*- \t"):
                got[tag].append("- " + line.strip("*- \t"))
    return {t: "\n".join(lines) for t, lines in got.items()}


def same(a: str, b: str) -> bool:
    """두 항목이 같은 내용인가. 시각을 떼고, 짧은 쪽의 일정 비율 이상이 긴 쪽에 그대로 들어 있으면 같다고 본다."""
    a, b = TIMES_RE.sub("", a), TIMES_RE.sub("", b)
    block = SequenceMatcher(None, a, b, autojunk=False).find_longest_match()
    return block.size >= CFG["minutes"]["same"] * min(len(a), len(b))


def bullets(text: str, limit: int) -> list[str]:
    """모델 답에서 '- ' 항목만 limit개까지 남긴다. 덧붙인 설명 문장과 넘치는 항목은 버린다."""
    lines = [line.strip().replace("**", "") for line in text.splitlines() if line.strip().startswith("- ")]
    return [line for line in lines if line.strip("- .") != "없음"][:limit]


def compose_minutes(title: str, notes: list[str], step=lambda: None, day: str = "") -> str:
    """메모를 모아 회의록 본문을 쓴다. 안건 목록, 안건별 논의, 결정, 실행 항목, 미결, 요약을 따로 묻는다."""
    c = CFG["minutes"]
    by = sort_notes(notes)
    talk = "\n".join(f"[{t}]\n{by[t]}" for t in ("논의", "숫자", "결정") if by[t])

    def ask(system: str, user: str) -> str:
        step()
        return ask_llm(system, f"회의 제목: {title}\n회의 날짜: {day}\n\n{user or '(메모 없음)'}")  # 날짜는 기한을 셈하는 기준이다

    outline = ask(OUTLINE_PROMPT.format(low=c["agendas"][0], high=c["agendas"][1]), talk)
    names = [n for n in (line.strip("-*#0123456789. \t") for line in outline.splitlines()) if 0 < len(n) <= TITLE_MAX]
    names = names[: c["agendas"][1]] or [title]
    agendas: list[str] = []
    seen: list[str] = []
    for name in names:
        got = bullets(ask(AGENDA_PROMPT.format(names=", ".join(names), name=name, limit=c["items"]), talk), c["items"])
        # 안건끼리 겹치게 쓴 항목은 먼저 나온 것만 남긴다.
        got = [g for g in got if not any(same(g, old) for old in seen)]
        seen += got
        if got:
            agendas += [f"### {name}", *got, ""]
    decided = bullets(ask(DECISION_PROMPT, by["결정"]), c["decisions"]) if by["결정"] else []
    rows = ["| 할 일 | 담당 | 기한 | 시각 |"]  # 할 일 메모가 없으면 묻지 않는다. 모델이 표 대신 '없음'만 써서 회의록 전체가 실패한다
    for _ in range(CFG["llm"]["tries"] if by["할 일"] else 0):  # 표로 안 쓰면 다시 묻는다
        rows = [line.strip() for line in ask(TODO_PROMPT, by["할 일"]).splitlines() if line.strip().startswith("|")]
        if len(rows) >= 2 and "할 일" in rows[0]:
            break
    else:
        if by["할 일"]:
            raise RuntimeError("모델이 회의록 형식을 지키지 않았습니다. 다시 시도해 주세요.")
    rows = [r for r in rows if not set(r) <= set("|-: ")]  # 구분 줄은 아래에서 새로 넣는다
    pending = bullets(ask(OPEN_PROMPT, by["미결"]), c["open"]) if by["미결"] else []
    summary = bullets(ask(SUMMARY_PROMPT.format(limit=c["items"]), "\n".join(agendas + ["[결정]"] + decided)), c["items"])
    none = ["- 없음"]
    return "\n".join(
        [MINUTES_HEADS[0], *(summary or none), "", MINUTES_HEADS[1], *agendas, MINUTES_HEADS[2], *(decided or none), ""]
        + [MINUTES_HEADS[3], rows[0], "|" + " --- |" * (rows[0].count("|") - 1), *rows[1:], ""]
        + [MINUTES_HEADS[4], *(pending or none)]
    )


def tidy(text: str) -> str:
    """모델이 쓴 회의록 다듬기. 지시해도 자주 남는 버릇을 지운다: 괄호 속 영어, 항목 끝 마침표, 붙여 쓴 쌍점."""
    out = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            line = ENGLISH_PAREN_RE.sub("", line)
            line = re.sub(r"\.(?=\s*(\(\d{1,2}:\d\d\))?\s*$)", "", line)
        out.append(COLON_RE.sub(" : ", line).rstrip())  # '기준값: 내용'을 '기준값 : 내용'으로
    return "\n".join(out)


def minutes_head(meta: dict) -> str:
    """회의록 맨 위: 제목, 일시, 장소, 참석자. 장소는 넣었을 때만 나온다. 참석자는 손으로 적은 것이 있으면 그것을 쓴다."""
    named = sorted(set(meta.get("speakers", {}).values()))
    lines = [f"# {meta['title']} 회의록", "", f"- 일시 : {meta['created'].replace('T', ' ')[:16]}"]
    if meta.get("place"):
        lines.append(f"- 장소 : {meta['place']}")
    lines += [f"- 참석자 : {meta.get('attendees') or ', '.join(named) or '이름 미확인'}", "", ""]
    return "\n".join(lines)


def refresh_head(d: Path, meta: dict) -> None:
    """제목, 장소, 참석자 이름이 바뀌면 본문은 두고 맨 위만 다시 쓴다. 고친 시각은 그대로 둬서 '다시 작성' 안내가 바뀌지 않는다."""
    f = d / MINUTES_NAME
    if not f.is_file():
        return
    st, text = f.stat(), f.read_text(encoding="utf-8")
    tmp = d / (MINUTES_NAME + ".tmp")
    tmp.write_text(minutes_head(meta) + text[max(text.find("\n## ") + 1, 0) :], encoding="utf-8")
    os.replace(tmp, f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))


def minutes_docx(text: str) -> bytes:
    """회의록 Markdown을 양식 꼴 Word 문서로 바꾼다: 가운데 제목, 회의 개요 표, 번호 붙은 제목, 항목, 실행 항목 표.
    화면에서 눌러 듣는 데 쓰는 (시:분)과 '시각' 칸은 문서에서 뺀다."""
    import io

    from docx import Document
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    c = CFG["docx"]
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(c["page_cm"][0]), Cm(c["page_cm"][1])
    sec.left_margin = sec.right_margin = sec.top_margin = sec.bottom_margin = Cm(c["margin_cm"])
    body_cm = c["page_cm"][0] - 2 * c["margin_cm"]
    normal = doc.styles["Normal"]
    normal.font.name = c["font"]
    normal.font.size = Pt(c["size_pt"])
    fonts = normal.element.rPr.rFonts
    fonts.set(qn("w:eastAsia"), c["font"])  # 한글은 이 속성을 따로 봐야 글꼴이 바뀐다
    for key in [k for k in fonts.attrib if k.endswith("Theme")]:
        del fonts.attrib[key]  # 테마 글꼴 지정이 남아 있으면 Word가 위 글꼴을 무시한다
    normal.paragraph_format.line_spacing = c["line_spacing"]
    normal.paragraph_format.space_after = Pt(c["gap_pt"])

    def el(tag: str, **attrs):
        node = OxmlElement(tag)
        for k, v in attrs.items():
            node.set(qn(f"w:{k}"), str(v))
        return node

    def para(text: str, size=None, bold=False, before=0.0, left=0.0, hang=0.0, level=None, line=False, center=False):
        p = doc.add_paragraph()
        if line:  # 제목 아래 가는 줄. pPr 맨 앞에 와야 해서 다른 서식보다 먼저 넣는다
            border = el("w:pBdr")
            border.append(el("w:bottom", val="single", sz=6, space=3, color="000000"))
            p._p.get_or_add_pPr().append(border)
        run = p.add_run(text)
        run.bold = bold
        if size:
            run.font.size = Pt(size)
        f = p.paragraph_format
        f.space_before, f.left_indent, f.first_line_indent = Pt(before), Cm(left), Cm(-hang)
        if hang:
            f.tab_stops.add_tab_stop(Cm(left))  # '-' 뒤 탭이 본문 시작 자리에서 멈추게 한다
        if center:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if level is not None:  # Word 탐색 창과 목차에 잡히는 제목 수준
            f.keep_with_next = True
            p._p.get_or_add_pPr().append(el("w:outlineLvl", val=level))
        return p

    def grid(rows: list[list[str]], widths: list[float], header: bool, left: int = 0) -> None:
        """header면 첫 줄이, 아니면 첫 칸이 이름표다. 이름표는 바탕을 칠하고 굵게 한다. header 표에서는 left 칸의 본문만 왼쪽에 붙이고 나머지는 가운데에 둔다."""
        t = doc.add_table(rows=len(rows), cols=len(widths), style="Table Grid")
        t.autofit = False
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        for column, w in zip(t.columns, widths):
            column.width = Cm(w)  # 칸 너비는 여기(tblGrid)와 아래 칸마다(tcW) 둘 다 적어야 Word와 LibreOffice가 같이 따른다
        for i, r in enumerate(rows):
            for j, w in enumerate(widths):
                cell = t.cell(i, j)
                cell.width = Cm(w)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                label = i == 0 if header else j == 0
                p = cell.paragraphs[0]
                p.add_run(r[j] if j < len(r) else "").bold = label
                p.paragraph_format.space_before = p.paragraph_format.space_after = Pt(c["cell_pad_pt"])
                if label or (header and j != left):
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                if label:
                    cell._tc.get_or_add_tcPr().append(el("w:shd", val="clear", color="auto", fill=c["shade"]))
        if header:
            t.rows[0]._tr.get_or_add_trPr().append(el("w:tblHeader"))  # 쪽이 넘어가면 머리행을 다시 보여 준다
        doc.add_paragraph().paragraph_format.space_after = Pt(0)

    table: list[list[str]] = []
    info: list[list[str]] = []  # 첫 제목 앞의 '일시 : ...' 줄들. 회의 개요 표가 된다

    def flush() -> None:
        rows = [r for r in table if not all(set(cell) <= set("-: ") for cell in r)]  # |---| 구분 줄은 뺀다
        table.clear()
        if rows:
            if "시각" in rows[0]:
                at = rows[0].index("시각")
                rows = [r[:at] + r[at + 1 :] for r in rows]
            cols = max(len(r) for r in rows)
            # 맨 앞에 번호 칸을 붙인다. 기한 칸은 '2026-11-11 ~ 2026-11-20'이 한 줄에 들어가게 넓힌다.
            rest = [c["due_cm"] if h == "기한" else c["narrow_cm"] for h in rows[0][1:]] + [c["narrow_cm"]] * (cols - len(rows[0]))
            rows = [[str(i) if i else "No", *r] for i, r in enumerate(rows)]
            grid(rows, [c["num_cm"], body_cm - c["num_cm"] - sum(rest), *rest], header=True, left=1)
        if info:
            grid(info, [c["label_cm"], body_cm - c["label_cm"]], header=False)
            info.clear()

    section = agenda = 0
    for raw in CONTROL_RE.sub("", text).splitlines():
        line = raw.replace("**", "").rstrip()
        if line.lstrip().startswith("|"):
            table.append([cell.strip() for cell in line.strip().strip("|").split("|")])
            continue
        line = TIMES_RE.sub("", line)
        head = re.match(r"(#{1,4}) +(.*)", line)
        item = re.match(r"(\s*)(?:[-*]|\d+\.) +(.*)", line)
        pair = item and not section and re.match(r"(.+?) : (.+)", item[2])
        if pair:
            info.append([pair[1], pair[2]])
            continue
        flush()
        if head and len(head[1]) == 1:
            para(head[2], size=c["title_pt"], bold=True, center=True).paragraph_format.space_after = Pt(c["title_pt"])
        elif head and len(head[1]) == 2:
            section, agenda = section + 1, 0
            para(f"{section}. {head[2]}", size=c["head_pt"], bold=True, before=c["head_pt"], level=0, line=True)
        elif head:
            agenda += 1
            para(f"({agenda}) {head[2]}", bold=True, before=c["size_pt"] / 2, left=c["indent_cm"] / 2, level=1)
        elif item:
            left = c["indent_cm"] * (3 if len(item[1]) >= 2 else 2)
            para(f"-\t{item[2]}", left=left, hang=c["indent_cm"])
        elif line.strip():
            para(line.strip(), left=c["indent_cm"])
    flush()

    foot = sec.footer.paragraphs[0]  # 쪽 번호
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for node in (el("w:fldChar", fldCharType="begin"), el("w:instrText"), el("w:fldChar", fldCharType="end")):
        if node.tag == qn("w:instrText"):
            node.text = "PAGE"
        foot.add_run()._r.append(node)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def run_minutes(d: Path) -> None:
    # 회의록을 못 만들어도 받아쓴 회의 자체는 멀쩡하다. 회의 상태는 완료로 되돌리고 실패 이유만 따로 남긴다.
    global model_now
    meta = read_meta(d)
    model_now = meta.get("model")
    try:
        write_minutes(d, meta)
        meta.pop("minutes_error", None)
    except Stopped:
        pass  # 쓰던 회의록은 버리고 전에 쓴 회의록을 그대로 둔다
    except Exception as e:
        meta["minutes_error"] = f"회의록을 만들지 못했습니다. Ollama가 켜져 있는지 확인하고 다시 시도해 주세요. ({type(e).__name__} : {e})"
    model_now = None
    stopping.discard(d.name)
    meta.update(status=STATUS_DONE, progress=1)
    meta.pop("stage", None)
    write_meta(d, meta)


def parse_count(text: str | None) -> int | None:
    """화면에서 받은 참석자 수. 비었거나 범위를 벗어나면 자동(None)으로 둔다."""
    return int(text) if text and text.isdigit() and 1 <= int(text) <= CFG["diarization"]["max_speakers"] else None


@app.post("/api/meetings")
async def upload(file: UploadFile, count: str = ""):
    name = Path(file.filename or "")
    ext = name.suffix if EXT_RE.match(name.suffix) else ""
    title = name.stem[:TITLE_MAX] or "제목 없음"
    mid = new_id(title)
    d = DATA / mid
    d.mkdir(parents=True)
    size = 0
    with (d / f"upload{ext}").open("wb") as out:
        while chunk := await file.read(CHUNK):
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                shutil.rmtree(d)
                raise HTTPException(413, f"파일이 너무 큽니다. 최대 {CFG['upload']['max_mb']}MB")
            out.write(chunk)
    if size == 0:
        shutil.rmtree(d)
        raise HTTPException(400, "빈 파일입니다.")
    meta = {
        "id": mid,
        "title": title,
        "created": datetime.now().isoformat(timespec="seconds"),
        "upload": f"upload{ext}",
        "count": parse_count(count),
        "status": STATUS_PROCESSING,
        "progress": 0,
    }
    write_meta(d, meta)
    jobs.submit(run_job, d, process_upload)
    return meta


class NewMeeting(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)


@app.post("/api/new")
def new_meeting(body: NewMeeting):
    """녹음 없이 회의부터 만든다. 녹음은 상세 화면에서 시작한다."""
    title = body.title.strip() or "제목 없음"
    mid = new_id(title)
    (DATA / mid).mkdir(parents=True)
    meta = {"id": mid, "title": title, "created": datetime.now().isoformat(timespec="seconds"), "count": None, "status": STATUS_DONE, "progress": 1}
    write_meta(DATA / mid, meta)
    return meta


def local_socket(ws: WebSocket) -> bool:
    # 브라우저는 WebSocket 연결에 Origin을 항상 붙인다. 없거나 다른 사이트면 받지 않는다.
    return ws.headers.get("host") in LOCAL_HOSTS and ws.headers.get("origin") in LOCAL_ORIGINS


@app.websocket("/api/record")
async def record(ws: WebSocket):
    """브라우저 마이크 소리(16kHz 모노 float32)를 받아 meeting 회의에 바로 Opus로 저장한다. 끝나면 전사와 화자 구분을 건다."""
    import numpy as np

    if not local_socket(ws) or ws.query_params.get("rate") != str(RATE):
        await ws.close(code=1008)
        return
    mid = ws.query_params.get("meeting", "")
    d = DATA / mid
    if not is_meeting(mid) or read_meta(d)["status"] in (STATUS_PROCESSING, STATUS_RECORDING):
        await ws.close(code=1008)
        return
    meta = read_meta(d)
    before = meta["status"], meta.get("error")
    # 녹음이 이미 있으면 조각으로 받아 뒤에 붙인다.
    target = d / (PART_GLOB.replace("*", datetime.now().strftime("%Y%m%d-%H%M%S")) if (d / AUDIO_NAME).is_file() else AUDIO_NAME)
    await ws.accept()
    meta.update(status=STATUS_RECORDING, progress=0)
    meta.pop("error", None)
    write_meta(d, meta)
    await ws.send_json(meta)
    writer = OpusWriter(target)
    limit = CFG["record"]["max_minutes"] * 60 * RATE
    try:
        while writer.samples < limit:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect" or msg.get("text") == "stop":
                break  # 탭을 닫아 끊겨도 그때까지 받은 소리는 저장한다
            data = msg.get("bytes")
            if data and len(data) % 4 == 0:
                writer.write(np.nan_to_num(np.frombuffer(data, dtype=np.float32)).clip(-1, 1))
    finally:
        writer.close()
        if writer.samples:
            meta["status"] = STATUS_PROCESSING
            write_meta(d, meta)
            jobs.submit(run_job, d, process_recording)
        else:  # 녹음을 눌렀다가 소리 없이 끝냈다. 회의를 원래 상태로 돌린다.
            target.unlink(missing_ok=True)
            meta.update(status=before[0], progress=1, **({"error": before[1]} if before[1] else {}))
            write_meta(d, meta)
    try:
        await ws.close()
    except RuntimeError:
        pass  # 이미 끊긴 연결


def list_files(d: Path) -> list[dict]:
    files = []
    for p in sorted(d.iterdir()):
        try:
            # 처리 작업이 쓰고 있는 임시 파일(.tmp)은 목록을 읽는 사이에 사라진다. 보여 주지 않는다.
            if p.suffix not in (".tmp", ".new") and p.is_file() and not p.is_symlink():
                files.append({"name": p.name, "size": p.stat().st_size})
        except FileNotFoundError:
            pass
    return files


def idle_meeting(mid: str) -> tuple[Path, dict]:
    """고칠 수 있는(처리 중이 아닌) 회의만 돌려준다. 처리 작업이 meta를 계속 덮어쓰기 때문이다."""
    d = meeting_dir(mid)
    meta = read_meta(d)
    if meta["status"] in (STATUS_PROCESSING, STATUS_RECORDING):
        raise HTTPException(409, "녹음 중이거나 처리 중인 회의는 바꿀 수 없습니다.")
    return d, meta


def months_ago(day: date, n: int) -> date:
    y, m = divmod(day.year * 12 + day.month - 1 - n, 12)
    return day.replace(year=y, month=m + 1, day=min(day.day, monthrange(y, m + 1)[1]))  # 5월 31일의 석 달 전은 2월 말일


@app.get("/api/meetings")
def list_meetings(q: str = "", start: date | None = None, end: date | None = None, pages: int = Query(1, ge=1)):
    """최신순. 제목에 q가 들어 있고 만든 날이 start~end인 회의를 pages쪽만큼 돌려준다. 기간을 비우면 오늘부터 기본 개월 수 전까지다."""
    end = end or date.today()
    start = start or months_ago(end, LIST["months"])
    word = q.strip().casefold()
    # 요청마다 meta.json을 전부 읽는다. 회의가 수천 개로 늘면 색인을 둔다.
    metas = [m for m in (read_meta(p.parent) for p in DATA.glob("*/meta.json"))
             if word in m["title"].casefold() and start.isoformat() <= m["created"][:10] <= end.isoformat()]
    metas.sort(key=lambda m: m["created"], reverse=True)
    items = metas[:pages * LIST["page_size"]]
    return {"items": items, "total": len(metas), "start": start.isoformat(), "end": end.isoformat()}


@app.get("/api/meetings/{mid}")
def get_meeting(mid: str):
    d = meeting_dir(mid)
    rows = [{k: r.get(k) for k in ("start", "end", "speaker", "text")} for r in read_transcript(d)]
    f = d / MINUTES_NAME
    minutes = f.read_text(encoding="utf-8") if f.is_file() else None
    # 회의록을 쓴 뒤에 회의 내용, 참석자 이름, 제목이 바뀌었으면 화면에서 다시 쓰라고 알려 준다.
    stale = bool(minutes) and (d / TEXT_NAME).is_file() and f.stat().st_mtime < (d / TEXT_NAME).stat().st_mtime
    return {**read_meta(d), "transcript": rows, "folder": str(d), "files": list_files(d), "minutes": minutes, "minutes_stale": stale, "clean": (d / CLEAN_NAME).is_file()}


class Merge(BaseModel):
    source: int
    target: int


class MeetingPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=TITLE_MAX)
    created: datetime | None = None  # 회의 일시. 목록의 날짜와 회의록 맨 위에 쓰인다. 폴더 이름의 날짜는 그대로 둔다
    place: str | None = Field(None, max_length=TITLE_MAX)  # 회의 장소. 빈 글자면 지운다
    attendees: str | None = Field(None, max_length=ROW_MAX)  # 손으로 적은 참석자. 비우면 화자 이름을 쓴다
    speakers: dict[str, str] | None = None  # 화자 번호 -> 이름
    merge: Merge | None = None  # source 화자의 발언을 모두 target 화자의 것으로 합친다


@app.patch("/api/meetings/{mid}")
def patch_meeting(mid: str, body: MeetingPatch):
    with edit_lock:
        return apply_patch(mid, body)


def apply_patch(mid: str, body: MeetingPatch) -> dict:
    d, meta = idle_meeting(mid)
    # 고치기 전에 모두 확인한다. 도중에 거부하면 폴더 이름만 바뀌고 meta는 그대로인 상태가 남는다.
    if body.speakers is not None and not all(k.isdigit() and 0 < len(v.strip()) <= TITLE_MAX for k, v in body.speakers.items()):
        raise HTTPException(422, "화자 이름이 올바르지 않습니다.")
    rows = read_transcript(d)
    if body.merge and (body.merge.source == body.merge.target or not {body.merge.source, body.merge.target} <= {r["speaker"] for r in rows}):
        raise HTTPException(422, "합칠 참석자를 찾을 수 없습니다.")
    if body.title and body.title != meta["title"]:
        meta["title"] = body.title
        # 폴더 이름도 새 제목을 따라간다. 날짜는 회의 일시를 고쳐도 폴더를 만든 날 그대로 둔다.
        day = d.name[:8] if d.name[:8].isdigit() else meta["created"][:10].replace("-", "")
        renamed = DATA / new_id(body.title, day, keep=d.name)
        if renamed != d:
            os.rename(d, renamed)
            d = renamed
        meta["id"] = d.name
    if body.speakers is not None:
        meta["speakers"] = {**meta.get("speakers", {}), **{k: v.strip() for k, v in body.speakers.items()}}
    if body.merge:
        for r in rows:
            if r["speaker"] == body.merge.source:
                r["speaker"] = body.merge.target
        write_rows(d, rows)
        meta.get("speakers", {}).pop(str(body.merge.source), None)
    if body.created:
        meta["created"] = body.created.replace(tzinfo=None).isoformat(timespec="seconds")
    if body.place is not None:
        meta["place"] = body.place.strip()
    if body.attendees is not None:
        meta["attendees"] = body.attendees.strip()
    write_meta(d, meta)
    if body.title or body.speakers is not None or body.merge:
        write_text(d, meta)  # 장소만 바꿨을 때는 회의 내용 파일을 건드리지 않아 회의록이 오래된 것으로 표시되지 않는다
    refresh_head(d, meta)
    return meta


class RowPatch(BaseModel):
    text: str = Field(min_length=1, max_length=ROW_MAX)


@app.patch("/api/meetings/{mid}/rows/{index}")
def patch_row(mid: str, index: int, body: RowPatch):
    """회의 내용 한 줄을 사람이 고친다."""
    text = body.text.strip()
    with edit_lock:
        d, meta = idle_meeting(mid)
        rows = read_transcript(d)
        if not 0 <= index < len(rows) or not text:
            raise HTTPException(404, "고칠 줄을 찾을 수 없습니다.")
        row = rows[index]
        # 고친 줄은 단어 하나로 취급한다. 화자를 다시 구분해도 고친 글이 남는다(이 줄 안에서 화자를 더 나누지는 못한다).
        row.update(text=text, words=[[row["start"], row["end"], " " + text]])
        write_rows(d, rows)
        write_text(d, meta)
    return {"index": index, "text": text}


LINE_HEAD_RE = re.compile(r"\s*(?:(?:[-*]|\d+\.|#{3,4}) +|\|)")  # 고칠 수 있는 줄: 항목, 안건 제목(###), 표의 행


class LinePatch(BaseModel):
    old: str = Field(max_length=ROW_MAX)  # 화면이 보고 있던 줄. 그사이 회의록이 바뀌었는지 가린다
    text: str = Field(min_length=1, max_length=ROW_MAX)


@app.patch("/api/meetings/{mid}/minutes/{index}")
def patch_minutes(mid: str, index: int, body: LinePatch):
    """회의록 한 줄을 사람이 고친다. 줄의 종류(항목, 안건 제목, 표의 행)와 표의 칸 수는 바꾸지 못한다."""
    with edit_lock:
        d, _ = idle_meeting(mid)
        f = d / MINUTES_NAME
        lines = f.read_text(encoding="utf-8").split("\n") if f.is_file() else []
        # 맨 위(제목, 일시, 장소, 참석자)는 회의 정보에서 고친다. 첫 '## ' 아래만 받는다.
        if not 0 <= index < len(lines) or lines[index] != body.old or not any(line.startswith("## ") for line in lines[:index]):
            raise HTTPException(409, "회의록이 그사이 바뀌었습니다. 다시 열어 고쳐 주세요.")
        was, now = LINE_HEAD_RE.match(body.old), LINE_HEAD_RE.match(body.text)
        if not was or not now or was[0] != now[0] or "\n" in body.text or "\r" in body.text:
            raise HTTPException(422, "이 줄은 고칠 수 없습니다.")
        if was[0].endswith("|") and body.old.count("|") != body.text.count("|"):
            raise HTTPException(422, "표의 칸 수는 바꿀 수 없습니다.")
        lines[index] = body.text.rstrip()
        st = f.stat()
        tmp = d / (MINUTES_NAME + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, f)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # 고친 시각은 그대로 둬서 '다시 작성' 안내가 바뀌지 않는다
    return {"index": index, "text": lines[index]}


class SpeakerCount(BaseModel):
    count: int | None = Field(None, ge=1, le=CFG["diarization"]["max_speakers"])  # 비우면 자동


@app.post("/api/meetings/{mid}/speakers")
def redo_speakers(mid: str, body: SpeakerCount):
    """참석자 수를 정해 화자 구분만 다시 한다. 전사는 다시 하지 않는다."""
    d, meta = idle_meeting(mid)
    if not meta.get("audio") or not (d / meta["audio"]).is_file():
        raise HTTPException(409, "녹음 파일이 없어 화자를 다시 구분할 수 없습니다.")
    meta.update(status=STATUS_PROCESSING, progress=0, count=body.count)  # 다시 받아쓸 때도 이 수를 쓴다
    meta.pop("error", None)
    write_meta(d, meta)
    jobs.submit(run_job, d, lambda d, meta: relabel(d, meta, body.count))
    return meta


class MinutesRequest(BaseModel):
    model: str | None = None  # 비우면 기본 모델


@app.get("/api/models")
def list_models():
    return local_models()


@app.get("/api/setup")
def setup_status():
    """첫 화면 준비 안내: Ollama, 회의록 모델, 음성 인식 모델이 갖춰졌는지."""
    c = CFG["llm"]
    names = ollama_models()
    return {
        "ollama": names is not None,
        "llm": bool(names),
        "whisper": whisper_ready(),
        "model": c["model"],
        "light_model": c["light_model"],
        "download_page": c["download_page"],
        "jobs": dict(setup_jobs),  # 받는 스레드가 끝나며 항목을 지우므로 사본을 보낸다
    }


class PullRequest(BaseModel):
    name: str


@app.post("/api/setup/pull")
def setup_pull(body: PullRequest):
    """모델 받기를 시작한다. 받을 수 있는 것은 설정의 두 회의록 모델과 음성 인식 모델뿐이다."""
    c = CFG["llm"]
    if body.name not in (c["model"], c["light_model"], "whisper"):
        raise HTTPException(422, "받을 수 없는 모델입니다.")
    if body.name in setup_jobs and not setup_jobs[body.name].get("error"):
        return dict(setup_jobs)  # 이미 받는 중
    if body.name != "whisper" and ollama_models() is None:
        raise HTTPException(409, "Ollama가 켜져 있지 않습니다. Ollama를 설치하고 실행한 뒤 다시 누르세요.")
    setup_jobs[body.name] = {"done": 0, "total": 0, "error": None}
    work = pull_whisper if body.name == "whisper" else lambda: pull_llm(body.name)
    threading.Thread(target=work, daemon=True).start()
    return dict(setup_jobs)


@app.post("/api/meetings/{mid}/minutes")
def make_minutes(mid: str, body: MinutesRequest | None = None):
    """회의 내용을 요약해 회의록(회의록.md)을 쓴다. 다시 누르면 새로 쓴다."""
    d, meta = idle_meeting(mid)
    model = (body and body.model) or CFG["llm"]["model"]
    if model not in local_models():
        raise HTTPException(422, "이 PC의 Ollama에 없는 모델입니다.")
    meta["model"] = model
    if meta["status"] != STATUS_DONE or not read_transcript(d):
        raise HTTPException(409, "받아쓴 회의 내용이 있어야 회의록을 쓸 수 있습니다.")
    # 작성 중에 서버가 꺼지면 다음에 켤 때 처리 중단 오류로 보인다. '다시 받아쓰기'로 풀린다.
    meta.update(status=STATUS_PROCESSING, stage="회의록 작성 중", progress=0)
    write_meta(d, meta)
    jobs.submit(run_minutes, d)
    return meta


@app.get("/api/meetings/{mid}/minutes.docx")
def download_minutes(mid: str):
    """회의록을 Word 문서로 내려받는다. 저장된 회의록.md를 그대로 옮기므로 화면과 내용이 같다."""
    d = meeting_dir(mid)
    if not (d / MINUTES_NAME).is_file():
        raise HTTPException(404, "회의록이 아직 없습니다.")
    name = urllib.parse.quote(f"{read_meta(d)['title']} 회의록.docx")
    body = minutes_docx((d / MINUTES_NAME).read_text(encoding="utf-8"))
    return Response(body, media_type=DOCX_MIME, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@app.post("/api/meetings/{mid}/open")
def open_meeting_folder(mid: str):
    open_folder(meeting_dir(mid))
    return {"opened": mid}


@app.post("/api/meetings/{mid}/stop")
def stop_job(mid: str):
    """받아쓰기, 화자 구분, 회의록 작성을 중지한다. 하던 단계가 끝나는 대로 멈춘다."""
    d = meeting_dir(mid)
    if read_meta(d)["status"] != STATUS_PROCESSING:
        raise HTTPException(409, "처리 중인 회의가 아닙니다.")
    stopping.add(d.name)
    return {"stopping": True}


@app.post("/api/meetings/{mid}/process")
def reprocess(mid: str):
    """남아 있는 녹음으로 받아쓰기와 화자 구분을 처음부터 다시 한다. 중간에 끊긴 회의를 살릴 때 쓴다."""
    d, meta = idle_meeting(mid)
    if not (d / AUDIO_NAME).is_file():
        raise HTTPException(409, "녹음 파일이 없어 다시 처리할 수 없습니다.")
    meta.update(status=STATUS_PROCESSING, progress=0)
    meta.pop("error", None)
    write_meta(d, meta)
    jobs.submit(run_job, d, process_recording)
    return meta


def meeting_file(d: Path, name: str) -> Path:
    # 폴더에 실제로 있는 파일 이름과 같을 때만 내준다. 경로 조각이 섞인 이름은 여기서 걸러진다.
    if name not in {f["name"] for f in list_files(d)}:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return d / name


@app.get("/api/meetings/{mid}/files/{name}")
def get_file(mid: str, name: str):
    path = meeting_file(meeting_dir(mid), name)
    if name in (AUDIO_NAME, CLEAN_NAME):  # 화면에서 바로 재생한다
        return FileResponse(path, media_type="audio/ogg")
    # 그 밖의 파일은 브라우저가 열어 실행하지 못하게 항상 내려받기로만 준다(처리에 실패해 남은 upload.html 등).
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@app.delete("/api/meetings/{mid}/files/{name}")
def delete_file(mid: str, name: str):
    """녹음 파일만 지워 용량을 줄인다. 전사문과 회의록은 남는다."""
    d, meta = idle_meeting(mid)
    if name != meta.get("audio"):
        raise HTTPException(400, "녹음 파일만 따로 지울 수 있습니다.")
    meeting_file(d, name).unlink()
    (d / CLEAN_NAME).unlink(missing_ok=True)
    del meta["audio"]
    write_meta(d, meta)
    return meta


@app.delete("/api/meetings/{mid}")
def delete_meeting(mid: str):
    d, _ = idle_meeting(mid)
    shutil.rmtree(d)
    return {"deleted": mid}


def mark_interrupted() -> None:
    """서버가 처리 도중 꺼졌던 회의를 오류로 표시한다."""
    for p in DATA.glob("*/meta.json"):
        meta = read_meta(p.parent)
        if meta["status"] in (STATUS_PROCESSING, STATUS_RECORDING):
            meta.update(status=STATUS_ERROR, error="서버가 녹음이나 처리 도중 종료되었습니다. '다시 받아쓰기'를 누르면 남은 녹음으로 다시 처리합니다.")
            if (p.parent / AUDIO_NAME).is_file():
                meta["audio"] = AUDIO_NAME
            write_meta(p.parent, meta)
        if not (p.parent / TEXT_NAME).is_file():  # 이 기능이 생기기 전에 만든 회의
            write_text(p.parent, meta)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

def port_free() -> bool:
    import socket

    with socket.socket() as s:
        if os.name != "nt":  # 막 끈 서버의 TIME_WAIT를 사용 중으로 잘못 보지 않게. Windows에서는 남의 포트를 뺏는 옵션이라 쓰지 않는다
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, PORT))
        except OSError:
            return False
    return True


if __name__ == "__main__":
    import uvicorn
    import webbrowser

    url = f"http://{HOST}:{PORT}"
    if not port_free():
        sys.exit(
            f"{PORT}번 포트를 이미 쓰고 있습니다. 회의록 도구가 이미 켜져 있다면 브라우저에서 {url} 을 여세요.\n"
            "다른 프로그램이 쓰고 있다면 config.toml의 [server] port를 다른 숫자(예: 8766)로 바꾸고 다시 실행하세요."
        )
    DATA.mkdir(parents=True, exist_ok=True)
    mark_interrupted()
    print(f"회의록 도구: {url}  (끄려면 이 창에서 Ctrl+C)")
    threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run(app, host=HOST, port=PORT)
