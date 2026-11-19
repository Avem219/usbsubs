"""Best-effort volume / hardware information. Everything here degrades gracefully."""
import os, re, shutil, subprocess, sys, plistlib
from dataclasses import dataclass


@dataclass
class Volume:
    mount: str = "?"
    fstype: str = "unknown"
    label: str = ""
    free: int = 0
    total: int = 0


FAT_TYPES = {"vfat", "fat", "fat12", "fat16", "fat32", "msdos"}


def is_fat(fstype: str) -> bool:
    return fstype.lower() in FAT_TYPES


def _unescape_octal(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _linux(path):
    p = os.path.realpath(path)
    best = None
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = _unescape_octal(parts[1])
                if p == mp or p.startswith(mp.rstrip("/") + "/"):
                    if best is None or len(mp) >= len(best[1]):
                        best = (parts[0], mp, parts[2])
    except OSError:
        return Volume()
    if not best:
        return Volume()
    dev, mp, fs = best
    label = ""
    try:
        d = "/dev/disk/by-label"
        for name in os.listdir(d):
            if os.path.realpath(os.path.join(d, name)) == os.path.realpath(dev):
                label = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)
    except OSError:
        pass
    return Volume(mp, fs, label)


def _mac(path):
    mp = os.path.realpath(path)
    while not os.path.ismount(mp) and mp != "/":
        mp = os.path.dirname(mp)
    v = Volume(mount=mp)
    try:
        out = subprocess.run(["diskutil", "info", "-plist", mp], capture_output=True, timeout=10).stdout
        info = plistlib.loads(out)
        v.fstype = str(info.get("FilesystemType", "unknown"))
        v.label = str(info.get("VolumeName", ""))
    except Exception:
        pass
    return v


def _windows(path):
    import ctypes
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    v = Volume(mount=drive + "\\" if drive else "?")
    if not drive:
        return v
    name, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(drive + "\\"), name, 261, None, None, None, fs, 261)
    if ok:
        v.label, v.fstype = name.value, fs.value
    return v


def volume_info(path) -> Volume:
    try:
        if sys.platform.startswith("win"):
            v = _windows(path)
        elif sys.platform == "darwin":
            v = _mac(path)
        else:
            v = _linux(path)
        du = shutil.disk_usage(path)
        v.free, v.total = du.free, du.total
        return v
    except Exception:
        return Volume()


def total_ram() -> int:
    """Total physical RAM in bytes, 0 if unknown."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                            ("ap", ctypes.c_ulonglong), ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            s = MS(); s.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return int(s.tp)
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
