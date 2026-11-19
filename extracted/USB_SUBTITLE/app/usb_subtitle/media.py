"""FFmpeg integration: probing and audio extraction. Uses system ffmpeg, else the imageio-ffmpeg binary."""
import os, re, shutil, subprocess, threading, wave
from dataclasses import dataclass
from typing import Optional


class Cancelled(Exception):
    pass


class MediaError(RuntimeError):
    pass


def find_ffmpeg() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@dataclass
class MediaInfo:
    duration: Optional[float]
    audio_streams: int


def probe(path, ffmpeg=None) -> MediaInfo:
    """Parse `ffmpeg -i` output (works with both full ffmpeg and the imageio build; no ffprobe needed)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise MediaError("FFmpeg not found. See README (FFmpeg setup).")
    p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
                       capture_output=True, text=True, errors="replace", timeout=120)
    err = p.stderr
    if "Invalid data found" in err or "No such file" in err or "moov atom not found" in err:
        raise MediaError("unreadable or incomplete video file")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    audio = len(re.findall(r"Stream #\d+:\d+.*?: Audio:", err))
    if dur is None and audio == 0 and "Video:" not in err:
        raise MediaError("not a recognisable media file")
    return MediaInfo(dur, audio)


def extract_audio(video, wav_out, track=0, duration=None, progress=None, cancel: threading.Event = None, ffmpeg=None):
    """Extract mono 16 kHz 16-bit PCM WAV. Cancellable; progress(fraction) if duration known."""
    ffmpeg = ffmpeg or find_ffmpeg()
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
           "-i", str(video), "-map", f"0:a:{track}", "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", "-y", str(wav_out)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    errbuf = []
    t = threading.Thread(target=lambda: errbuf.append(proc.stderr.read()), daemon=True)
    t.start()
    try:
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled()
            if line.startswith("out_time_us=") and duration and progress:
                try:
                    progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
                except ValueError:
                    pass
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    t.join(timeout=5)
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if proc.returncode != 0:
        raise MediaError("FFmpeg failed: " + (errbuf[0].strip()[-300:] if errbuf else f"exit {proc.returncode}"))


def wav_duration(wav_path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / w.getframerate()
