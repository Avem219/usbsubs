"""SQLite processing history. Uses rollback-journal mode (WAL is unsafe on FAT/exFAT USB drives)."""
import sqlite3, threading, time
from dataclasses import dataclass
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  rel_path TEXT PRIMARY KEY,
  size INTEGER, mtime_ns INTEGER,
  status TEXT NOT NULL,            -- pending|processing|done|failed|cancelled|skipped_existing
  model TEXT, language TEXT, subtitle_rel TEXT, error TEXT,
  attempts INTEGER DEFAULT 0,
  created REAL, updated REAL, completed REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


@dataclass
class Job:
    rel_path: str
    size: int
    mtime_ns: int
    status: str
    model: Optional[str]
    language: Optional[str]
    subtitle_rel: Optional[str]
    error: Optional[str]
    attempts: int
    completed: Optional[float]


COLS = "rel_path,size,mtime_ns,status,model,language,subtitle_rel,error,attempts,completed"


class Database:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = None
        self.reopen()

    def reopen(self):
        with self.lock:
            try:
                if self.conn:
                    self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=DELETE")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def get(self, rel) -> Optional[Job]:
        with self.lock:
            r = self.conn.execute(f"SELECT {COLS} FROM jobs WHERE rel_path=?", (rel,)).fetchone()
        return Job(*r) if r else None

    def enqueue(self, rel, size, mtime_ns, model):
        now = time.time()
        self._x("""INSERT INTO jobs(rel_path,size,mtime_ns,status,model,attempts,created,updated)
                   VALUES(?,?,?,'pending',?,0,?,?)
                   ON CONFLICT(rel_path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                     status='pending', model=excluded.model, attempts=0, error=NULL,
                     subtitle_rel=NULL, completed=NULL, updated=excluded.updated""",
                (rel, size, mtime_ns, model, now, now))

    def record_skipped(self, rel, size, mtime_ns, subtitle_rel):
        now = time.time()
        self._x("""INSERT INTO jobs(rel_path,size,mtime_ns,status,subtitle_rel,attempts,created,updated,completed)
                   VALUES(?,?,?,'skipped_existing',?,0,?,?,?)
                   ON CONFLICT(rel_path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                     status='skipped_existing', subtitle_rel=excluded.subtitle_rel, updated=excluded.updated""",
                (rel, size, mtime_ns, subtitle_rel, now, now, now))

    def claim_next(self) -> Optional[Job]:
        with self.lock:
            r = self.conn.execute(f"SELECT {COLS} FROM jobs WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
            if not r:
                return None
            self.conn.execute("UPDATE jobs SET status='processing', updated=? WHERE rel_path=?", (time.time(), r[0]))
            self.conn.commit()
        return Job(*r)

    def set_status(self, rel, status, error=None, bump_attempt=False):
        self._x("UPDATE jobs SET status=?, error=?, attempts=attempts+?, updated=? WHERE rel_path=?",
                (status, error, 1 if bump_attempt else 0, time.time(), rel))

    def mark_done(self, rel, size, mtime_ns, model, language, subtitle_rel):
        now = time.time()
        self._x("""UPDATE jobs SET status='done', size=?, mtime_ns=?, model=?, language=?, subtitle_rel=?,
                   error=NULL, completed=?, updated=? WHERE rel_path=?""",
                (size, mtime_ns, model, language, subtitle_rel, now, now, rel))

    def upsert_done(self, rel, size, mtime_ns, model, language, subtitle_rel):
        now = time.time()
        self._x("""INSERT INTO jobs(rel_path,size,mtime_ns,status,model,language,subtitle_rel,attempts,created,updated,completed)
                   VALUES(?,?,?,'done',?,?,?,0,?,?,?)
                   ON CONFLICT(rel_path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, status='done',
                     model=excluded.model, language=excluded.language, subtitle_rel=excluded.subtitle_rel,
                     error=NULL, completed=excluded.completed, updated=excluded.updated""",
                (rel, size, mtime_ns, model, language, subtitle_rel, now, now, now))

    def reset_interrupted(self) -> int:
        return self._x("UPDATE jobs SET status='pending', updated=? WHERE status='processing'", (time.time(),)).rowcount

    def retry_failed(self) -> int:
        return self._x("UPDATE jobs SET status='pending', attempts=0, error=NULL, updated=? "
                       "WHERE status IN ('failed','cancelled')", (time.time(),)).rowcount

    def by_status(self, *statuses, limit=50):
        q = ",".join("?" * len(statuses))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {COLS} FROM jobs WHERE status IN ({q}) ORDER BY updated DESC LIMIT ?",
                (*statuses, limit)).fetchall()
        return [Job(*r) for r in rows]
