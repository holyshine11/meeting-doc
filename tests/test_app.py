import sys
import io
import math
import struct
import time
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as m


def word(start, end, text):
    return SimpleNamespace(start=start, end=end, word=text)


class FakeModel:
    def transcribe(self, samples, **kwargs):
        words = [word(0.0, 0.4, " 안녕하세요"), word(0.5, 0.9, " 네")]
        return iter([SimpleNamespace(start=0.0, end=0.9, text=" 안녕하세요 네", words=words)]), None


def wav_bytes(seconds=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(m.RATE)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(m.RATE * seconds)))
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DATA", tmp_path)
    monkeypatch.setattr(m, "get_model", lambda: FakeModel())
    # 0.45초에 화자가 바뀐다. count를 주면 한 사람으로 본다.
    monkeypatch.setattr(m, "find_turns", lambda s, count, cb=None: [(0, 0.45, 7), (0.45, 1, 3)])
    monkeypatch.setattr(m, "merge_turns", lambda s, turns, count: [(a, b, 7) for a, b, _ in turns] if count == 1 else turns)
    monkeypatch.setattr(m, "denoise", lambda s: s)
    base = f"http://127.0.0.1:{m.PORT}"
    return TestClient(m.app, base_url=base, headers={"origin": base})


def wait_done(client, mid):
    for _ in range(100):
        got = client.get(f"/api/meetings/{mid}").json()
        if got["status"] not in (m.STATUS_PROCESSING, m.STATUS_RECORDING):
            return got
        time.sleep(0.05)
    raise AssertionError("전사가 끝나지 않음")


def test_upload_speakers_files_delete(client, tmp_path, monkeypatch):
    r = client.post("/api/meetings", files={"file": ("../../주간 회의.wav", wav_bytes())})
    assert r.status_code == 200
    mid = r.json()["id"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [mid]  # 파일 이름으로 경로를 벗어나지 못한다
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_DONE and got["title"] == "주간 회의", got
    # 한 Whisper 구간 안에서 화자가 바뀌면 줄이 나뉜다. 번호는 말한 순서대로 붙는다.
    assert [(t["speaker"], t["text"]) for t in got["transcript"]] == [(1, "안녕하세요"), (2, "네")]
    # 올린 원본은 지우고 가벼운 사본과 소음을 줄인 듣기용 사본만 남긴다.
    assert [f["name"] for f in got["files"]] == ["audio-clean.ogg", "audio.ogg", "meta.json", "transcript.jsonl", "회의 내용.txt"]
    assert got["clean"]
    base = f"/api/meetings/{mid}"
    audio = client.get(f"{base}/files/audio.ogg")
    assert audio.status_code == 200 and audio.content[:4] == b"OggS"
    assert client.get(f"{base}/files/..%2Fmeta.json").status_code == 404
    got_meta = client.get(f"{base}/files/meta.json")  # 녹음 말고는 내려받기로만 준다
    assert got_meta.headers["content-disposition"].startswith("attachment") and got_meta.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"{base}/files/audio.ogg", headers={"sec-fetch-site": "cross-site"}).status_code == 403

    day = mid[:8]
    assert mid == f"{day}-주간 회의"  # 폴더 이름은 년월일-회의 제목
    patched = client.patch(base, json={"title": "새 제목: 1/4분기?", "speakers": {"1": "김 부장"}}).json()
    mid = patched["id"]  # 제목을 바꾸면 폴더 이름도 바뀐다. 폴더에 못 쓰는 글자는 빠진다.
    assert mid == f"{day}-새 제목 1 4분기" and [p.name for p in tmp_path.iterdir()] == [mid]
    assert patched["speakers"] == {"1": "김 부장"}
    base = f"/api/meetings/{mid}"
    again = client.post("/api/meetings", files={"file": ("새 제목 1 4분기.wav", wav_bytes())}).json()["id"]
    assert again == f"{mid}-2"  # 같은 날 같은 제목은 번호를 붙인다
    wait_done(client, again)
    assert client.delete(f"/api/meetings/{again}").status_code == 200
    assert client.patch(base, json={"speakers": {"../x": "a"}}).status_code == 422
    text = (tmp_path / mid / m.TEXT_NAME).read_text(encoding="utf-8")  # 읽을 수 있는 회의 내용 파일에 바꾼 이름이 들어간다
    assert "[00:00:00] 김 부장: 안녕하세요" in text and "참석자 2: 네" in text and text.startswith("새 제목: 1/4분기?")
    opened = []
    monkeypatch.setattr(m, "open_folder", opened.append)
    assert client.post(f"{base}/open").status_code == 200 and opened == [tmp_path / mid]

    # 한 줄을 고치면 읽는 파일에도 반영되고, 화자를 다시 구분해도 고친 글이 남는다.
    assert client.patch(f"{base}/rows/1", json={"text": " 넵 "}).json() == {"index": 1, "text": "넵"}
    assert client.patch(f"{base}/rows/9", json={"text": "x"}).status_code == 404
    assert "참석자 2: 넵" in (tmp_path / mid / m.TEXT_NAME).read_text(encoding="utf-8")
    assert client.post(f"{base}/speakers", json={"count": 1}).status_code == 200  # 참석자 수를 정해 다시 구분
    got = wait_done(client, mid)
    assert [(t["speaker"], t["text"]) for t in got["transcript"]] == [(1, "안녕하세요 넵")]
    assert got["speakers"] == {"1": "김 부장"}  # 번호가 바뀌어도 붙여 둔 이름은 같은 사람을 따라간다

    assert client.delete(f"{base}/files/meta.json").status_code == 400
    assert client.delete(f"{base}/files/audio.ogg").status_code == 200
    assert client.post(f"{base}/speakers", json={}).status_code == 409
    assert client.delete(base).status_code == 200
    assert client.get("/api/meetings").json()["items"] == []


