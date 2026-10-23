import os
import json
import sqlite3
import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime, date
from typing import List, Dict, Any, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_FILE = os.path.join(DATA_DIR, "jobs.db")
LEGACY_JSON = os.path.join(DATA_DIR, "jobs_db.json")

# Jobs without a deadline (LinkedIn, Remotive) expire once they haven't shown up in a scrape for this long
STALE_DAYS = int(os.environ.get("STALE_DAYS", "7"))

_lock = threading.RLock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    num INTEGER UNIQUE NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS tailored (
    job_id TEXT PRIMARY KEY,
    num INTEGER,
    job TEXT,
    status TEXT,            -- working | done | failed
    created TEXT,
    pdf_path TEXT,
    result TEXT,            -- changes, gaps, keyword scores, removed claims
    cover_letter TEXT,
    applied INTEGER DEFAULT 0,
    error TEXT
);
"""


def today() -> date:
    return datetime.now().date()


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@contextmanager
def _db():
    global _initialized
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_FILE, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if not _initialized:
                conn.executescript(SCHEMA)
                conn.execute("PRAGMA journal_mode=WAL")
                _migrate_legacy_json(conn)
                _initialized = True
            yield conn
            conn.commit()
        finally:
            conn.close()


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    if not os.path.exists(LEGACY_JSON) or conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
        return
    with open(LEGACY_JSON, "r", encoding="utf-8") as f:
        legacy = json.load(f)
    for jid, j in legacy.get("jobs", {}).items():
        _insert_job(conn, jid, j, j.get("First_Seen"), j.get("Last_Seen"))
    _set_meta(conn, "cv_hash", legacy.get("cv_hash"))
    _set_meta(conn, "last_run", legacy.get("last_run"))
    _set_meta(conn, "last_added", legacy.get("last_added", []))
    os.replace(LEGACY_JSON, LEGACY_JSON + ".migrated")
    print(f"[DB] Migrated {len(legacy.get('jobs', {}))} jobs from jobs_db.json to SQLite.")


def _get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))


def get_meta(key: str, default=None):
    with _db() as conn:
        return _get_meta(conn, key, default)


def set_meta(key: str, value) -> None:
    with _db() as conn:
        _set_meta(conn, key, value)


def _insert_job(conn: sqlite3.Connection, jid: str, job: Dict[str, Any], first_seen: str, last_seen: str) -> None:
    # Numbers are never reused, so "tailor 12" always means the same job
    num = _get_meta(conn, "next_num", 1)
    _set_meta(conn, "next_num", num + 1)
    data = {k: v for k, v in job.items() if k not in ("Num", "Tailor_Status", "Applied")}
    conn.execute(
        "INSERT INTO jobs(job_id, num, data, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
        (jid, num, json.dumps(data, ensure_ascii=False), first_seen, last_seen),
    )


def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    j = json.loads(row["data"])
    j["Num"] = row["num"]
    j["First_Seen"] = row["first_seen"]
    j["Last_Seen"] = row["last_seen"]
    if "t_status" in row.keys():
        j["Tailor_Status"] = row["t_status"]
        j["Applied"] = bool(row["t_applied"])
    return j


def deadline_date(job: Dict[str, Any]) -> Optional[date]:
    iso = str(job.get("Deadline_ISO") or "")[:10]
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def is_expired(job: Dict[str, Any], on: date) -> bool:
    dl = deadline_date(job)
    if dl:
        return dl < on
    try:
        last_seen = date.fromisoformat(job.get("Last_Seen", ""))
    except (ValueError, TypeError):
        return False
    return (on - last_seen).days > STALE_DAYS


def days_left(job: Dict[str, Any]) -> Optional[int]:
    dl = deadline_date(job)
    return (dl - today()).days if dl else None


def split_known(scraped: List[Dict[str, Any]], cv_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns (already_scored, needs_scoring). Jobs already in the DB keep their Gemini score.
    If the CV changed since the DB was built, everything is re-scored.
    """
    with _db() as conn:
        if _get_meta(conn, "cv_hash") != file_hash(cv_path):
            return [], scraped
        known_ids = {r[0] for r in conn.execute("SELECT job_id FROM jobs")}
    known, new = [], []
    for j in scraped:
        (known if str(j.get("Job_ID")) in known_ids else new).append(j)
    return known, new


