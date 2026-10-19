"""The whole app through its HTTP API: upload, transcribe (demo backend), edit, export, burn in, restart."""
import json
import re
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from reelcaptioner import asr, jobs, media
from reelcaptioner.app import create_app

from .conftest import make_settings, upload, wait_for, wait_task
from .helpers import needs_ffmpeg, needs_libass

pytestmark = needs_ffmpeg


def edited(cards):
    """The shape the editor sends back: start, end, text and the words it already has."""
    return [{"start": c["start"], "end": c["end"], "text": " ".join(w["w"] for w in c["words"]), "words": c["words"]}
            for c in cards]


# --------------------------------------------------------------------------- the shell of the app
def test_status_describes_the_install(client):
    st = client.get("/api/status").json()
    assert st["demo"] is True and st["backend"] == "mock" and st["device"] == "demo"
    assert st["tools"]["ffmpeg"] is True
    assert [p["preset"] for p in st["presets"]] == ["pumpkin", "pop", "classic", "box"]
    assert {m["id"] for m in st["models"]} >= {"tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"}
    assert all(m["cached"] for m in st["models"])                         # demo mode needs no downloads
    assert {lang["id"] for lang in st["languages"]} == {"hinglish", "english", "auto"}
    assert all(m["note"] for m in st["models"])                           # said under the list for the chosen model
    # A closed drop-down in the Options panel has room for about this much before it clips the label.
    assert all(len(item["label"]) <= 26 for item in st["models"] + st["languages"])
    assert st["ollama"]["available"] is False and st["max_upload_mb"] == 50


def test_status_in_whisper_mode_reports_whether_models_are_downloaded(settings, monkeypatch):
    monkeypatch.setattr("reelcaptioner.app.is_model_cached", lambda m: m == "small")
    with TestClient(create_app(replace(settings, asr_backend="faster-whisper"))) as client:
        st = client.get("/api/status").json()
    assert st["demo"] is False and st["device"] == "cpu"
    assert {m["id"]: m["cached"] for m in st["models"]}["small"] is True
    assert {m["id"]: m["cached"] for m in st["models"]}["large-v3"] is False


def test_status_sees_a_running_ollama(settings, fake_ollama):
    with TestClient(create_app(replace(settings, ollama_url=fake_ollama.url))) as client:
        st = client.get("/api/status").json()
    assert st["ollama"] == {"available": True, "models": ["llama3.2:3b", "gemma3:4b"], "picked": "gemma3:4b"}


def test_the_page_and_its_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Hinglish Reel Captioner" in page.text
    assert page.headers["cache-control"] == "no-cache"
    js = client.get("/static/js/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    font = client.get("/static/fonts/Poppins-Bold.ttf")
    assert font.status_code == 200 and "immutable" in font.headers["cache-control"]
    assert client.get("/static/nope.css").status_code == 404
    assert client.get("/api/status").headers["cache-control"] == "no-cache"


# --------------------------------------------------------------------------- upload validation
def test_upload_validation(client, clip, tmp_path):
    def post(**kw):
        with clip.open("rb") as fh:
            return client.post("/api/jobs", files={"file": ("a.mp4", fh, "video/mp4")}, data=kw)

    assert post(language="klingon").status_code == 400
    assert post(model="bad\x00name").status_code == 400
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with empty.open("rb") as fh:
        resp = client.post("/api/jobs", files={"file": ("empty.mp4", fh, "video/mp4")})
    assert resp.status_code == 400 and "empty" in resp.json()["detail"]
    assert client.get("/api/jobs").json() == []                           # nothing was left behind


def test_oversized_uploads_are_refused_and_cleaned_up(settings, tmp_path):
    small = replace(settings, max_upload_mb=1)
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (2 * 1024 * 1024))
    with TestClient(create_app(small)) as client:
        with big.open("rb") as fh:
            resp = client.post("/api/jobs", files={"file": ("big.mp4", fh, "video/mp4")})
        assert resp.status_code == 413 and "1 MB" in resp.json()["detail"]
        assert client.get("/api/jobs").json() == []
    assert list((small.data_dir / "jobs").iterdir()) == []


def test_a_file_that_is_not_media_ends_in_a_friendly_error(client, tmp_path):
    junk = tmp_path / "notes.mp4"
    junk.write_text("definitely not a video")
    job = wait_for(client, upload(client, junk))
    assert job["status"] == "error" and "does not look like a video or audio file" in job["error"]
    assert client.get(f"/api/jobs/{job['id']}/cards").status_code == 409