def test_rejects_foreign_host_and_origin(client):
    assert client.get("/api/meetings", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/meetings", headers={"origin": "http://evil.example"}).status_code == 403
    # 바꾸는 요청은 Origin이 없어도 거부한다.
    assert TestClient(m.app, base_url=f"http://127.0.0.1:{m.PORT}").post("/api/meetings", files={"file": ("a.wav", b"x")}).status_code == 403


def test_rejects_bad_id_empty_and_oversize(client, monkeypatch):
    assert client.get("/api/meetings/..%2F..%2Fetc").status_code == 404
    assert client.post("/api/meetings", files={"file": ("a.wav", b"")}).status_code == 400
    monkeypatch.setattr(m, "MAX_UPLOAD", 5)
    assert client.post("/api/meetings", files={"file": ("a.wav", b"x" * 10)}).status_code == 413
    assert client.get("/api/meetings").json()["items"] == []


def test_far_word_stays_unknown():
    rows = [{"seg": 0, "words": [[0.0, 0.4, " 가"], [5.0, 5.4, " 나"]]}]
    got = m.assign_speakers(rows, [(0.0, 0.5, 9)])
    assert [(r["speaker"], r["text"]) for r in got] == [(1, "가"), (None, "나")]


def test_record_over_websocket(client, tmp_path):
    import numpy as np
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):  # 다른 사이트에서 연 연결은 받지 않는다
        with client.websocket_connect(f"/api/record?rate={m.RATE}", headers={"host": f"127.0.0.1:{m.PORT}", "origin": "http://evil.example"}):
            pass
    tone = (np.sin(np.arange(m.RATE) / 10) * 0.3).astype(np.float32)
    host = f"127.0.0.1:{m.PORT}"  # 테스트 클라이언트는 WebSocket에 Host를 testserver로 보낸다
    origin = {"host": host, "origin": f"http://{host}"}
    with pytest.raises(WebSocketDisconnect):  # 회의를 먼저 만들어야 녹음할 수 있다
        with client.websocket_connect(f"/api/record?rate={m.RATE}", headers=origin):
            pass
    mid = client.post("/api/new", json={"title": "녹음 회의"}).json()["id"]
    blank = client.get(f"/api/meetings/{mid}").json()
    assert blank["status"] == m.STATUS_DONE and not blank["transcript"] and not blank.get("audio")
    # 녹음을 눌렀다가 소리 없이 끝내도 빈 회의는 남는다.
    with client.websocket_connect(f"/api/record?rate={m.RATE}&meeting={mid}", headers=origin) as ws:
        ws.receive_json()
        ws.send_text("stop")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert client.get(f"/api/meetings/{mid}").json()["status"] == m.STATUS_DONE
    with client.websocket_connect(f"/api/record?rate={m.RATE}&meeting={mid}", headers=origin) as ws:
        assert ws.receive_json()["id"] == mid
        for i in range(0, len(tone), 4000):
            ws.send_bytes(tone[i : i + 4000].tobytes())
        assert client.delete(f"/api/meetings/{mid}").status_code == 409  # 녹음 중에는 지울 수 없다
        ws.send_text("stop")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_DONE and got["title"] == "녹음 회의", got
    assert abs(got["duration"] - 1) < 0.1 and got["audio"] == "audio.ogg"
    assert [t["speaker"] for t in got["transcript"]] == [1, 2]

    # 같은 회의에 이어서 녹음하면 뒤에 붙는다.
    with client.websocket_connect(f"/api/record?rate={m.RATE}&meeting={mid}", headers=origin) as ws:
        assert ws.receive_json()["status"] == m.STATUS_RECORDING
        ws.send_bytes(tone.tobytes())
        ws.send_text("stop")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_DONE and abs(got["duration"] - 2) < 0.1, got
    assert not [f for f in got["files"] if f["name"].startswith("part-")]
    # 이어 녹음을 눌렀다가 바로 끝내면 회의는 그대로다.
    with client.websocket_connect(f"/api/record?rate={m.RATE}&meeting={mid}", headers=origin) as ws:
        ws.receive_json()
        ws.send_text("stop")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    after = client.get(f"/api/meetings/{mid}").json()
    assert after["status"] == m.STATUS_DONE and after["files"] == got["files"]
    assert client.post(f"/api/meetings/{mid}/process").status_code == 200
    assert abs(wait_done(client, mid)["duration"] - 2) < 0.1


