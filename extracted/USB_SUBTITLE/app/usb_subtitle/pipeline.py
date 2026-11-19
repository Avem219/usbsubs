"""Video -> English cues -> validated SRT. No database access here."""
import os, shutil, tempfile
from dataclasses import dataclass
from pathlib import Path

from . import srt
from .fsinfo import human
from .media import MediaError, extract_audio, find_ffmpeg, probe, wav_duration


class SubtitleError(RuntimeError):
    pass


@dataclass
class Result:
    cues: list
    language: str
    language_prob: float
    task: str
    duration: float


def subtitle_candidates(video: Path):
    base = video.with_suffix("")
    return [base.with_name(base.name + ".srt"), base.with_name(base.name + ".en.srt"),
            base.with_name(base.name + ".eng.srt")]


def find_existing_english(video: Path):
    """Return the first existing, valid, English-looking subtitle beside the video, else None."""
    for c in subtitle_candidates(video):
        if c.exists():
            ok, _, _ = srt.validate(c)
            if ok and srt.looks_english(srt.parse(c)):
                return c
    return None


class Pipeline:
    def __init__(self, settings, workspace, transcriber, log):
        self.s, self.ws, self.tr, self.log = settings, workspace, transcriber, log

    def temp_base(self):
        if self.s["temp_location"] == "usb":
            self.ws.temp_dir.mkdir(parents=True, exist_ok=True)
            return str(self.ws.temp_dir)
        return None  # system temp on the host

    def purge_stale_temp(self):
        try:
            for p in self.ws.temp_dir.glob("usbsub_*"):
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass

    def generate(self, video: Path, progress=None, cancel=None) -> Result:
        """progress(stage:str, fraction:float)"""
        p = progress or (lambda *_: None)
        p("Checking file", 0.0)
        getattr(self.tr, "load", lambda: None)()      # fail fast if the model is missing, before any heavy work
        ffmpeg = find_ffmpeg()
        try:
            info = probe(video, ffmpeg)
        except MediaError as e:
            raise SubtitleError(str(e))
        if info.audio_streams == 0:
            raise SubtitleError("video has no audio track")
        if self.s["audio_track"] >= info.audio_streams:
            raise SubtitleError(f"audio_track {self.s['audio_track']} does not exist (file has {info.audio_streams})")
        tmp = tempfile.mkdtemp(prefix="usbsub_", dir=self.temp_base())
        try:
            if info.duration:
                need = int(info.duration * 32000 * 1.1)
                free = shutil.disk_usage(tmp).free
                if free < need:
                    raise SubtitleError(f"not enough temp space ({human(free)} free, {human(need)} needed)")
            wav = os.path.join(tmp, "audio.wav")
            p("Extracting audio", 0.0)
            extract_audio(video, wav, self.s["audio_track"], info.duration,
                          lambda f: p("Extracting audio", f), cancel, ffmpeg)
            total = wav_duration(wav)
            if total < 2:
                raise SubtitleError("audio is empty")
            p("Detecting language", 0.0)
            lang, prob = self.tr.detect_language(wav, total)
            self.log.info("%s: language=%s (%.0f%%)", video.name, lang, prob * 100)
            stage = "Transcribing" if lang == "en" else f"Translating {lang} -> English"
            p(stage, 0.0)
            cues, task = self.tr.transcribe(wav, total, lang, lambda f: p(stage, f), cancel)
            cues = srt.normalize(cues)
            if not cues:
                raise SubtitleError("no speech detected")
            return Result(cues, lang, prob, task, total)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def target_for(self, video: Path) -> Path:
        main = subtitle_candidates(video)[0]
        if not main.exists() or self.s["overwrite_existing"]:
            return main
        self.log.warning("%s exists (not English/valid) - writing %s instead of overwriting",
                         main.name, subtitle_candidates(video)[1].name)
        return subtitle_candidates(video)[1]

    def publish(self, video: Path, cues, target: Path = None) -> Path:
        target = target or self.target_for(video)
        srt.write_srt(target, cues, bom=self.s["utf8_bom"])
        ok, msg, n = srt.validate(target)
        if not ok:
            try:
                target.unlink()
            except OSError:
                pass
            raise SubtitleError(f"generated SRT failed validation: {msg}")
        self.log.info("Wrote %s (%d cues)", target.name, n)
        return target
