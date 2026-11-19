"""Scanner + sequential worker. State lives in SQLite so every job survives restarts and unplugging."""
import logging, logging.handlers, os, threading, time
from pathlib import Path

from . import srt
from .db import Database
from .media import Cancelled
from .pipeline import Pipeline, SubtitleError, find_existing_english, subtitle_candidates
from .transcribe import ModelUnavailable, Transcriber


def make_logger(ws):
    log = logging.getLogger("usb_subtitle")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        ws.log_dir.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(ws.log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    log.addHandler(h)
    return log


def iter_videos(folder: Path, exts):
    """Recursive walk; ignores hidden/macOS '._' files, our own part files and the workspace."""
    for dp, dns, fns in os.walk(folder):
        dns[:] = [d for d in dns if not d.startswith(".") and d.upper() != "USB_SUBTITLE"]
        for fn in fns:
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() in exts:
                yield Path(dp) / fn


def is_readable(path: Path) -> bool:
    """Opens the file and reads its first and last bytes (fails if still locked/being written)."""
    try:
        with open(path, "rb") as f:
            f.read(4096)
            size = os.fstat(f.fileno()).st_size
            if size > 8192:
                f.seek(size - 4096)
                f.read(4096)
        return True
    except OSError:
        return False


class Engine:
    def __init__(self, ws, settings, transcriber=None, on_event=None):
        self.ws, self.s = ws, settings
        self.log = make_logger(ws)
        self.db = Database(ws.db_path)
        self.tr = transcriber or Transcriber(settings, ws, self.log)
        self.pipe = Pipeline(settings, ws, self.tr, self.log)
        self.on_event = on_event or (lambda *a: None)
        self.stop_evt = threading.Event()
        self.cancel_evt = threading.Event()
        self.cancel_reason = None
        self.threads = []
        self.current = None            # {"name","stage","progress"}
        self.disconnected = False
        self.fatal = None              # e.g. model unavailable
        self._seen = {}
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        n = self.db.reset_interrupted()
        if n:
            self.log.info("Recovered %d interrupted job(s)", n)
        self.pipe.purge_stale_temp()
        for fn in (self._scan_loop, self._worker_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_evt.set()
        self.cancel_reason = "shutdown"
        self.cancel_evt.set()
        for t in self.threads:
            t.join(timeout=20)
        self.db.close()

    def cancel_current(self):
        if self.current:
            self.cancel_reason = "user"
            self.cancel_evt.set()
            return True
        return False

    # ---- availability / disconnection -------------------------------------
    def check_available(self) -> bool:
        with self._lock:
            if not self.ws.is_available():
                if not self.disconnected:
                    self.disconnected = True
                    self.log.warning("USB drive disconnected - pausing; pending jobs are kept")
                    self.on_event("disconnected")
                return False
            if self.disconnected:
                try:
                    self.db.reopen()
                    self.db.reset_interrupted()
                except Exception as e:
                    self.log.warning("Drive back but database not ready yet: %s", e)
                    return False
                self.disconnected = False
                self._seen.clear()
                self.log.info("USB drive reconnected - resuming")
                self.on_event("reconnected")
            return True

    # ---- scanning ----------------------------------------------------------
    def scan_once(self, assume_stable=False) -> int:
        """Detect new/changed videos. Returns the number queued or skipped this pass."""
        exts = set(self.s["video_extensions"])
        now, changed, live = time.monotonic(), 0, set()
        folder = self.ws.movies_dir
        if not folder.is_dir():
            return 0
        for path in iter_videos(folder, exts):
            key = str(path)
            live.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime_ns)
            prev = self._seen.get(key)
            if prev is None or prev[0] != sig:
                self._seen[key] = (sig, now)
                if not assume_stable:
                    continue
                prev = self._seen[key]
            if not assume_stable and now - prev[1] < self.s["stable_seconds"]:
                continue
            if st.st_size == 0:
                continue
            if self._consider(path, st):
                changed += 1
        for k in set(self._seen) - live:
            self._seen.pop(k, None)
        return changed

    def _consider(self, path: Path, st):
        """True if something was queued/skipped this call. Cheap when the video is already known."""
        rel = self.ws.rel(path)
        rec = self.db.get(rel)
        same = rec and rec.size == st.st_size and rec.mtime_ns == st.st_mtime_ns
        if same:
            if rec.status in ("pending", "processing", "cancelled"):
                return False
            if rec.status == "skipped_existing" and rec.subtitle_rel and self.ws.abs(rec.subtitle_rel).exists():
                return False
            if rec.status == "failed" and rec.attempts >= self.s["max_attempts"]:
                return False
            if rec.status == "done" and rec.subtitle_rel and self.ws.abs(rec.subtitle_rel).exists():
                return False                # delete the .srt to regenerate it
        existing = find_existing_english(path)
        if existing is not None and not (same and rec.status == "failed"):
            self.db.record_skipped(rel, st.st_size, st.st_mtime_ns, self.ws.rel(existing))
            self.log.info("Skipping %s: valid English subtitle already present (%s)", path.name, existing.name)
            self.on_event("skipped", path.name)
            return True
        if not is_readable(path):
            return False                    # still locked / being written: look again next scan
        self.db.enqueue(rel, st.st_size, st.st_mtime_ns, self.s["model"])
        self.log.info("Queued %s", rel)
        self.on_event("queued", path.name)
        return True

    def _scan_loop(self):
        while not self.stop_evt.is_set():
            try:
                if self.check_available():
                    self.scan_once()
            except Exception as e:
                self.log.exception("scan error: %s", e)
            self.stop_evt.wait(self.s["scan_interval_seconds"])

    # ---- processing ----------------------------------------------------------
    def _progress(self, name):
        def cb(stage, frac):
            self.current = {"name": name, "stage": stage, "progress": frac}
            self.on_event("progress", self.current)
        return cb

    def process_next(self) -> bool:
        """Process one pending job. Returns False when nothing was pending."""
        if not self.check_available():
            return False
        try:
            job = self.db.claim_next()
        except Exception as e:
            self.log.warning("db error while claiming job: %s", e)
            return False
        if not job:
            return False
        path = self.ws.abs(job.rel_path)
        self.cancel_evt.clear(); self.cancel_reason = None
        self.current = {"name": path.name, "stage": "Starting", "progress": 0.0}
        self.log.info("Processing %s (model %s)", job.rel_path, self.s["model"])
        try:
            st = path.stat()
            res = self.pipe.generate(path, self._progress(path.name), self.cancel_evt)
            st2 = path.stat()
            if (st.st_size, st.st_mtime_ns) != (st2.st_size, st2.st_mtime_ns):
                self.db.set_status(job.rel_path, "pending")
                self.log.info("%s changed while processing; will redo", path.name)
                return True
            self._progress(path.name)("Writing SRT", 1.0)
            target = self.pipe.publish(path, res.cues)
            self.db.mark_done(job.rel_path, st2.st_size, st2.st_mtime_ns, self.s["model"], res.language,
                              self.ws.rel(target))
            self.log.info("Done %s [%s -> en via %s]", path.name, res.language, res.task)
            self.on_event("done", path.name)
        except Cancelled:
            status = "pending" if self.cancel_reason == "shutdown" else "cancelled"
            self._safe_status(job.rel_path, status, "cancelled")
            self.log.info("Cancelled %s (%s)", path.name, status)
        except ModelUnavailable as e:
            self.fatal = str(e)
            self._safe_status(job.rel_path, "pending")
            self.log.error("%s", e)
            self.on_event("fatal", str(e))
        except Exception as e:
            if not self.ws.is_available():
                self._safe_status(job.rel_path, "pending")
                self.check_available()
            else:
                self.log.error("Failed %s: %s", path.name, e)
                self._safe_status(job.rel_path, "failed" if isinstance(e, SubtitleError) else "failed", str(e), True)
                rec = self.db.get(job.rel_path)
                if rec and rec.attempts < self.s["max_attempts"] and not isinstance(e, SubtitleError):
                    self._safe_status(job.rel_path, "pending", str(e))
                self.on_event("failed", path.name, str(e))
        finally:
            self.current = None
        return True

    def _safe_status(self, rel, status, error=None, bump=False):
        try:
            self.db.set_status(rel, status, error, bump)
        except Exception as e:
            self.log.warning("could not record status %s for %s: %s (reset on reconnect)", status, rel, e)

    def _worker_loop(self):
        while not self.stop_evt.is_set():
            try:
                if self.fatal:
                    self.stop_evt.wait(15)
                    self.fatal = None            # retry loading the model after a pause
                    continue
                worked = self.process_next()
            except Exception as e:
                self.log.exception("worker error: %s", e)
                worked = False
            if not worked:
                self.stop_evt.wait(2)

    def drain(self, assume_stable=True):
        """Headless helper: scan, then process everything pending, then return."""
        self.scan_once(assume_stable=assume_stable)
        while not self.stop_evt.is_set() and self.process_next():
            if self.fatal:
                break