def test_split_chunks_keeps_every_line():
    lines = [f"참석자 1: {'가' * 30}" for _ in range(10)]
    chunks = m.split_chunks(lines, 100)
    assert len(chunks) > 1 and "\n".join(chunks).splitlines() == lines
    assert m.split_chunks([], 100) == []


def test_same_item_is_found_inside_a_longer_one():
    short = "- 신청서 양식을 먼저 정리하고 담당 부서에 공유하기로 함 (00:49)"
    assert m.same(short, "- 접수 절차가 복잡하다는 의견이 있어 신청서 양식을 먼저 정리하고 담당 부서에 공유하기로 함 (00:46)")
    assert not m.same(short, "- 다음 달 행사 장소를 다시 알아보기로 함 (00:19)")


def test_minutes_written_and_failure_keeps_meeting_done(client, monkeypatch):
    mid = client.post("/api/meetings", files={"file": ("회의.wav", wav_bytes(), "audio/wav")}).json()["id"]
    wait_done(client, mid)
    assert client.get(f"/api/meetings/{mid}/minutes.docx").status_code == 404
    asked = []
    note = "**[논의] 안건별로 오간 내용**\n- 인사를 나눔 (00:00)\n[결정]\n- 결론: 미정. (00:00)\n[할 일]\n- 회의실 예약 (00:00)\n[미결]\n- 다음 날짜 미정 (00:00)"
    table = "| 할 일 | 담당 | 기한 | 시각 |\n|---|---|---|---|\n| 회의실 <예약> & 공지 | 미정 | 미정 | 00:00 |"
    todo = iter(["표 없이 답함", "안녕하세요. 표입니다.\n" + table])  # 첫 답은 표가 아니다. 한 번 더 물어 표를 받는다
    answers = {
        "안건 이름만": "1. 인사",
        "안건에 해당하는": "간추린 내용입니다.\n- 인사를 나눔 (00:00)\n" + "- 넘치는 항목\n" * 9,
        "결정됐다고": "- 결론: 미정. (00:00)",
        "따로 확인하기로 한 것을 적어 둔": "- 다음 회의 날짜 (Date) 미정 (00:00)",
        "요약하라": "- 인사를 나누고 끝남",
    }

    def fake(system, user):
        asked.append((system, user))
        if system == m.NOTE_PROMPT:
            return note
        if system == m.TODO_PROMPT:
            return next(todo)
        return next(v for k, v in answers.items() if k in system)

    monkeypatch.setattr(m, "ask_llm", fake)
    assert client.post(f"/api/meetings/{mid}/minutes").status_code == 200
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_DONE and "minutes_error" not in got, got
    text = got["minutes"]
    assert text.startswith("# 회의 회의록\n\n- 일시 : ") and "## 회의 요약 내용\n- 인사를 나누고 끝남\n" in text
    # 안건 항목은 상한까지만 남고, 모델이 덧붙인 설명, 괄호 속 영어, 항목 끝 마침표, 붙여 쓴 쌍점은 다듬어진다.
    assert "### 인사\n- 인사를 나눔 (00:00)\n" in text and text.count("- 넘치는 항목") == m.CFG["minutes"]["items"] - 1
    assert "- 결론 : 미정 (00:00)\n" in text and "- 다음 회의 날짜 미정 (00:00)" in text
    assert "입니다" not in text and [h for h in m.MINUTES_HEADS if h in text] == list(m.MINUTES_HEADS)
    assert "[00:00]\n참석자 1: 안녕하세요" in asked[0][1] and "- 회의실 예약 (00:00)" in asked[-3][1]
    assert sum(system == m.TODO_PROMPT for system, _ in asked) == 2
    assert m.MINUTES_NAME in [f["name"] for f in got["files"]] and not got["minutes_stale"]

    # Word 문서: 제목, 항목, 표가 들어가고 한글 글꼴이 지정된다.
    import io

    from docx import Document

    r = client.get(f"/api/meetings/{mid}/minutes.docx")
    assert r.status_code == 200 and "UTF-8''" in r.headers["content-disposition"]
    doc = Document(io.BytesIO(r.content))
    texts = [p.text for p in doc.paragraphs]
    assert "회의 회의록" in texts and "1. 회의 요약 내용" in texts and "(1) 인사" in texts and "-\t인사를 나눔" in texts
    # 맨 위 일시, 참석자는 회의 개요 표로 가고, 실행 항목 표에서 '시각' 칸은 빠진다.
    info, todo = doc.tables
    assert [r.cells[0].text for r in info.rows] == ["일시", "참석자"]
    assert [[c.text for c in r.cells] for r in todo.rows] == [["No", "할 일", "담당", "기한"], ["1", "회의실 <예약> & 공지", "미정", "미정"]]
    assert m.CFG["docx"]["font"] in doc.styles["Normal"].element.xml

    # 장소를 넣으면 회의록 맨 위만 바뀌고 오래된 것으로 표시되지는 않는다.
    assert client.patch(f"/api/meetings/{mid}", json={"place": " 3층 회의실 "}).status_code == 200
    assert client.patch(f"/api/meetings/{mid}", json={"created": "2026-10-01T14:30"}).json()["created"] == "2026-10-01T14:30:00"
    assert client.patch(f"/api/meetings/{mid}", json={"created": "어제"}).status_code == 422
    got = client.get(f"/api/meetings/{mid}").json()
    assert "- 장소 : 3층 회의실\n- 참석자 : " in got["minutes"] and "녹음 길이" not in got["minutes"]
    assert "\n\n## 회의 요약 내용\n- 인사를 나누고" in got["minutes"] and not got["minutes_stale"]

    # 회의록 한 줄 고치기: 항목과 표의 행만, 보고 있던 줄이 그대로일 때만 받는다.
    lines = got["minutes"].split("\n")
    at = lines.index("- 인사를 나눔 (00:00)")
    url = f"/api/meetings/{mid}/minutes"
    assert client.patch(f"{url}/{at}", json={"old": lines[at], "text": "- 서로 인사함 (00:00)"}).status_code == 200
    assert client.patch(f"{url}/{at}", json={"old": lines[at], "text": "- 또 고침"}).status_code == 409  # 이미 바뀐 줄
    assert client.patch(f"{url}/2", json={"old": lines[2], "text": "- 일시 : 내일"}).status_code == 409  # 맨 위는 회의 정보에서
    assert client.patch(f"{url}/{at}", json={"old": "- 서로 인사함 (00:00)", "text": "## 제목으로"}).status_code == 422
    row = next(i for i, line in enumerate(lines) if line.startswith("| 회의실"))
    assert client.patch(f"{url}/{row}", json={"old": lines[row], "text": "| 칸이 | 모자람 |"}).status_code == 422
    got = client.get(f"/api/meetings/{mid}").json()
    assert "- 서로 인사함 (00:00)\n" in got["minutes"] and "인사를 나눔" not in got["minutes"] and not got["minutes_stale"]
    assert client.patch(f"{url}/{at}", json={"old": "- 서로 인사함 (00:00)", "text": lines[at]}).status_code == 200

    # 회의 내용을 고치면 회의록이 오래된 것으로 표시된다.
    time.sleep(0.05)
    assert client.patch(f"/api/meetings/{mid}/rows/0", json={"text": "안녕하십니까"}).status_code == 200
    assert client.get(f"/api/meetings/{mid}").json()["minutes_stale"]

    def down(system, user):
        raise OSError("연결 거부")

    monkeypatch.setattr(m, "ask_llm", down)
    client.post(f"/api/meetings/{mid}/minutes")
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_DONE and "연결 거부" in got["minutes_error"] and "인사를 나눔" in got["minutes"]


