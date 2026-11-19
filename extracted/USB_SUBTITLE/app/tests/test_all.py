import os, shutil, subprocess, threading, time
from pathlib import Path
import pytest

from usb_subtitle import srt
from usb_subtitle.config import Settings
from usb_subtitle.db import Database
from usb_subtitle.engine import Engine, iter_videos
from usb_subtitle.media import Cancelled, extract_audio, find_ffmpeg, probe, wav_duration
from usb_subtitle.paths import RootNotFound, Workspace, find_root
from usb_subtitle.pipeline import Result, find_existing_english

ENGLISH = srt.Cue(0, 5, "I don't know what you are talking about, but this is the thing that we have to do now")
ENG_LONG = [srt.Cue(i * 6, i * 6 + 5, "You know that this is the one thing that they were going to do with the money") for i in range(5)]


class FakeTranscriber:
    """Stands in for faster-whisper so pipeline logic can be tested without a model."""
    def __init__(self, fail=None, lang="es"):
        self.fail, self.lang, self.calls = fail, lang, 0
    def unload(self): pass
    def detect_language(self, wav, total): return self.lang, 0.93
    def transcribe(self, wav, total, lang, progress=None, cancel=None):
        self.calls += 1
        if self.fail: raise self.fail
        if progress: progress(1.0)
        return [srt.Cue(1, 3, "Hello there, how are you?"), srt.Cue(4, 6.5, "I am fine, thank you.")], "translate"


@pytest.fixture
def usb(tmp_path):
    root = tmp_path / "USB"
    (root / "USB_SUBTITLE").mkdir(parents=True)
    ws = Workspace(root); ws.ensure()
    s = Settings(ws.settings_path); s["temp_location"] = "usb"
    return ws, s


def make_video(path, seconds=4, audio=True):
    ff = find_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-pix_fmt", "yuv420p", "-y", str(path)]
    subprocess.run(cmd, check=True)


needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="no ffmpeg available")


# ---------- SRT ----------
def test_srt_roundtrip_and_validation(tmp_path):
    cues = srt.normalize([srt.Cue(0, 2, "Hello"), srt.Cue(2.0, 4.5, "World")])
    p = tmp_path / "a.srt"; srt.write_srt(p, cues)
    assert srt.validate(p) == (True, "ok", 2)
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"00:00:00,000 --> 00:00:02,000" in raw


def test_srt_bom_and_utf8(tmp_path):
    p = tmp_path / "a.srt"; srt.write_srt(p, [srt.Cue(0, 1, "caf\u00e9 \u2013 \u00fcber")], bom=True)
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    assert srt.parse(p)[0].text == "caf\u00e9 \u2013 \u00fcber"


def test_srt_rejects_garbage(tmp_path):
    p = tmp_path / "bad.srt"
    p.write_text("not a subtitle")
    assert not srt.validate(p)[0]
    p.write_bytes(b"1\n00:00:01,000 --> 00:00:00,500\nx\n\n")
    assert not srt.validate(p)[0]
    p.write_bytes(b"\xff\xfe\x00bad")
    assert not srt.validate(p)[0]
    p.write_text("")
    assert not srt.validate(p)[0]


def test_normalize_fixes_overlap_wraps_and_dedups():
    cues = srt.normalize([srt.Cue(0, 5, "a " * 60), srt.Cue(4, 4.1, "Next"), srt.Cue(5, 6, "Next")])
    assert all(len(l) <= 42 for c in cues for l in c.text.split("\n"))
    assert all(len(c.text.split("\n")) <= 2 for c in cues)
    assert all(b.start >= a.end - 1e-9 for a, b in zip(cues, cues[1:]))
    assert sum(c.text == "Next" for c in cues) == 1


def test_looks_english():
    assert srt.looks_english(ENG_LONG)
    es = [srt.Cue(0, 1, "No sé qué quieres decir con eso, pero esto es lo que tenemos que hacer ahora mismo señor") for _ in range(4)]
    fr = [srt.Cue(0, 1, "Je ne sais pas ce que tu veux dire mais c'est ce que nous devons faire maintenant avec lui") for _ in range(4)]
    ru = [srt.Cue(0, 1, "Я не знаю что ты имеешь в виду но это то что мы должны сделать сейчас") for _ in range(4)]
    assert not srt.looks_english(es) and not srt.looks_english(fr) and not srt.looks_english(ru)


# ---------- paths ----------
def test_root_detection_and_relative_paths(tmp_path):
    root = tmp_path / "Some Drive"
    deep = root / "USB_SUBTITLE" / "app" / "usb_subtitle" / "x.py"
    deep.parent.mkdir(parents=True); deep.write_text("")
    assert find_root(start=deep) == root.resolve()
    ws = Workspace(root)
    f = root / "MOVIES" / "Series" / "ep 1.mkv"
    assert ws.rel(f) == "MOVIES/Series/ep 1.mkv"
    assert ws.abs("MOVIES/Series/ep 1.mkv") == root / "MOVIES" / "Series" / "ep 1.mkv"