def test_a_video_without_audio_is_explained(client, silent_video):
    job = wait_for(client, upload(client, silent_video))
    assert job["status"] == "error" and "no audio track" in job["error"]


def test_unknown_and_malformed_ids_are_404s(client):
    for path in ("/api/jobs/aaaaaaaaaaaaaaaaaaaa", "/api/jobs/not-an-id", "/api/jobs/../../etc/passwd"):
        assert client.get(path).status_code == 404
    assert client.get("/api/jobs/aaaaaaaaaaaaaaaaaaaa/download/srt").status_code == 404
    assert client.delete("/api/jobs/aaaaaaaaaaaaaaaaaaaa").status_code == 404
    assert client.post("/api/jobs/aaaaaaaaaaaaaaaaaaaa/render", json={}).status_code == 404


# --------------------------------------------------------------------------- the happy path
def test_a_video_becomes_editable_roman_captions(client, clip):
    job_id = upload(client, clip, language="hinglish")
    job = wait_for(client, job_id)
    assert job["status"] == "ready" and job["stage"] == "Ready" and job["progress"] == 1.0
    assert job["name"] == "reel.mp4" and job["language"] == "hi" and job["model"] == "demo"
    assert (job["info"]["width"], job["info"]["height"]) == (270, 480)
    assert job["preview"] == f"/api/jobs/{job_id}/media"
    assert job["style"]["preset"] == "pumpkin"

    payload = client.get(f"/api/jobs/{job_id}/cards").json()
    texts = [" ".join(w["w"] for w in c["words"]) for c in payload["cards"]]
    assert texts[0].startswith("Namaste doston")                          # romanised, sentence-cased
    assert not re.search("[ऀ-ॿ]", " ".join(texts))             # no Devanagari left anywhere
    assert payload["position"] == {"an": 2, "margin": 0.24}
    for card in payload["cards"]:
        assert card["d"] == [re.sub(r"[.,;:…]+$", "", w["w"]) for w in card["words"]]
        assert [i for line in card["lines"] for i in line] == list(range(len(card["words"])))
        assert 0 < card["scale"] <= 1.0
    starts = [c["start"] for c in payload["cards"]]
    assert starts == sorted(starts) and all(c["end"] <= job["info"]["duration"] + 1e-6 for c in payload["cards"])

    media_resp = client.get(job["preview"])
    assert media_resp.status_code == 200 and len(media_resp.content) > 1000