def test_merge_turns_joins_same_voice(monkeypatch):
    import numpy as np

    # 소리 값 자체를 목소리 특징으로 쓴다: 1과 2는 같은 목소리, 3은 다른 목소리, 4는 몇 마디만 한 다른 사람, 5는 너무 짧아 누구인지 모른다.
    voices = {1: [1, 0, 0], 2: [0.99, 0.1, 0], 3: [0, 1, 0], 4: [0, 0, 1], 5: [1, 0, 0]}
    monkeypatch.setattr(m, "embed_voice", lambda s: np.array(voices[int(s[0])]) / np.linalg.norm(voices[int(s[0])]))
    turns = [(0, 20, 1), (20, 35, 2), (35, 55, 3), (55, 57, 4), (57, 57.5, 5), (58, 70, 2)]
    samples = np.zeros(70 * m.RATE, dtype=np.float32)
    for start, end, sp in turns:
        samples[int(start * m.RATE) : int(end * m.RATE)] = sp
    assert [t[2] for t in m.merge_turns(samples, turns, None)] == [2, 2, 3, 4, None, 2]
    assert [t[2] for t in m.merge_turns(samples, turns, 1)] == [2, 2, 2, 2, None, 2]
    # 화자 미확인 구간과 겹치는 단어는 미확인으로 남는다.
    rows = m.assign_speakers([{"seg": 0, "words": [[57.1, 57.4, " 네"]]}], m.merge_turns(samples, turns, None))
    assert rows[0]["speaker"] is None


