"""settings.json handling (lives on the USB, contains only relative / portable values)."""
import json, os, shutil, time
from pathlib import Path

DEFAULTS = {
    "movies_folder": "MOVIES",          # relative to the USB root
    "model": "tiny",                    # tiny | base | small
    "device": "cpu",
    "compute_type": "int8",
    "cpu_threads": 0,                   # 0 = automatic (half the cores)
    "output_language": "en",            # only "en" is supported in v1
    "beam_size": 1,                     # 1 = fastest; 3-5 = slightly better, slower
    "vad_filter": True,                 # skip silence, fewer hallucinated lines
    "audio_track": 0,                   # which audio stream to transcribe
    "chunk_minutes": 10,                # audio is processed in chunks of this length
    "scan_interval_seconds": 10,
    "stable_seconds": 20,               # file size/mtime must be unchanged this long
    "max_attempts": 2,
    "overwrite_existing": False,        # never replace a valid subtitle unless true
    "utf8_bom": False,                  # some old TVs need a BOM; most do not
    "model_location": "host",           # host = cache on the computer, portable = USB_SUBTITLE/models
    "allow_model_download": True,       # set false for strict offline
    "temp_location": "host",            # host = system temp, usb = USB_SUBTITLE/temp
    "video_extensions": [".mp4", ".mkv", ".avi", ".mov", ".webm"],
}
MODELS = ("tiny", "base", "small")


def _coerce(key, value):
    d = DEFAULTS[key]
    if isinstance(d, bool):
        return bool(value)
    if isinstance(d, int):
        return int(value)
    if isinstance(d, list):
        return [str(x).lower() if str(x).startswith(".") else "." + str(x).lower() for x in value]
    return str(value)


def validate(s: dict) -> dict:
    if s["model"] not in MODELS:
        raise ValueError(f"model must be one of {MODELS}")
    if s["output_language"] != "en":
        raise ValueError("Only English output ('en') is supported in this version. "
                         "Other languages would need extra translation models (see README, Future work).")
    if s["model_location"] not in ("host", "portable") or s["temp_location"] not in ("host", "usb"):
        raise ValueError("model_location must be host|portable and temp_location host|usb")
    for k in ("chunk_minutes", "beam_size", "max_attempts"):
        if s[k] < 1:
            raise ValueError(f"{k} must be >= 1")
    if s["stable_seconds"] < 0 or s["scan_interval_seconds"] < 1:
        raise ValueError("invalid timing values")
    return s


class Settings(dict):
    def __init__(self, path: Path):
        super().__init__(DEFAULTS)
        self.path = Path(path)
        self.load()

    def load(self):
        if not self.path.exists():
            self.save()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            merged = dict(DEFAULTS)
            for k, v in data.items():
                if k in DEFAULTS:
                    merged[k] = _coerce(k, v)
            self.update(validate(merged))
        except (ValueError, OSError, TypeError) as e:
            bad = self.path.with_suffix(f".bad-{int(time.time())}")
            try:
                shutil.copy2(self.path, bad)
            except OSError:
                pass
            print(f"[warning] settings.json invalid ({e}); defaults loaded, copy kept as {bad.name}")
            self.update(DEFAULTS)

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        new = dict(self)
        new[key] = _coerce(key, value)
        self.update(validate(new))
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