def test_the_media_preview_supports_range_requests(client, ready_job):
    full = client.get(f"/api/jobs/{ready_job}/media")
    part = client.get(f"/api/jobs/{ready_job}/media", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == full.content[:100]
    assert part.headers["content-range"].startswith("bytes 0-99/")


def test_a_voice_note_works_too_and_the_canvas_sets_the_layout(client, voice_note):
    job_id = upload(client, voice_note, canvas="square")
    job = wait_for(client, job_id)
    assert job["status"] == "ready" and job["info"]["has_video"] is False
    assert job["options"]["canvas"] == "square"
    assert client.get(f"/api/jobs/{job_id}/cards").json()["position"] == {"an": 2, "margin": 0.07}   # not portrait
    portrait = wait_for(client, upload(client, voice_note, canvas="vertical"))["id"]
    assert client.get(f"/api/jobs/{portrait}/cards").json()["position"] == {"an": 2, "margin": 0.24}
    bogus = wait_for(client, upload(client, voice_note, canvas="bogus"))
    assert bogus["options"]["canvas"] == "vertical"


def test_my_words_win_over_everything(client, clip):
    job_id = upload(client, clip, user_words="दोस्तों=dosto\nreels=REELS")
    wait_for(client, job_id)
    flat = [w["w"] for c in client.get(f"/api/jobs/{job_id}/cards").json()["cards"] for w in c["words"]]
    assert "dosto," in flat and "REELS" in flat and "doston," not in flat


def test_my_words_can_name_several_words_and_follow_the_spelling_the_captions_show(client, clip):
    """The demo transcript says "Hinglish captions" in Latin and "दोस्तों," in Devanagari (captioned "doston,")."""
    job_id = upload(client, clip, user_words="hinglish captions=HC\ndoston=dosto")
    wait_for(client, job_id)
    words = [w for c in client.get(f"/api/jobs/{job_id}/cards").json()["cards"] for w in c["words"]]
    flat = [w["w"] for w in words]
    assert "HC" in flat and "dosto," in flat
    assert "Hinglish" not in flat and "captions" not in flat and "doston," not in flat
    joined = next(w for w in words if w["w"] == "HC")
    assert joined["s"] == pytest.approx(4.86, abs=0.05) and joined["e"] == pytest.approx(5.79, abs=0.05)    # both words' time


def test_a_style_chosen_at_upload_is_used_for_the_first_grouping(client, clip):
    job_id = upload(client, clip, style=json.dumps({"preset": "pop", "max_words": 2}))
    job = wait_for(client, job_id)
    assert job["style"]["preset"] == "pop" and job["style"]["max_words"] == 2
    cards = client.get(f"/api/jobs/{job_id}/cards").json()["cards"]
    assert max(len(c["words"]) for c in cards) <= 3
    assert all(w.isupper() or not w.isalpha() for c in cards for w in c["d"])
    ignored = wait_for(client, upload(client, clip, style="{not json"))
    assert ignored["style"]["preset"] == "pumpkin"


# --------------------------------------------------------------------------- editing
def test_fixing_a_spelling_keeps_every_word_timing(client, ready_job):
    """The common edit: same number of words, one respelled. Nothing about the timing may move."""
    cards = client.get(f"/api/jobs/{ready_job}/cards").json()["cards"]
    body = edited(cards)
    tokens = body[1]["text"].split()
    tokens[1] = "Badla"
    body[1]["text"] = " ".join(tokens)
    again = client.post(f"/api/jobs/{ready_job}/sync", json={"cards": body}).json()["cards"]
    assert [w["w"] for w in again[1]["words"]][1] == "Badla" and again[1]["d"][1] == "Badla"
    assert [(w["s"], w["e"]) for w in again[1]["words"]] == [(w["s"], w["e"]) for w in cards[1]["words"]]
    assert [(c["start"], c["end"]) for c in again] == [(c["start"], c["end"]) for c in cards]
    saved = client.get(f"/api/jobs/{ready_job}/cards").json()["cards"]
    assert [w["w"] for w in saved[1]["words"]][1] == "Badla"                          # and it was persisted


def test_adding_a_word_gives_it_a_slot_inside_the_card(client, ready_job):
    cards = client.get(f"/api/jobs/{ready_job}/cards").json()["cards"]
    body = edited(cards)
    body[1]["text"] = body[1]["text"] + " yaar"
    again = client.post(f"/api/jobs/{ready_job}/sync", json={"cards": body}).json()["cards"]
    card = again[1]
    assert [w["w"] for w in card["words"]][-1] == "yaar" and card["d"][-1] == "yaar"
    assert len(card["words"]) == len(cards[1]["words"]) + 1
    spans = [(w["s"], w["e"]) for w in card["words"]]
    assert all(s < e for s, e in spans) and spans == sorted(spans)                  # in order, each with a duration
    assert card["start"] <= spans[0][0] and spans[-1][1] <= card["end"] + 1e-6
    assert (card["start"], card["end"]) == (cards[1]["start"], cards[1]["end"])     # the card itself did not move
    assert client.get(f"/api/jobs/{ready_job}/cards").json()["cards"][1]["words"][-1]["w"] == "yaar"


def test_the_editor_can_split_join_and_delete_cards(client, ready_job):
    cards = client.get(f"/api/jobs/{ready_job}/cards").json()["cards"]
    body = edited(cards)
    n = len(body)
    deleted = client.post(f"/api/jobs/{ready_job}/sync", json={"cards": body[:-1]}).json()["cards"]
    assert len(deleted) == n - 1
    merged = {"start": body[0]["start"], "end": body[1]["end"], "text": body[0]["text"] + " " + body[1]["text"],
              "words": body[0]["words"] + body[1]["words"]}
    joined = client.post(f"/api/jobs/{ready_job}/sync", json={"cards": [merged] + body[2:]}).json()["cards"]
    assert len(joined) == n - 1 and joined[0]["words"][0]["w"] == body[0]["words"][0]["w"]
    assert [c["id"] for c in joined] == list(range(1, len(joined) + 1))


def test_malformed_edits_are_ignored_not_fatal(client, ready_job):
    ok = client.post(f"/api/jobs/{ready_job}/sync", json={"cards": ["x", None, {"start": "a"},
                                                                      {"start": 1, "end": 2, "text": "fine"}]})
    assert ok.status_code == 200 and [c["words"][0]["w"] for c in ok.json()["cards"]] == ["fine"]
    assert client.post(f"/api/jobs/{ready_job}/sync", json={"cards": "nope"}).status_code == 400
    assert client.post(f"/api/jobs/{ready_job}/sync", json={"style": ["nope"]}).status_code == 400
    assert client.post(f"/api/jobs/{ready_job}/sync", content="not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post(f"/api/jobs/{ready_job}/sync", json=[1, 2]).status_code == 400


def test_changing_the_style_re_lays_out_and_is_remembered(client, ready_job):
    style = {"preset": "pop", "position": "top", "case": "upper"}
    got = client.post(f"/api/jobs/{ready_job}/sync", json={"style": style}).json()
    assert got["style"]["preset"] == "pop" and got["position"] == {"an": 8, "margin": 0.11}
    assert all(w == w.upper() for c in got["cards"] for w in c["d"])
    assert client.get(f"/api/jobs/{ready_job}").json()["style"]["position"] == "top"
    junk = client.post(f"/api/jobs/{ready_job}/sync", json={"style": {"font_size": 1e9, "text_color": "nope"}}).json()
    assert junk["style"]["font_size"] == 9.0 and junk["style"]["text_color"] == "#FFFFFF"


def test_regrouping_changes_how_words_are_split_without_losing_any(client, ready_job):
    def flat(cards):
        return [w["w"] for c in cards for w in c["words"]]

    before = client.get(f"/api/jobs/{ready_job}/cards").json()["cards"]
    two = client.post(f"/api/jobs/{ready_job}/regroup", json={"style": {"max_words": 2, "preset": "custom"}}).json()["cards"]
    six = client.post(f"/api/jobs/{ready_job}/regroup", json={"style": {"max_words": 6, "preset": "custom"}}).json()["cards"]
    assert flat(two) == flat(before) == flat(six)
    assert len(two) > len(before) >= len(six)
    assert max(len(c["words"]) for c in two) <= 3
    assert client.post(f"/api/jobs/{ready_job}/regroup", json={"style": 5}).status_code == 400


def test_regrouping_keeps_the_text_the_user_edited(client, ready_job):
    cards = client.get(f"/api/jobs/{ready_job}/cards").json()["cards"]
    body = edited(cards)
    body[0]["text"] = body[0]["text"].replace("doston", "dosto")
    client.post(f"/api/jobs/{ready_job}/sync", json={"cards": body})
    regrouped = client.post(f"/api/jobs/{ready_job}/regroup", json={"style": {"max_words": 3, "preset": "custom"}}).json()
    assert "dosto" in [w["w"].strip(",") for c in regrouped["cards"] for w in c["words"]]


# --------------------------------------------------------------------------- exports
def test_caption_files_download_with_sensible_names_and_types(client, ready_job):
    expected = {"srt": "application/x-subrip", "vtt": "text/vtt", "ass": "text/x-ssa", "txt": "text/plain"}
    for kind, mime in expected.items():
        resp = client.get(f"/api/jobs/{ready_job}/download/{kind}")
        assert resp.status_code == 200 and resp.headers["content-type"].startswith(mime)
        assert resp.headers["content-disposition"] == f'attachment; filename="reel.{kind}"'
    srt = client.get(f"/api/jobs/{ready_job}/download/srt").text
    assert srt.startswith("1\n00:00:00,") and "Namaste doston" in srt
    assert client.get(f"/api/jobs/{ready_job}/download/vtt").text.startswith("WEBVTT")
    ass = client.get(f"/api/jobs/{ready_job}/download/ass").text
    assert "PlayResX: 270" in ass and "PlayResY: 480" in ass and "Dialogue:" in ass
    assert "doston," in client.get(f"/api/jobs/{ready_job}/download/txt").text
    assert client.get(f"/api/jobs/{ready_job}/download/pdf").status_code == 404
    assert client.get(f"/api/jobs/{ready_job}/download/mp4").status_code == 404      # not rendered yet


def test_exports_follow_the_current_style(client, ready_job):
    before = client.get(f"/api/jobs/{ready_job}/download/srt").text
    assert "Namaste" in before
    client.post(f"/api/jobs/{ready_job}/sync", json={"style": {"preset": "pop"}})      # pop = UPPERCASE
    after = client.get(f"/api/jobs/{ready_job}/download/srt").text
    cue_text = [ln for ln in after.splitlines() if ln and not ln.isdigit() and "-->" not in ln]
    assert cue_text and all(ln == ln.upper() for ln in cue_text)
    assert "NAMASTE" in after and "Namaste" not in after


def test_download_names_cannot_inject_headers(client, clip, tmp_path):
    nasty = tmp_path / 'my "best"; reel\r\nX-Evil: 1 (v2).mp4'
    try:
        nasty.write_bytes(clip.read_bytes())
    except OSError:
        pytest.skip("this file system rejects the name")
    job_id = upload(client, nasty)
    wait_for(client, job_id)
    header = client.get(f"/api/jobs/{job_id}/download/srt").headers["content-disposition"]
    assert header.count('"') == 2 and "\r" not in header and "\n" not in header
    assert header.endswith('.srt"')


# --------------------------------------------------------------------------- rendering
@needs_libass
def test_burning_captions_in_and_downloading_the_result(client, ready_job):
    resp = client.post(f"/api/jobs/{ready_job}/render", json={"max_height": 320})
    assert resp.status_code == 200 and resp.json()["task"]["status"] == "working"
    job = wait_task(client, ready_job)
    assert job["task"]["status"] == "done" and job["task"]["url"] == f"/api/jobs/{ready_job}/download/mp4"
    video = client.get(job["task"]["url"])
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    assert video.headers["content-disposition"] == 'attachment; filename="reel-captioned.mp4"'
    out = client.app.state.jobs.job_dir(ready_job) / "captioned.mp4"
    info = media.probe(out)
    assert (info["width"], info["height"]) == (180, 320) and info["has_audio"]


@needs_libass
def test_rendering_at_full_size_uses_the_source_size(client, ready_job):
    client.post(f"/api/jobs/{ready_job}/render", json={})
    wait_task(client, ready_job)
    info = media.probe(client.app.state.jobs.job_dir(ready_job) / "captioned.mp4")
    assert (info["width"], info["height"]) == (270, 480)


def test_render_input_is_validated(client, ready_job):
    assert client.post(f"/api/jobs/{ready_job}/render", json={"max_height": "tall"}).status_code == 400
    assert client.post(f"/api/jobs/{ready_job}/render", json={"max_height": 100}).status_code == 400
    assert client.post(f"/api/jobs/{ready_job}/render", json={"max_height": 99999}).status_code == 400


def test_only_one_render_runs_at_a_time(client, ready_job, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def slow_render(job_dir, source, info, cards, style, **kwargs):
        started.set()
        gate.wait(10)
        (job_dir / "captioned.mp4").write_bytes(b"fake")
        return job_dir / "captioned.mp4"

    monkeypatch.setattr(jobs, "render_video", slow_render)
    assert client.post(f"/api/jobs/{ready_job}/render", json={}).status_code == 200
    assert started.wait(5)
    second = client.post(f"/api/jobs/{ready_job}/render", json={})
    assert second.status_code == 409 and "still running" in second.json()["detail"]
    gate.set()
    assert wait_task(client, ready_job)["task"]["status"] == "done"
    assert client.post(f"/api/jobs/{ready_job}/render", json={}).status_code == 200     # free again


def test_a_failed_render_reports_why(client, ready_job, monkeypatch):
    def broken(*_a, **_k):
        raise media.MediaError("ffmpeg exploded")

    monkeypatch.setattr(jobs, "render_video", broken)
    client.post(f"/api/jobs/{ready_job}/render", json={})
    task = wait_task(client, ready_job)["task"]
    assert task["status"] == "error" and task["message"] == "ffmpeg exploded" and task["url"] == ""


def test_an_unexpected_render_crash_never_hangs_the_ui(client, ready_job, monkeypatch):
    def crash(*_a, **_k):
        raise ZeroDivisionError("oops")

    monkeypatch.setattr(jobs, "render_video", crash)
    client.post(f"/api/jobs/{ready_job}/render", json={})
    task = wait_task(client, ready_job)["task"]
    assert task["status"] == "error" and "ZeroDivisionError" in task["message"]


# --------------------------------------------------------------------------- jobs that are not ready
@pytest.fixture
def slow_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HRC_MOCK_DELAY", "3")
    with TestClient(create_app(make_settings(tmp_path))) as client:
        yield client


def test_nothing_can_be_edited_or_exported_before_the_captions_exist(slow_client, clip):
    job_id = upload(slow_client, clip)
    job = slow_client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] in ("queued", "working")
    for method, path, kw in [("get", "cards", {}), ("post", "sync", {"json": {}}), ("post", "regroup", {"json": {}}),
                             ("post", "render", {"json": {}}), ("get", "download/srt", {})]:
        resp = getattr(slow_client, method)(f"/api/jobs/{job_id}/{path}", **kw)
        assert resp.status_code == 409, (path, resp.text)
        assert "not ready" in resp.json()["detail"]


def test_a_running_job_can_be_cancelled(slow_client, clip):
    job_id = upload(slow_client, clip)
    deadline = time.time() + 10
    while time.time() < deadline and slow_client.get(f"/api/jobs/{job_id}").json()["status"] != "working":
        time.sleep(0.02)
    assert slow_client.post(f"/api/jobs/{job_id}/cancel").json() == {"ok": True}
    job = wait_for(slow_client, job_id, until=("cancelled", "ready", "error"))
    assert job["status"] == "cancelled"


def test_progress_and_stage_are_visible_while_working(slow_client, clip):
    job_id = upload(slow_client, clip)
    seen = set()
    deadline = time.time() + 15
    while time.time() < deadline:
        job = slow_client.get(f"/api/jobs/{job_id}").json()
        seen.add(job["stage"])
        if job["status"] == "ready":
            break
        time.sleep(0.05)
    assert "Ready" in seen and len(seen) >= 3 and any("Demo mode" in s or "Listening" in s for s in seen)


# --------------------------------------------------------------------------- deleting and restarting
def test_deleting_a_project_removes_its_files(client, ready_job):
    folder = client.app.state.jobs.job_dir(ready_job)
    assert folder.exists()
    assert client.delete(f"/api/jobs/{ready_job}").json() == {"ok": True}
    assert not folder.exists()
    assert client.get(f"/api/jobs/{ready_job}").status_code == 404
    assert client.get("/api/jobs").json() == []


def test_projects_survive_a_restart(settings, clip):
    with TestClient(create_app(settings)) as first:
        job_id = upload(first, clip)
        wait_for(first, job_id)
        body = edited(first.get(f"/api/jobs/{job_id}/cards").json()["cards"])
        body[0]["text"] = "Edited by hand"
        first.post(f"/api/jobs/{job_id}/sync", json={"cards": body, "style": {"preset": "box"}})
    with TestClient(create_app(settings)) as second:
        listing = second.get("/api/jobs").json()
        assert [j["id"] for j in listing] == [job_id] and listing[0]["status"] == "ready"
        again = second.get(f"/api/jobs/{job_id}/cards").json()
        assert again["cards"][0]["words"][0]["w"] == "Edited" and again["style"]["preset"] == "box"
        assert second.get(f"/api/jobs/{job_id}/media").status_code == 200


def test_a_job_interrupted_by_a_crash_is_marked_so(settings, clip):
    with TestClient(create_app(settings)) as first:
        job_id = upload(first, clip)
        wait_for(first, job_id)
        folder = first.app.state.jobs.job_dir(job_id)
    meta = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    meta.update(status="working", stage="Listening", task={"kind": "render", "status": "working", "progress": 0.3,
                                                          "message": "x", "file": ""})
    (folder / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    with TestClient(create_app(settings)) as second:
        job = second.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "error" and "closed while this was running" in job["error"]
        assert job["task"]["status"] == "error" and "restart" in job["task"]["message"]


def test_corrupt_job_files_do_not_stop_the_app_from_starting(settings):
    broken = settings.data_dir / "jobs" / "abcdefabcdefabcdefab"
    broken.mkdir(parents=True)
    (broken / "job.json").write_text("{not json")
    (settings.data_dir / "jobs" / "stray-file.txt").write_text("hi")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/jobs").json() == []


# --------------------------------------------------------------------------- optional spelling pass
def test_polish_is_skipped_politely_when_ollama_is_not_running(client, clip):
    job_id = upload(client, clip, polish="1")
    job = wait_for(client, job_id)
    assert job["status"] == "ready" and job["options"]["polish"] is True
    assert any("Polish skipped" in n for n in job["notes"])


def test_polish_uses_a_local_model_and_never_touches_timing(settings, clip, fake_ollama):
    fake_ollama.fixes = {"doston,": "dosto,", "baat": "baat"}
    with TestClient(create_app(replace(settings, ollama_url=fake_ollama.url))) as client:
        plain_id = upload(client, clip)
        wait_for(client, plain_id)
        plain = client.get(f"/api/jobs/{plain_id}/cards").json()["cards"]
        polished_id = upload(client, clip, polish="1")
        job = wait_for(client, polished_id)
        polished = client.get(f"/api/jobs/{polished_id}/cards").json()["cards"]
    assert any(n.startswith("Polished with gemma3:4b") for n in job["notes"])
    assert "dosto," in [w["w"] for c in polished for w in c["words"]]
    assert [(w["s"], w["e"]) for c in polished for w in c["words"]] == [(w["s"], w["e"]) for c in plain for w in c["words"]]


def test_polish_can_use_a_chosen_model(settings, clip, fake_ollama):
    with TestClient(create_app(replace(settings, ollama_url=fake_ollama.url))) as client:
        job = wait_for(client, upload(client, clip, polish="1", ollama_model="llama3.2:3b"))
    assert any("llama3.2:3b" in n for n in job["notes"])
    assert fake_ollama.requests and all(r["model"] == "llama3.2:3b" for r in fake_ollama.requests)


# --------------------------------------------------------------------------- the real Whisper code path
def test_the_whisper_backend_runs_end_to_end_with_a_fake_model(settings, clip, monkeypatch):
    """Everything except the neural network itself: model loading, options, word timestamps, romanising."""
    from types import SimpleNamespace as NS

    class Fake:
        def transcribe(self, audio, **kwargs):
            self.kwargs = kwargs
            words = [("नमस्ते", 0.3, 0.9), ("दोस्तों,", 1.0, 1.6), ("आज", 1.8, 2.1), ("reels", 2.2, 2.7),
                     ("में", 2.8, 3.0), ("captions", 3.1, 3.8), ("लगाओ।", 3.9, 4.5)]
            seg = NS(start=0.3, end=4.5, text="", words=[NS(word=" " + w, start=s, end=e, probability=0.9) for w, s, e in words])
            return iter([seg]), NS(language="hi", duration=6.0)

    fake = Fake()
    monkeypatch.setattr(asr, "load_whisper", lambda model, st, status=asr._noop: (status("Loading the speech model"), fake)[1])
    whisper_settings = replace(settings, asr_backend="faster-whisper")
    with TestClient(create_app(whisper_settings)) as client:
        job = wait_for(client, upload(client, clip, language="hinglish", hints="Gemma, Render", model="small"))
        assert job["status"] == "ready" and job["language"] == "hi" and job["model"] == "small"
        cards = client.get(f"/api/jobs/{job['id']}/cards").json()["cards"]
    spoken = " ".join(w["w"] for c in cards for w in c["words"])
    assert spoken == "Namaste doston, aaj reels mein captions lagao."
    assert fake.kwargs["language"] == "hi" and fake.kwargs["hotwords"] == "Gemma, Render"
    assert fake.kwargs["word_timestamps"] is True and fake.kwargs["vad_filter"] is True


def test_a_whisper_failure_is_reported_not_raised(settings, clip, monkeypatch):
    def fail(model, st, status=asr._noop):
        raise asr.ModelError("Could not load the speech model 'small': offline. The first run needs internet")

    monkeypatch.setattr(asr, "load_whisper", fail)
    with TestClient(create_app(replace(settings, asr_backend="faster-whisper"))) as client:
        job = wait_for(client, upload(client, clip))
    assert job["status"] == "error" and "first run needs internet" in job["error"]


def test_silence_is_reported_as_no_speech(settings, clip, monkeypatch):
    from types import SimpleNamespace as NS

    class Silent:
        def transcribe(self, audio, **kwargs):
            return iter([]), NS(language="hi", duration=6.0)

    monkeypatch.setattr(asr, "load_whisper", lambda model, st, status=asr._noop: Silent())
    with TestClient(create_app(replace(settings, asr_backend="faster-whisper"))) as client:
        job = wait_for(client, upload(client, clip))
    assert job["status"] == "error" and "No speech was found" in job["error"]