def test_count_is_kept_and_speakers_merge(client):
    mid = client.post("/api/meetings?count=", files={"file": ("회의.wav", wav_bytes())}).json()["id"]
    assert wait_done(client, mid)["count"] is None
    base = f"/api/meetings/{mid}"
    assert client.patch(base, json={"speakers": {"1": "김 부장", "2": "이 대리"}}).status_code == 200
    assert client.patch(base, json={"merge": {"source": 2, "target": 9}}).status_code == 422
    assert client.patch(base, json={"merge": {"source": 2, "target": 1}}).json()["speakers"] == {"1": "김 부장"}
    got = client.get(base).json()
    assert [t["speaker"] for t in got["transcript"]] == [1, 1]
    assert "김 부장: 네" in (m.DATA / mid / m.TEXT_NAME).read_text(encoding="utf-8")
    # 올릴 때 넣은 참석자 수는 처음 처리에 쓰이고 회의에 남는다.
    one = client.post("/api/meetings?count=1", files={"file": ("한 사람.wav", wav_bytes())}).json()["id"]
    got = wait_done(client, one)
    assert got["count"] == 1 and [t["speaker"] for t in got["transcript"]] == [1]


def test_names_follow_the_voice_when_relabeled():
    old = [{"speaker": 3, "words": [[0, 5, "가"], [5, 6, "나"]]}, {"speaker": 8, "words": [[6, 9, "다"]]}, {"speaker": 4, "words": [[9, 10, "라"]]}]
    new = [{"speaker": 1, "words": [[0, 5, "가"]]}, {"speaker": 2, "words": [[5, 6, "나"], [6, 9, "다"]]}, {"speaker": None, "words": [[9, 10, "라"]]}]
    # 3번(김)의 말은 대부분 새 1번으로, 8번(박)은 새 2번으로 갔다. 4번(최)의 말은 미확인이 되어 이름을 둘 곳이 없다.
    assert m.carry_names(old, new, {"3": "김", "8": "박", "4": "최"}) == {"1": "김", "2": "박"}


