"""SRT building, atomic writing and validation (plain UTF-8, no styling)."""
import os, re, textwrap
from dataclasses import dataclass
from pathlib import Path

MAX_LINE = 42
TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})")


class SrtError(ValueError):
    pass


@dataclass
class Cue:
    start: float
    end: float
    text: str


def fmt_ts(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_long(cue: Cue):
    """Wrap to 42 chars/line, max 2 lines per cue; longer text becomes several cues."""
    text = re.sub(r"\s+", " ", cue.text).strip()
    lines = textwrap.wrap(text, MAX_LINE, break_long_words=True) or []
    groups = [lines[i:i + 2] for i in range(0, len(lines), 2)]
    if len(groups) <= 1:
        return [Cue(cue.start, cue.end, "\n".join(lines))] if lines else []
    total = sum(len(" ".join(g)) for g in groups)
    out, t = [], cue.start
    for g in groups:
        share = (cue.end - cue.start) * len(" ".join(g)) / total
        out.append(Cue(t, t + share, "\n".join(g)))
        t += share
    return out


def normalize(cues):
    """Sort, wrap, drop empties/consecutive duplicates, remove overlaps, enforce min duration."""
    cues = sorted((c for c in cues if c.text and c.text.strip()), key=lambda c: (c.start, c.end))
    out, prev_text = [], None
    for c in cues:
        for piece in _split_long(c):
            if piece.text == prev_text and out and piece.start - out[-1].end < 2:
                out[-1].end = max(out[-1].end, piece.end)       # merge repeats (loop hallucination)
                continue
            start = max(piece.start, out[-1].end if out else 0.0)
            end = max(piece.end, start + 0.6)
            out.append(Cue(start, end, piece.text))
            prev_text = piece.text
    return out


def render(cues) -> str:
    return "".join(f"{i}\n{fmt_ts(c.start)} --> {fmt_ts(c.end)}\n{c.text}\n\n" for i, c in enumerate(cues, 1))


def write_srt(path, cues, bom=False):
    """Atomic write: temp file in the same folder, fsync, then rename."""
    path = Path(path)
    tmp = path.with_name(path.name + ".usbsub-part")
    data = render(cues).replace("\n", "\r\n").encode("utf-8")
    with open(tmp, "wb") as f:
        if bom:
            f.write(b"\xef\xbb\xbf")
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def parse(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SrtError(f"not valid UTF-8: {e}")
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [l for l in block.splitlines()]
        if not lines:
            continue
        ti = 1 if len(lines) > 1 and TS_RE.search(lines[1]) else 0
        m = TS_RE.search(lines[ti]) if ti < len(lines) else None
        if not m:
            raise SrtError(f"bad timing line in block: {block[:40]!r}")
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        cues.append(Cue(start, end, "\n".join(lines[ti + 1:]).strip()))
    return cues


def validate(path):
    """Return (ok, message, cue_count). Strict about what we generate, tolerant of foreign files."""
    try:
        cues = parse(path)
    except (SrtError, OSError) as e:
        return False, str(e), 0
    if not cues:
        return False, "no subtitle entries", 0
    last = -1.0
    for i, c in enumerate(cues, 1):
        if c.end <= c.start:
            return False, f"entry {i}: end is not after start", len(cues)
        if c.start < last - 0.001:
            return False, f"entry {i}: timestamps go backwards", len(cues)
        if not c.text:
            return False, f"entry {i}: empty text", len(cues)
        last = c.start
    return True, "ok", len(cues)


_EN_WORDS = set("the and you that was for are with this have not but what they his her from she him your "
                "all there would their will can had were one just know about been said when who what "
                "into than them then now like get got out here how its our did don't i'm it's of to is "
                "he we my yes".split())


def looks_english(cues) -> bool:
    """Heuristic: mostly ASCII letters and a healthy share of common English words.
    Too little text to judge counts as English (so we never overwrite someone's file)."""
    text = " ".join(c.text for c in cues)[:20000].lower()
    all_words = re.findall(r"\w+", text)
    words = re.findall(r"[a-z']+", text)
    letters = [ch for ch in text if ch.isalpha()]
    if len(all_words) < 20 or not letters:
        return True
    ascii_ratio = sum(ch.isascii() for ch in letters) / len(letters)
    hits = sum(w in _EN_WORDS for w in words) / max(1, len(all_words))
    return ascii_ratio >= 0.9 and hits >= 0.15
