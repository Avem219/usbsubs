"""Terminal interface: dashboard, commands, managed copy, environment check."""
import argparse, os, platform, shutil, sys, threading, time
from pathlib import Path

from . import __version__, srt
from .config import MODELS, Settings
from .engine import Engine
from .fsinfo import human, is_fat, total_ram, volume_info
from .media import find_ffmpeg
from .paths import RootNotFound, Workspace, find_root

HELP = """Commands:
  status            show queue and completed subtitles
  retry             retry failed / cancelled jobs
  cancel            cancel the job being processed
  model tiny|base|small   switch Whisper model (base/small = better, slower)
  lang en           output language (only English is supported in v1)
  folder <name>     choose the movies folder (relative to the USB root)
  threads <n>       CPU threads (0 = auto)
  logs              show the last log lines
  help              this text
  quit              stop safely and exit"""


def build(args):
    root = find_root(override=args.root)
    probe_ws = Workspace(root)
    settings = Settings(probe_ws.settings_path) if probe_ws.config_dir.exists() else None
    if settings is None:
        probe_ws.ensure()
        settings = Settings(probe_ws.settings_path)
    ws = Workspace(root, settings["movies_folder"])
    ws.ensure()
    return ws, settings


def header(ws, settings):
    v = volume_info(ws.root)
    print("\nUSB SUBTITLE  v" + __version__)
    print(f"Connected drive: {v.mount}  {('[' + v.label + '] ') if v.label else ''}"
          f"({v.fstype}, {human(v.free)} free of {human(v.total)})")
    print(f"Movie folder: {settings['movies_folder']}   Model: {settings['model']}   Output: English")
    if is_fat(v.fstype):
        print("  note: FAT filesystem - single files cannot exceed 4 GB. Check README before using exFAT with your TV.")
    if not find_ffmpeg():
        print("  WARNING: FFmpeg not found - see README (FFmpeg setup).")
    ram = total_ram()
    if settings["model"] != "tiny" and 0 < ram < 8 * 1024 ** 3:
        print(f"  note: model '{settings['model']}' with {human(ram)} RAM may be slow; 'tiny' is safer.")


def show_status(eng):
    print("\nProcessing queue:")
    cur = eng.current
    if eng.disconnected:
        print("  USB disconnected - waiting for it to return")
    if cur:
        print(f"  > {cur['name']}  [{cur['stage']}  {cur['progress'] * 100:.0f}%]  processing")
    for j in eng.db.by_status("pending", limit=20):
        print(f"  - {Path(j.rel_path).name}  [waiting]  pending")
    for j in eng.db.by_status("failed", "cancelled", limit=10):
        print(f"  ! {Path(j.rel_path).name}  [{j.status}]  {j.error or ''}")
    print("Completed subtitles:")
    rows = eng.db.by_status("done", "skipped_existing", limit=15)
    for j in rows:
        tag = "done" if j.status == "done" else "already had subtitles"
        lang = f" ({j.language}->en)" if j.language else ""
        print(f"  * {Path(j.rel_path).name}  ->  {Path(j.subtitle_rel or '?').name}  {tag}{lang}")
    if not rows:
        print("  (none yet)")
    if eng.fatal:
        print("PROBLEM:", eng.fatal)


def make_printer():
    last = {"k": None, "t": 0}

    def on_event(kind, *a):
        if kind == "progress":
            d = a[0]
            key = (d["name"], d["stage"], int(d["progress"] * 10))
            if key == last["k"] or time.time() - last["t"] < 1:
                return
            last["k"], last["t"] = key, time.time()
            print(f"  {d['name']}: {d['stage']} {d['progress'] * 100:.0f}%", flush=True)
        elif kind == "queued":
            print(f"+ detected {a[0]} - queued", flush=True)
        elif kind == "skipped":
            print(f"= {a[0]} already has a valid English subtitle - skipped", flush=True)
        elif kind == "done":
            print(f"OK {a[0]}  -> subtitle saved beside the video", flush=True)
        elif kind == "failed":
            print(f"FAILED {a[0]}: {a[1]}", flush=True)
        elif kind == "disconnected":
            print("!! USB disconnected. Reconnect it and processing resumes automatically.", flush=True)
        elif kind == "reconnected":
            print("USB reconnected - resuming.", flush=True)
        elif kind == "fatal":
            print("PROBLEM:", a[0], flush=True)
    return on_event