def test_failed_retranscription_keeps_edited_transcript(client, monkeypatch):
    mid = client.post("/api/meetings", files={"file": ("회의.wav", wav_bytes())}).json()["id"]
    wait_done(client, mid)
    base = f"/api/meetings/{mid}"
    client.patch(f"{base}/rows/0", json={"text": "손으로 고친 말"})

    class Broken:
        def transcribe(self, samples, **kwargs):
            def segments():
                yield SimpleNamespace(start=0.0, end=0.4, text=" 새로", words=[word(0.0, 0.4, " 새로")])
                raise RuntimeError("받아쓰기 고장")

            return segments(), None

    monkeypatch.setattr(m, "get_model", lambda: Broken())
    assert client.post(f"{base}/process").status_code == 200
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_ERROR and got["transcript"][0]["text"] == "손으로 고친 말"
    assert "transcript.new" not in [f["name"] for f in got["files"]]


def test_stop_keeps_what_was_there(client, monkeypatch):
    mid = client.post("/api/meetings", files={"file": ("회의.wav", wav_bytes(), "audio/wav")}).json()["id"]
    before = wait_done(client, mid)["transcript"]
    assert client.post(f"/api/meetings/{mid}/stop").status_code == 409  # 처리 중이 아니면 중지할 것이 없다

    def slow(d, meta):  # 일이 도는 사이에 중지가 들어온 것처럼
        m.stopping.add(d.name)
        m.tick(d, meta)

    monkeypatch.setattr(m, "process_recording", slow)
    assert client.post(f"/api/meetings/{mid}/process").status_code == 200
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_DONE and got["transcript"] == before and not m.stopping
    monkeypatch.setattr(m, "write_minutes", slow)
    assert client.post(f"/api/meetings/{mid}/minutes").status_code == 200
    got = wait_done(client, mid)
    assert got["status"] == m.STATUS_DONE and not got.get("minutes_error") and not got["minutes"] and not m.stopping


def test_model_choice_and_attendees(client, monkeypatch):
    monkeypatch.setattr(m, "local_models", lambda: ["기본", "큰 모델"])
    assert client.get("/api/models").json() == ["기본", "큰 모델"]
    mid = client.post("/api/meetings", files={"file": ("회의.wav", wav_bytes(), "audio/wav")}).json()["id"]
    wait_done(client, mid)
    assert client.post(f"/api/meetings/{mid}/minutes", json={"model": "없는 모델"}).status_code == 422
    used = []
    monkeypatch.setattr(m, "write_minutes", lambda d, meta: used.append(m.model_now))
    assert client.post(f"/api/meetings/{mid}/minutes", json={"model": "큰 모델"}).status_code == 200
    wait_done(client, mid)
    assert used == ["큰 모델"] and m.model_now is None
    # 손으로 적은 참석자가 회의록 머리말에 화자 이름 대신 들어간다.
    assert client.patch(f"/api/meetings/{mid}", json={"attendees": "김하나, 이두리"}).json()["attendees"] == "김하나, 이두리"
    assert "- 참석자 : 김하나, 이두리" in m.minutes_head(client.get(f"/api/meetings/{mid}").json())


def test_setup_status_and_pull(client, monkeypatch):
    light = m.CFG["llm"]["light_model"]
    monkeypatch.setattr(m, "whisper_ready", lambda: True)
    monkeypatch.setattr(m, "ollama_models", lambda: None)  # Ollama가 꺼져 있다
    got = client.get("/api/setup").json()
    assert not got["ollama"] and not got["llm"] and got["whisper"]
    assert client.post("/api/setup/pull", json={"name": light}).status_code == 409
    assert client.post("/api/setup/pull", json={"name": "아무 모델"}).status_code == 422
    # 가벼운 모델만 받아 둔 PC에서는 없는 기본 모델 대신 받아 둔 모델이 먼저 보인다.
    monkeypatch.setattr(m, "ollama_models", lambda: [light])
    assert client.get("/api/setup").json()["llm"] and client.get("/api/models").json() == [light]
    pulled = []
    monkeypatch.setattr(m, "pull_llm", pulled.append)
    assert client.post("/api/setup/pull", json={"name": light}).status_code == 200
    for _ in range(50):
        if pulled:
            break
        time.sleep(0.02)
    assert pulled == [light]
    m.setup_jobs.clear()


