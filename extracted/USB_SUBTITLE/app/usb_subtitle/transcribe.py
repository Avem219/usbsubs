"""faster-whisper wrapper: language detection + chunked transcription/translation to English."""
import os, threading, wave
from collections import defaultdict
from typing import Callable, Optional

from .media import Cancelled
from .srt import Cue

SR = 16000


class ModelUnavailable(RuntimeError):
    pass


def read_wav(path, start_s, dur_s):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        w.setpos(min(int(start_s * SR), w.getnframes()))
        raw = w.readframes(int(dur_s * SR))
    return np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0


def split_points(path, total_s, chunk_s):
    """Chunk boundaries near the target length, nudged to the quietest 0.25 s within +-15 s."""
    import numpy as np
    pts, t = [0.0], 0.0
    while total_s - t > chunk_s * 1.3:
        target = t + chunk_s
        a = read_wav(path, target - 15, 30)
        n = int(0.25 * SR)
        frames = len(a) // n
        if frames > 2:
            rms = np.sqrt((a[:frames * n].reshape(frames, n) ** 2).mean(axis=1))
            target = target - 15 + (int(rms.argmin()) + 0.5) * 0.25
        pts.append(target)
        t = target
    pts.append(total_s)
    return pts


class Transcriber:
    def __init__(self, settings, workspace, log):
        self.s, self.ws, self.log = settings, workspace, log
        self.model = None
        self.loaded_name = None

    def _cache_dir(self):
        if self.s["model_location"] == "portable":
            self.ws.models_dir.mkdir(parents=True, exist_ok=True)
            return str(self.ws.models_dir)
        return None

    def is_cached(self, name=None) -> bool:
        try:
            from faster_whisper.utils import download_model
            download_model(name or self.s["model"], cache_dir=self._cache_dir(), local_files_only=True)
            return True
        except Exception:
            return False

    def download(self, name=None):
        from faster_whisper.utils import download_model
        return download_model(name or self.s["model"], cache_dir=self._cache_dir())

    def load(self):
        name = self.s["model"]
        if self.model is not None and self.loaded_name == name:
            return
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
        except ImportError as e:
            raise ModelUnavailable("faster-whisper is not installed on this computer. Run the launcher again "
                                   "or see README (Setup).") from e
        cache = self._cache_dir()
        try:
            path = download_model(name, cache_dir=cache, local_files_only=True)
        except Exception:
            if not self.s["allow_model_download"]:
                raise ModelUnavailable(f"Model '{name}' is not cached on this computer and downloads are disabled. "
                                       f"Run once with internet: python -m usb_subtitle download-model {name}")
            self.log.info("Model %s not cached; downloading (one-time, needs internet)", name)
            try:
                path = download_model(name, cache_dir=cache)
            except Exception as e:
                raise ModelUnavailable(f"Model '{name}' is not cached and could not be downloaded ({e}). "
                                       f"Connect to the internet once and run: python -m usb_subtitle download-model {name}") from e
        threads = self.s["cpu_threads"] or max(1, (os.cpu_count() or 2) // 2)
        for ct in dict.fromkeys([self.s["compute_type"], "int8", "float32"]):
            try:
                self.model = WhisperModel(path, device=self.s["device"], compute_type=ct, cpu_threads=threads)
                self.loaded_name = name
                self.log.info("Loaded model %s (%s, %s threads)", name, ct, threads)
                return
            except ValueError as e:
                self.log.warning("compute_type %s unsupported here: %s", ct, e)
        raise ModelUnavailable("Could not initialise the speech model on this computer.")

    def unload(self):
        self.model = None
        self.loaded_name = None

    def detect_language(self, wav, total_s):
        """Vote over up to 4 non-silent 30 s windows spread through the film."""
        import numpy as np
        self.load()
        votes, windows = defaultdict(float), 0
        for frac in (0.1, 0.35, 0.6, 0.85):
            a = read_wav(wav, max(0.0, total_s * frac), 30)
            if len(a) < SR * 3 or float(np.sqrt((a ** 2).mean())) < 0.005:
                continue
            res = self.model.detect_language(audio=a)
            probs = res[2] if len(res) > 2 and res[2] else [(res[0], res[1])]
            for lang, p in probs:
                votes[lang] += float(p)
            windows += 1
        if not windows:
            return "en", 0.0
        lang = max(votes, key=votes.get)
        return lang, votes[lang] / windows

    def transcribe(self, wav, total_s, lang, progress: Optional[Callable] = None,
                   cancel: Optional[threading.Event] = None):
        """English cues. Non-English speech uses Whisper's built-in translate task; English uses transcribe."""
        self.load()
        task = "transcribe" if lang == "en" else "translate"
        pts = split_points(wav, total_s, self.s["chunk_minutes"] * 60)
        cues = []
        for a_s, b_s in zip(pts, pts[1:]):
            audio = read_wav(wav, a_s, b_s - a_s)
            if len(audio) < SR // 2:
                continue
            segs, _ = self.model.transcribe(
                audio, language=lang, task=task, beam_size=self.s["beam_size"],
                vad_filter=self.s["vad_filter"], condition_on_previous_text=False)
            for seg in segs:                       # lazy generator: decoding happens here
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if seg.no_speech_prob > 0.8 and seg.avg_logprob < -1.0:
                    continue
                cues.append(Cue(a_s + seg.start, a_s + seg.end, seg.text))
                if progress:
                    progress(min(1.0, (a_s + seg.end) / total_s))
            if progress:
                progress(min(1.0, b_s / total_s))
        return cues, task