def test_root_not_found(tmp_path):
    f = tmp_path / "a" / "b.py"; f.parent.mkdir(); f.write_text("")
    with pytest.raises(RootNotFound):
        find_root(start=f)


# ---------- settings ----------
def test_settings_validation_and_corrupt_file(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings(p)
    s.set("model", "base"); assert Settings(p)["model"] == "base"
    with pytest.raises(ValueError): s.set("model", "huge")
    with pytest.raises(ValueError): s.set("output_language", "fr")
    p.write_text("{broken")
    assert Settings(p)["model"] == "tiny"


# ---------- database ----------
def test_db_recovery_and_dedup(tmp_path):
    db = Database(tmp_path / "h.db")
    db.enqueue("MOVIES/a.mkv", 10, 5, "tiny")
    db.enqueue("MOVIES/a.mkv", 10, 5, "tiny")           # duplicate -> still one row
    assert len(db.by_status("pending")) == 1
    j = db.claim_next(); assert j.status == "pending" and db.claim_next() is None
    assert db.reset_interrupted() == 1                   # simulated crash / unplug
    assert db.claim_next().rel_path == "MOVIES/a.mkv"
    db.set_status("MOVIES/a.mkv", "failed", "x", True)
    assert db.retry_failed() == 1


# ---------- scanner ----------
def test_nested_detection_and_ignored_files(usb):
    ws, s = usb
    for n in ("m1.mkv", "Series/S1/e1.MP4", "Series/e2.webm", "x.avi", "y.mov", "notes.txt", "._m1.mkv",
              "m1.mkv.usbsub-part", ".hidden/z.mkv"):
        p = ws.movies_dir / n; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"x" * 10)
    found = sorted(p.relative_to(ws.movies_dir).as_posix() for p in iter_videos(ws.movies_dir, set(s["video_extensions"])))
    assert found == ["Series/S1/e1.MP4", "Series/e2.webm", "m1.mkv", "x.avi", "y.mov"]


def test_stability_wait_for_transfer(usb):
    ws, s = usb; s["stable_seconds"] = 0.5
    eng = Engine(ws, s, FakeTranscriber())
    f = ws.movies_dir / "copying.mkv"; f.write_bytes(b"a" * 20000)
    assert eng.scan_once() == 0                          # first sighting: never process
    f.write_bytes(b"a" * 40000)                          # still growing
    assert eng.scan_once() == 0
    time.sleep(0.6)
    assert eng.scan_once() == 1                          # unchanged long enough
    assert eng.scan_once() == 0                          # not queued twice
    assert len(eng.db.by_status("pending")) == 1
    eng.db.close()


# ---------- existing subtitles ----------
def test_existing_subtitle_detection_and_skip(usb):
    ws, s = usb
    v = ws.movies_dir / "film.mkv"; v.write_bytes(b"x" * 10)
    srt.write_srt(ws.movies_dir / "film.srt", srt.normalize(ENG_LONG))
    assert find_existing_english(v) is not None
    eng = Engine(ws, s, FakeTranscriber())
    eng.scan_once(assume_stable=True)
    assert eng.db.by_status("pending") == [] and len(eng.db.by_status("skipped_existing")) == 1
    eng.db.close()


def test_invalid_or_foreign_subtitle_is_not_overwritten(usb):
    ws, s = usb
    v = ws.movies_dir / "film.mkv"
    es = [srt.Cue(i * 6, i * 6 + 5, "No sé qué quieres decir con eso, pero esto es lo que tenemos que hacer ahora mismo") for i in range(5)]
    srt.write_srt(ws.movies_dir / "film.srt", es)
    eng = Engine(ws, s, FakeTranscriber())
    assert eng.pipe.target_for(v).name == "film.en.srt"
    s["overwrite_existing"] = True
    assert eng.pipe.target_for(v).name == "film.srt"
    eng.db.close()


# ---------- full pipeline with real FFmpeg, fake speech model ----------
@needs_ffmpeg
def test_probe_and_extract(tmp_path):
    v = tmp_path / "t.mp4"; make_video(v, 4)
    info = probe(v); assert info.audio_streams == 1 and 3 < info.duration < 5
    wav = tmp_path / "a.wav"; extract_audio(v, wav, 0, info.duration)
    assert 3.5 < wav_duration(wav) < 4.5
    mute = tmp_path / "mute.mp4"; make_video(mute, 2, audio=False)
    assert probe(mute).audio_streams == 0


@needs_ffmpeg
def test_end_to_end_with_fake_model_nested_and_duplicates(usb):
    ws, s = usb
    v = ws.movies_dir / "Series" / "Episode 01.mkv"; make_video(v, 4)
    fake = FakeTranscriber()
    eng = Engine(ws, s, fake)
    eng.drain()
    out = v.with_suffix(".srt")
    assert out.exists() and srt.validate(out)[0]
    assert "Hello there" in out.read_text(encoding="utf-8")
    j = eng.db.get("MOVIES/Series/Episode 01.mkv")
    assert j.status == "done" and j.language == "es" and j.subtitle_rel == "MOVIES/Series/Episode 01.srt"
    assert fake.calls == 1
    eng.drain()                                           # duplicate detection
    assert fake.calls == 1
    out.unlink(); eng.drain()                             # deleting the SRT regenerates it
    assert fake.calls == 2
    assert not list(ws.temp_dir.glob("usbsub_*"))         # temp files cleaned
    eng.db.close()