def cmd_run(args):
    ws, settings = build(args)
    eng = Engine(ws, settings, on_event=make_printer())
    header(ws, settings)
    eng.start()
    print("Watching for new videos. Type 'help' for commands, 'quit' to exit.")
    try:
        if args.no_interactive or not sys.stdin or not sys.stdin.isatty():
            while True:
                time.sleep(1)
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                while True:
                    time.sleep(1)
            if not line:
                continue
            cmd, _, arg = line.partition(" ")
            cmd = cmd.lower()
            try:
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "status":
                    show_status(eng)
                elif cmd == "retry":
                    print(f"{eng.db.retry_failed()} job(s) re-queued")
                elif cmd == "cancel":
                    print("cancelling..." if eng.cancel_current() else "nothing is running")
                elif cmd == "model":
                    settings.set("model", arg.strip()); eng.tr.unload(); print("model set to", arg.strip(),
                                                                              "(applies to next job)")
                elif cmd == "lang":
                    settings.set("output_language", arg.strip().lower()); print("output language:", arg.strip())
                elif cmd == "folder":
                    settings.set("movies_folder", arg.strip())
                    ws.movies_name = arg.strip(); ws.movies_dir.mkdir(parents=True, exist_ok=True)
                    print("movie folder:", ws.movies_dir)
                elif cmd == "threads":
                    settings.set("cpu_threads", int(arg)); eng.tr.unload(); print("threads set")
                elif cmd == "logs":
                    print("".join(ws.log_path.read_text(encoding="utf-8").splitlines(True)[-25:]))
                elif cmd == "help":
                    print(HELP)
                else:
                    print("unknown command; type 'help'")
            except (ValueError, KeyError, OSError) as e:
                print("error:", e)
    except KeyboardInterrupt:
        pass
    print("Stopping safely (the current job will resume next time)...")
    eng.stop()
    print("Done. You can now eject the USB drive.")
    return 0


def cmd_scan(args):
    ws, settings = build(args)
    eng = Engine(ws, settings, on_event=make_printer())
    header(ws, settings)
    eng.pipe.purge_stale_temp()
    eng.db.reset_interrupted()
    try:
        eng.drain(assume_stable=args.assume_stable)
    except KeyboardInterrupt:
        eng.stop_evt.set()
    show_status(eng)
    eng.db.close()
    return 1 if eng.fatal else 0