def test_list_filters_by_title_and_period_newest_first(client, monkeypatch):
    monkeypatch.setitem(m.LIST, "page_size", 2)
    for title in ("가 주간", "나 주간", "다 월간"):
        mid = client.post("/api/new", json={"title": title}).json()["id"]
        meta = m.read_meta(m.DATA / mid)
        m.write_meta(m.DATA / mid, {**meta, "created": {"가": "2026-01-05T09:00:00", "나": "2026-03-05T09:00:00", "다": "2026-03-06T09:00:00"}[title[0]]})
    get = lambda **params: client.get("/api/meetings", params=params).json()
    whole = get(start="2026-01-01", end="2026-12-31")
    assert [x["title"] for x in whole["items"]] == ["다 월간", "나 주간"] and whole["total"] == 3
    assert len(get(start="2026-01-01", end="2026-12-31", pages=2)["items"]) == 3
    assert [x["title"] for x in get(q="주간", start="2026-03-01", end="2026-03-31")["items"]] == ["나 주간"]
    assert get(end="2026-05-31")["start"] == "2026-02-28"  # 기간을 비우면 석 달 전부터
    assert m.months_ago(m.date(2026, 1, 15), 3) == m.date(2025, 10, 15)


def test_failed_patch_and_title_rename_keep_folder(client, tmp_path):
    mid = client.post("/api/meetings", files={"file": ("주간 회의.wav", wav_bytes())}).json()["id"]
    wait_done(client, mid)
    base = f"/api/meetings/{mid}"
    # 잘못된 요청은 아무것도 바꾸지 않는다. 폴더 이름도 그대로다.
    assert client.patch(base, json={"title": "다른 제목", "speakers": {"x": "a"}}).status_code == 422
    assert client.patch(base, json={"title": "다른 제목", "merge": {"source": 1, "target": 9}}).status_code == 422
    assert [p.name for p in tmp_path.iterdir()] == [mid] and client.get(base).json()["title"] == "주간 회의"
    # 폴더에 못 쓰는 글자만 다른 제목이면 폴더 이름이 그대로다(-2가 붙지 않는다).
    assert client.patch(base, json={"title": "주간 회의?"}).json()["id"] == mid
    # 회의 일시를 바꿔도 폴더 이름의 날짜는 처음 만든 날 그대로다.
    client.patch(base, json={"created": "2020-01-02T09:00"})
    assert client.patch(base, json={"title": "새 이름"}).json()["id"] == f"{mid[:8]}-새 이름"


def test_minutes_without_todo_notes_still_written(monkeypatch):
    asked = []

    def fake(system, user):
        asked.append(system)
        return "1. 인사" if "안건 이름만" in system else "- 인사를 나눔 (00:00)"

    monkeypatch.setattr(m, "ask_llm", fake)
    text = m.compose_minutes("회의", ["[논의]\n- 인사를 나눔 (00:00)"])
    assert m.TODO_PROMPT not in asked  # 할 일 메모가 없으면 표를 묻지 않는다
    assert "## 실행 항목\n| 할 일 | 담당 | 기한 | 시각 |\n| --- | --- | --- | --- |\n\n## 미결" in text


def test_replace_waits_while_windows_holds_the_file(tmp_path, monkeypatch):
    # Windows는 다른 요청이 meta.json을 읽는 순간 덮어쓰기를 거부한다(WinError 5). 잠깐 뒤 다시 해서 성공해야 한다.
    real, calls = m.os.replace, []

    def busy(src, dest):
        calls.append(dest)
        if len(calls) < 3:
            raise PermissionError(5, "액세스가 거부되었습니다")
        real(src, dest)

    monkeypatch.setattr(m.os, "replace", busy)
    monkeypatch.setattr(m, "REPLACE_WAIT", 0)
    m.write_meta(tmp_path, {"title": "회의"})
    assert len(calls) == 3 and m.read_meta(tmp_path)["title"] == "회의"

    def locked(src, dest):
        raise PermissionError(5, "액세스가 거부되었습니다")

    monkeypatch.setattr(m.os, "replace", locked)
    with pytest.raises(PermissionError):
        m.write_meta(tmp_path, {"title": "다른 회의"})
    assert m.read_meta(tmp_path)["title"] == "회의"