@needs_ffmpeg
def test_no_audio_fails_cleanly(usb):
    ws, s = usb
    make_video(ws.movies_dir / "silent.mp4", 2, audio=False)
    eng = Engine(ws, s, FakeTranscriber()); eng.drain()
    j = eng.db.get("MOVIES/silent.mp4")
    assert j.status == "failed" and "no audio" in j.error
    assert not (ws.movies_dir / "silent.srt").exists()
    eng.db.close()


@needs_ffmpeg
def test_corrupt_and_incomplete_files_not_processed(usb):
    ws, s = usb
    (ws.movies_dir / "bad.mkv").write_bytes(os.urandom(50000))
    eng = Engine(ws, s, FakeTranscriber()); eng.drain()
    assert eng.db.get("MOVIES/bad.mkv").status == "failed"
    assert not (ws.movies_dir / "bad.srt").exists()
    eng.db.close()


@needs_ffmpeg
def test_usb_disconnect_and_recovery(usb):
    ws, s = usb
    v = ws.movies_dir / "film.mp4"; make_video(v, 3)
    eng = Engine(ws, s, FakeTranscriber())
    eng.scan_once(assume_stable=True)
    job = eng.db.get("MOVIES/film.mp4"); assert job.status == "pending"
    # Simulate unplug: workspace disappears while a job is mid-flight
    real = ws.is_available
    ws.is_available = lambda: False
    assert eng.process_next() is False and eng.disconnected
    ws.is_available = real
    eng.db.claim_next()                                   # job left in 'processing' as if the app was killed
    assert eng.db.get("MOVIES/film.mp4").status == "processing"
    eng.db.close()
    eng2 = Engine(ws, s, FakeTranscriber())               # new run after reconnect
    eng2.start(); time.sleep(0.1)
    deadline = time.time() + 20
    while time.time() < deadline and not v.with_suffix(".srt").exists():
        time.sleep(0.3)
    eng2.stop()
    assert v.with_suffix(".srt").exists()


@needs_ffmpeg
def test_failure_in_model_is_retried_then_fails(usb):
    ws, s = usb; s["max_attempts"] = 2
    make_video(ws.movies_dir / "f.mp4", 3)
    eng = Engine(ws, s, FakeTranscriber(fail=RuntimeError("boom")))
    eng.drain()
    assert eng.db.get("MOVIES/f.mp4").status == "failed"
    assert eng.db.get("MOVIES/f.mp4").attempts == 2
    assert eng.db.retry_failed() == 1
    eng.db.close()


@needs_ffmpeg
def test_cancel(usb):
    ws, s = usb
    make_video(ws.movies_dir / "c.mp4", 3)
    eng = Engine(ws, s, FakeTranscriber(fail=Cancelled()))
    eng.cancel_reason = "user"; eng.drain()
    assert eng.db.get("MOVIES/c.mp4").status in ("cancelled", "pending")
    eng.db.close()


def test_paths_with_spaces_and_unicode(usb):
    ws, s = usb
    p = ws.movies_dir / "Série Ünï 日本語" / "Épisode 1 (2020).mkv"
    p.parent.mkdir(); p.write_bytes(b"x")
    rel = ws.rel(p)
    assert "\\" not in rel and ws.abs(rel) == p


def test_split_points_prefer_silence(tmp_path):
    import wave, numpy as np
    sr = 16000
    t = np.arange(sr * 100) / sr
    a = (0.3 * np.sin(2 * np.pi * 300 * t)).astype("float32")
    a[int(41.0 * sr):int(41.6 * sr)] = 0          # a silent gap near the 40 s target
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((a * 32767).astype("int16").tobytes())
    from usb_subtitle.transcribe import split_points
    pts = split_points(p, 100.0, 40.0)
    assert pts[0] == 0 and pts[-1] == 100.0 and 40.9 <= pts[1] <= 41.7


def test_missing_model_keeps_job_pending_and_offline_message(usb):
    ws, s = usb
    s["allow_model_download"] = False
    s["model_location"] = "portable"
    from usb_subtitle.transcribe import Transcriber
    from usb_subtitle.engine import make_logger
    if find_ffmpeg() is None:
        pytest.skip("no ffmpeg")
    make_video(ws.movies_dir / "m.mp4", 3)
    eng = Engine(ws, s, Transcriber(s, ws, make_logger(ws)))
    eng.drain()
    assert eng.fatal and "not cached" in eng.fatal
    assert eng.db.get("MOVIES/m.mp4").status == "pending"      # nothing lost, retried once the model exists
    eng.db.close()