def update(scraped: List[Dict[str, Any]], scored_new: List[Dict[str, Any]], cv_path: str) -> Dict[str, Any]:
    """
    Merges a run into the DB: refreshes listing details of known jobs, adds newly scored jobs,
    and drops expired ones. Returns a summary of what changed.
    """
    with _db() as conn:
        cv_hash = file_hash(cv_path)
        if _get_meta(conn, "cv_hash") != cv_hash:
            conn.execute("DELETE FROM jobs")
            _set_meta(conn, "cv_hash", cv_hash)

        today_str = today().isoformat()
        score_keys = ("Match_Score", "Match_Level", "Match_Reason", "AI_Scored")

        for j in scraped:
            jid = str(j.get("Job_ID"))
            row = conn.execute("SELECT data FROM jobs WHERE job_id=?", (jid,)).fetchone()
            if row:
                # Listing details (deadline etc.) may have changed; keep the score fields
                old = json.loads(row["data"])
                merged = {**j, **{k: old[k] for k in score_keys if k in old}}
                if old.get("Description") and not merged.get("Description"):
                    merged["Description"] = old["Description"]
                conn.execute("UPDATE jobs SET data=?, last_seen=? WHERE job_id=?",
                             (json.dumps(merged, ensure_ascii=False), today_str, jid))

        added = []
        for j in scored_new:
            # Keyword-fallback results aren't stored, so Gemini retries them next run
            jid = str(j.get("Job_ID"))
            if not j.get("AI_Scored") or jid in added:
                continue
            if conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (jid,)).fetchone():
                continue
            _insert_job(conn, jid, j, today_str, today_str)
            added.append(jid)

        now = today()
        expired = [r["job_id"] for r in conn.execute("SELECT * FROM jobs") if is_expired(_row_to_job(r), now)]
        conn.executemany("DELETE FROM jobs WHERE job_id=?", [(jid,) for jid in expired])

        _set_meta(conn, "last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _set_meta(conn, "last_added", added)
        active = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return {"added": len(added), "expired": len(expired), "active": active}


_JOBS_WITH_TAILOR = """
SELECT jobs.*, tailored.status AS t_status, COALESCE(tailored.applied, 0) AS t_applied
FROM jobs LEFT JOIN tailored ON tailored.job_id = jobs.job_id
"""


def active_jobs(levels: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(_JOBS_WITH_TAILOR).fetchall()
    now = today()
    jobs = [j for j in map(_row_to_job, rows) if not is_expired(j, now)]
    if levels:
        jobs = [j for j in jobs if j.get("Match_Level") in levels]
    jobs.sort(key=lambda x: x.get("Match_Score", 0), reverse=True)
    return jobs


def get_job(job_id: str = None, num: int = None) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        if num is not None:
            row = conn.execute(_JOBS_WITH_TAILOR + " WHERE jobs.num=?", (num,)).fetchone()
        else:
            row = conn.execute(_JOBS_WITH_TAILOR + " WHERE jobs.job_id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def save_job_fields(job_id: str, **fields) -> None:
    with _db() as conn:
        row = conn.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row:
            data = {**json.loads(row["data"]), **fields}
            conn.execute("UPDATE jobs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), job_id))


def last_added_ids() -> List[str]:
    return get_meta("last_added", [])


def last_run() -> Optional[str]:
    return get_meta("last_run")


# ---- Tailored CVs ----

def _row_to_tailored(row: sqlite3.Row) -> Dict[str, Any]:
    t = dict(row)
    t["job"] = json.loads(t["job"] or "{}")
    t["result"] = json.loads(t["result"] or "{}")
    t["applied"] = bool(t["applied"])
    return t


def get_tailored(job_id: str = None, num: int = None) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        if num is not None:
            row = conn.execute("SELECT * FROM tailored WHERE num=?", (num,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM tailored WHERE job_id=?", (job_id,)).fetchone()
    return _row_to_tailored(row) if row else None


def list_tailored() -> List[Dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM tailored ORDER BY created DESC").fetchall()
    return [_row_to_tailored(r) for r in rows]


def reset_stuck_tailoring() -> None:
    """A restart mid-tailoring leaves rows on 'working' forever; mark them failed so they can be retried."""
    with _db() as conn:
        conn.execute("UPDATE tailored SET status='failed', error='Interrupted by a restart' WHERE status='working'")


def save_tailored(job: Dict[str, Any], **fields) -> None:
    jid = str(job.get("Job_ID"))
    with _db() as conn:
        exists = conn.execute("SELECT 1 FROM tailored WHERE job_id=?", (jid,)).fetchone()
        if not exists:
            conn.execute("INSERT INTO tailored(job_id, num, created) VALUES (?, ?, ?)",
                         (jid, job.get("Num"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        fields.setdefault("job", job)
        for key, value in fields.items():
            if key in ("job", "result"):
                value = json.dumps(value, ensure_ascii=False)
            elif key == "applied":
                value = int(bool(value))
            conn.execute(f"UPDATE tailored SET {key}=? WHERE job_id=?", (value, jid))