def cmd_check(args):
    ws, settings = build(args)
    ok = True
    print(f"Python {platform.python_version()} on {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"CPU cores: {os.cpu_count()}   RAM: {human(total_ram()) if total_ram() else 'unknown'}")
    print(f"USB root: {ws.root}")
    v = volume_info(ws.root)
    print(f"Drive: {v.mount} fs={v.fstype} label={v.label!r} free={human(v.free)}")
    ff = find_ffmpeg()
    print("FFmpeg:", ff or "NOT FOUND"); ok &= bool(ff)
    try:
        import faster_whisper
        print("faster-whisper:", faster_whisper.__version__)
        from .transcribe import Transcriber
        from .engine import make_logger
        t = Transcriber(settings, ws, make_logger(ws))
        cached = t.is_cached()
        print(f"Model '{settings['model']}' cached on this computer: {cached}")
        if not cached:
            print("  -> run once with internet:  python -m usb_subtitle download-model")
    except ImportError:
        print("faster-whisper: NOT INSTALLED"); ok = False
    try:
        probe = ws.temp_dir / ".write-test"
        probe.write_text("x"); probe.unlink(); print("USB writable: yes")
    except OSError as e:
        print("USB writable: NO -", e); ok = False
    return 0 if ok else 1


def cmd_download(args):
    ws, settings = build(args)
    if args.name:
        settings.set("model", args.name)
    from .transcribe import Transcriber
    from .engine import make_logger
    t = Transcriber(settings, ws, make_logger(ws))
    print(f"Downloading model '{settings['model']}' (one-time, needs internet)...")
    print("Saved to:", t.download())
    return 0


def cmd_copy(args):
    """Managed copy: copy a video into MOVIES under a temporary name (so the watcher never sees a half file),
    and at the same time generate subtitles from the COMPLETE source file."""
    ws, settings = build(args)
    src = Path(args.source).expanduser().resolve()
    if not src.is_file():
        print("Source file not found:", src); return 1
    dest_dir = ws.movies_dir / args.subfolder if args.subfolder else ws.movies_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    v = volume_info(ws.root)
    size = src.stat().st_size
    if is_fat(v.fstype) and size >= 4 * 1024 ** 3:
        print(f"This file is {human(size)} but the USB is {v.fstype} (4 GB per-file limit). "
              "Use a smaller encode or reformat to exFAT (check your TV supports it).")
        return 1
    if v.free and size > v.free:
        print(f"Not enough space on USB ({human(v.free)} free, need {human(size)})."); return 1
    if dest.exists():
        print("Destination already exists:", dest); return 1
    eng = Engine(ws, settings, on_event=make_printer())
    header(ws, settings)
    result, err = {}, {}

    def gen():
        try:
            eng.current = {"name": src.name, "stage": "Starting", "progress": 0}
            result["r"] = eng.pipe.generate(src, eng._progress(src.name), eng.cancel_evt)
        except Exception as e:
            err["e"] = e

    t = None if args.no_subtitles else threading.Thread(target=gen)
    if t:
        t.start()
    part = dest.with_name(dest.name + ".usbsub-part")
    done, t0 = 0, time.time()
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                b = fi.read(8 * 1024 * 1024)
                if not b:
                    break
                fo.write(b); done += len(b)
                if time.time() - t0 > 2:
                    print(f"  copying {done / size * 100:.0f}%", flush=True); t0 = time.time()
            fo.flush(); os.fsync(fo.fileno())
        os.replace(part, dest)
    except (OSError, KeyboardInterrupt) as e:
        eng.cancel_evt.set(); eng.cancel_reason = "shutdown"
        try: part.unlink()
        except OSError: pass
        print("Copy aborted:", e); return 1
    print("Copy finished:", dest)
    if t:
        print("Waiting for subtitle generation to finish...")
        t.join()
        if "e" in err:
            print("Subtitle generation failed:", err["e"]); return 1
        r = result["r"]
        target = eng.pipe.publish(dest, r.cues)
        st = dest.stat()
        eng.db.upsert_done(ws.rel(dest), st.st_size, st.st_mtime_ns, settings["model"], r.language, ws.rel(target))
        print("Subtitle saved:", target.name)
    eng.db.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="usb_subtitle", description="USB SUBTITLE - offline English subtitles for the movies on this drive")
    ap.add_argument("--root", help="USB root (folder containing USB_SUBTITLE); normally auto-detected")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="watch MOVIES and process automatically (default)")
    r.add_argument("--no-interactive", action="store_true", help="background mode, no prompt")
    s = sub.add_parser("scan", help="process everything now, then exit")
    s.add_argument("--assume-stable", action="store_true", help="do not wait for files to settle")
    sub.add_parser("check", help="check this computer is ready")
    d = sub.add_parser("download-model", help="cache the model on this computer (needs internet once)")
    d.add_argument("name", nargs="?", choices=MODELS)
    c = sub.add_parser("copy", help="managed copy into MOVIES with simultaneous subtitle generation")
    c.add_argument("source"); c.add_argument("--subfolder"); c.add_argument("--no-subtitles", action="store_true")
    args = ap.parse_args(argv)
    try:
        fn = {"run": cmd_run, "scan": cmd_scan, "check": cmd_check, "download-model": cmd_download,
              "copy": cmd_copy}.get(args.cmd or "run")
        if args.cmd is None:
            args.no_interactive = False
        return fn(args)
    except RootNotFound as e:
        print(e); return 2
