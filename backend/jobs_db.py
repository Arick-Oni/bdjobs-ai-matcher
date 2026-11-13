import os
import re
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
CV_STORE_DIR = os.path.join(BASE_DIR, "cv_store")

# The owner is the chat that owns the web app and the original CV. Everyone else registers through the bot.
OWNER_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or "owner"

# Jobs without a deadline (LinkedIn, Remotive) expire once they haven't shown up in a scrape for this long
STALE_DAYS = int(os.environ.get("STALE_DAYS", "7"))

# Scores belong to a (user, job) pair now, so they are never stored on the shared job rows
SCORE_FIELDS = ("Match_Score", "Match_Level", "Match_Reason", "AI_Scored")
ROW_FIELDS = ("Num", "Tailor_Status", "Applied")

_lock = threading.RLock()
_initialized = False

TAILORED_TABLE = """
CREATE TABLE IF NOT EXISTS tailored (
    chat_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    num INTEGER,
    job TEXT,
    status TEXT,            -- working | done | failed | none
    created TEXT,
    pdf_path TEXT,
    result TEXT,            -- changes, gaps, keyword scores, removed claims
    cover_letter TEXT,
    applied INTEGER DEFAULT 0,
    error TEXT,
    PRIMARY KEY (chat_id, job_id)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    num INTEGER UNIQUE NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS users (
    chat_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,           -- pending (waiting for admin) | awaiting_cv (approved) | active | rejected
    name TEXT,                      -- display name from Telegram, for the admin's list
    search TEXT,                    -- JSON {"keywords": [...], "remotive": [...]}: what the scrapers look for on their behalf
    cv_path TEXT,
    cv_hash TEXT,
    created TEXT,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    chat_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    cv_hash TEXT NOT NULL,          -- the CV version this score was made against
    score INTEGER,
    level TEXT,
    reason TEXT,
    run_id TEXT,                    -- when it was scored; a user's "new" jobs share their latest run_id
    PRIMARY KEY (chat_id, job_id)
);
CREATE TABLE IF NOT EXISTS sent_messages (
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    sent TEXT,                      -- Telegram only lets a bot delete its messages for 48 hours
    PRIMARY KEY (chat_id, message_id)
);
""" + TAILORED_TABLE


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
                _migrate_to_multi_user(conn)
                _add_missing_columns(conn)
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
    os.replace(LEGACY_JSON, LEGACY_JSON + ".migrated")
    print(f"[DB] Migrated {len(legacy.get('jobs', {}))} jobs from jobs_db.json to SQLite.")


def _migrate_to_multi_user(conn: sqlite3.Connection) -> None:
    """
    One-time upgrade from the single-CV layout. Existing scores become the owner's matches, the owner's CV
    becomes the owner's user row, and tailored CVs are keyed by user.
    """
    if _get_meta(conn, "schema_version") == 2:
        return
    print("[DB] Upgrading database to multi-user layout...")
    conn.execute("BEGIN")
    try:
        if "chat_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(tailored)")}:
            conn.execute("ALTER TABLE tailored RENAME TO tailored_v1")
            conn.execute(TAILORED_TABLE)
            conn.execute(
                "INSERT INTO tailored(chat_id, job_id, num, job, status, created, pdf_path, result, cover_letter, applied, error) "
                "SELECT ?, job_id, num, job, status, created, pdf_path, result, cover_letter, applied, error FROM tailored_v1",
                (OWNER_ID,),
            )
            conn.execute("DROP TABLE tailored_v1")

        # Old scores were made against the single CV recorded in meta; keep that hash so they stay valid only if the CV is unchanged
        old_hash = _get_meta(conn, "cv_hash") or ""
        last_run = _get_meta(conn, "last_run") or ""
        for row in conn.execute("SELECT job_id, data FROM jobs").fetchall():
            data = json.loads(row["data"])
            if "Match_Level" in data:
                conn.execute(
                    "INSERT OR REPLACE INTO matches(chat_id, job_id, cv_hash, score, level, reason, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (OWNER_ID, row["job_id"], old_hash, data.get("Match_Score"), data.get("Match_Level"),
                     data.get("Match_Reason"), last_run),
                )
            stripped = {k: v for k, v in data.items() if k not in SCORE_FIELDS}
            conn.execute("UPDATE jobs SET data=? WHERE job_id=?", (json.dumps(stripped, ensure_ascii=False), row["job_id"]))

        master = _get_meta(conn, "master_cv")
        if master is not None:
            _set_meta(conn, master_cv_key(OWNER_ID), master)
            conn.execute("DELETE FROM meta WHERE key='master_cv'")

        owner_cv = cv_path_for(OWNER_ID)
        if os.path.exists(owner_cv) and not conn.execute("SELECT 1 FROM users WHERE chat_id=?", (OWNER_ID,)).fetchone():
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO users(chat_id, status, cv_path, cv_hash, created, updated) VALUES (?, 'active', ?, ?, ?, ?)",
                (OWNER_ID, owner_cv, file_hash(owner_cv), now, now),
            )
        _set_meta(conn, "schema_version", 2)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Older databases lack columns added since: users.name (approval flow), users.search (per-user scraping)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col in ("name", "search"):
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")


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


def master_cv_key(chat_id: str) -> str:
    return f"master_cv:{chat_id}"


def _insert_job(conn: sqlite3.Connection, jid: str, job: Dict[str, Any], first_seen: str, last_seen: str) -> None:
    # Numbers are never reused, so "tailor 12" always means the same job
    num = _get_meta(conn, "next_num", 1)
    _set_meta(conn, "next_num", num + 1)
    data = {k: v for k, v in job.items() if k not in ROW_FIELDS}
    conn.execute(
        "INSERT INTO jobs(job_id, num, data, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
        (jid, num, json.dumps(data, ensure_ascii=False), first_seen, last_seen),
    )


def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    j = json.loads(row["data"])
    j["Num"] = row["num"]
    j["First_Seen"] = row["first_seen"]
    j["Last_Seen"] = row["last_seen"]
    return j


def _user_job(row: sqlite3.Row) -> Dict[str, Any]:
    j = _row_to_job(row)
    j["Match_Score"] = row["m_score"]
    j["Match_Level"] = row["m_level"]
    j["Match_Reason"] = row["m_reason"]
    j["AI_Scored"] = True
    j["Run_ID"] = row["m_run"]
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


# ---- Users ----

def cv_path_for(chat_id: str) -> str:
    """Where a user's CV PDF lives. The owner keeps the original file name the web app uses."""
    if chat_id == OWNER_ID:
        return os.path.join(CV_STORE_DIR, "current_cv.pdf")
    if not re.fullmatch(r"-?\d+", str(chat_id)):
        raise ValueError(f"Not a Telegram chat id: {chat_id}")
    os.makedirs(os.path.join(CV_STORE_DIR, "users"), exist_ok=True)
    return os.path.join(CV_STORE_DIR, "users", f"{chat_id}.pdf")


def get_user(chat_id: str) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def active_users() -> List[Dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM users WHERE status='active'").fetchall()
    return [dict(r) for r in rows]


def list_users() -> List[Dict[str, Any]]:
    """Everyone except the owner, pending requests first, then newest."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE chat_id<>? "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'awaiting_cv' THEN 1 WHEN 'active' THEN 2 ELSE 3 END, created DESC",
            (OWNER_ID,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_user(chat_id: str, status: str = "awaiting_cv", name: str = "") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(chat_id, status, name, created, updated) VALUES (?, ?, ?, ?, ?)",
                     (chat_id, status, name, now, now))


def set_user_status(chat_id: str, status: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute("UPDATE users SET status=?, updated=? WHERE chat_id=?", (status, now, chat_id))


def delete_user(chat_id: str) -> List[str]:
    """Removes a user's CV, matches, tailored CVs and master CV. Returns the files the caller should delete."""
    if chat_id == OWNER_ID:
        raise ValueError("The admin account can't be deleted from the bot.")
    with _db() as conn:
        paths = [r["pdf_path"] for r in conn.execute(
            "SELECT pdf_path FROM tailored WHERE chat_id=? AND pdf_path IS NOT NULL", (chat_id,)).fetchall()]
        row = conn.execute("SELECT cv_path FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if row and row["cv_path"]:
            paths.append(row["cv_path"])
        conn.execute("DELETE FROM matches WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM tailored WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM meta WHERE key=?", (master_cv_key(chat_id),))
        conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
    return paths


def set_user_cv(chat_id: str, cv_path: str, cv_hash: str) -> None:
    """Stores a CV and makes the user active. Scores made against any other CV stop counting."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "INSERT INTO users(chat_id, status, cv_path, cv_hash, created, updated) VALUES (?, 'active', ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET status='active', cv_path=excluded.cv_path, "
            "cv_hash=excluded.cv_hash, updated=excluded.updated",
            (chat_id, cv_path, cv_hash, now, now),
        )
        conn.execute("DELETE FROM matches WHERE chat_id=? AND cv_hash<>?", (chat_id, cv_hash))


MAX_KEYWORDS_PER_USER = 8
MAX_KEYWORDS_TOTAL = int(os.environ.get("MAX_LINKEDIN_KEYWORDS", "24"))  # One LinkedIn search each, every run


def get_search(chat_id: str) -> Dict[str, List[str]]:
    user = get_user(chat_id) or {}
    try:
        s = json.loads(user.get("search") or "{}")
    except ValueError:
        s = {}
    return {"keywords": list(s.get("keywords") or []), "remotive": list(s.get("remotive") or [])}


def set_search(chat_id: str, keywords: Optional[List[str]] = None, remotive: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """Replaces the given parts of a user's search terms; parts passed as None are kept."""
    s = get_search(chat_id)
    clean = lambda items: list(dict.fromkeys(" ".join(str(i).split()).lower() for i in items if str(i).strip()))
    if keywords is not None:
        s["keywords"] = clean(keywords)[:MAX_KEYWORDS_PER_USER]
    if remotive is not None:
        s["remotive"] = clean(remotive)
    with _db() as conn:
        conn.execute("UPDATE users SET search=? WHERE chat_id=?", (json.dumps(s), chat_id))
    return s


def search_terms() -> Tuple[List[str], List[str]]:
    """LinkedIn keywords and Remotive categories wanted by active users, shared fairly when there are too many."""
    per_user = [get_search(u["chat_id"]) for u in active_users()]
    keywords: List[str] = []
    # Round-robin: everyone's first keyword, then everyone's second, so a long list can't crowd out other users
    for i in range(MAX_KEYWORDS_PER_USER):
        for s in per_user:
            if i < len(s["keywords"]) and s["keywords"][i] not in keywords:
                keywords.append(s["keywords"][i])
    categories = list(dict.fromkeys(c for s in per_user for c in s["remotive"]))
    return keywords[:MAX_KEYWORDS_TOTAL], categories


def log_sent_message(chat_id: str, message_id: int) -> None:
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO sent_messages(chat_id, message_id, sent) VALUES (?, ?, ?)",
                     (str(chat_id), int(message_id), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def sent_message_ids(chat_id: str, since: str = "") -> List[int]:
    with _db() as conn:
        rows = conn.execute("SELECT message_id FROM sent_messages WHERE chat_id=? AND sent>=? ORDER BY message_id",
                            (str(chat_id), since)).fetchall()
    return [r["message_id"] for r in rows]


def forget_sent_messages(chat_id: str, message_ids: List[int]) -> None:
    with _db() as conn:
        conn.executemany("DELETE FROM sent_messages WHERE chat_id=? AND message_id=?",
                         [(str(chat_id), int(m)) for m in message_ids])


def ensure_owner() -> None:
    """Creates the owner's row on first start. Without a CV on disk the owner must send one in Telegram first."""
    if get_user(OWNER_ID):
        return
    path = cv_path_for(OWNER_ID)
    if os.path.exists(path):
        set_user_cv(OWNER_ID, path, file_hash(path))
    else:
        create_user(OWNER_ID, status="awaiting_cv")


# ---- Jobs ----

def _listing(job: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in job.items() if k not in SCORE_FIELDS + ROW_FIELDS}


def upsert_jobs(scraped: List[Dict[str, Any]]) -> List[str]:
    """Adds newly scraped jobs and refreshes listing details of known ones. Returns the new job ids."""
    today_str = today().isoformat()
    added = []
    with _db() as conn:
        for j in scraped:
            jid = str(j.get("Job_ID"))
            row = conn.execute("SELECT data FROM jobs WHERE job_id=?", (jid,)).fetchone()
            if row:
                old = json.loads(row["data"])
                merged = _listing(j)
                if old.get("Description") and not merged.get("Description"):
                    merged["Description"] = old["Description"]
                conn.execute("UPDATE jobs SET data=?, last_seen=? WHERE job_id=?",
                             (json.dumps(merged, ensure_ascii=False), today_str, jid))
            else:
                _insert_job(conn, jid, _listing(j), today_str, today_str)
                added.append(jid)
    return added


def expire_jobs() -> int:
    now = today()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
        expired = [r["job_id"] for r in rows if is_expired(_row_to_job(r), now)]
        conn.executemany("DELETE FROM matches WHERE job_id=?", [(jid,) for jid in expired])
        conn.executemany("DELETE FROM jobs WHERE job_id=?", [(jid,) for jid in expired])
    return len(expired)


def job_count() -> int:
    """Open jobs. Expired ones are removed by expire_jobs() before this is read."""
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def get_job(job_id: str = None, num: int = None) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        if num is not None:
            row = conn.execute("SELECT * FROM jobs WHERE num=?", (num,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def save_job_fields(job_id: str, **fields) -> None:
    with _db() as conn:
        row = conn.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row:
            data = {**json.loads(row["data"]), **fields}
            conn.execute("UPDATE jobs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), job_id))


def pending_jobs(chat_id: str, cv_hash: str, limit: int) -> List[Dict[str, Any]]:
    """Open jobs this user has no score for yet, newest first."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE job_id NOT IN (SELECT job_id FROM matches WHERE chat_id=? AND cv_hash=?) "
            "ORDER BY first_seen DESC, num DESC",
            (chat_id, cv_hash),
        ).fetchall()
    now = today()
    return [j for j in map(_row_to_job, rows) if not is_expired(j, now)][:limit]


def save_matches(chat_id: str, cv_hash: str, scored: List[Dict[str, Any]], run_id: str) -> int:
    """Stores real AI scores only; keyword-fallback results are retried on the next run."""
    rows = [
        (chat_id, str(j.get("Job_ID")), cv_hash, j.get("Match_Score"), j.get("Match_Level"), j.get("Match_Reason"), run_id)
        for j in scored if j.get("AI_Scored")
    ]
    with _db() as conn:
        current = conn.execute("SELECT cv_hash FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if not current or current["cv_hash"] != cv_hash:
            return 0  # The CV changed while this was being scored
        conn.executemany(
            "INSERT OR REPLACE INTO matches(chat_id, job_id, cv_hash, score, level, reason, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


_USER_JOBS = """
SELECT jobs.job_id, jobs.num, jobs.data, jobs.first_seen, jobs.last_seen,
       matches.score AS m_score, matches.level AS m_level, matches.reason AS m_reason, matches.run_id AS m_run,
       tailored.status AS t_status, COALESCE(tailored.applied, 0) AS t_applied
FROM matches
JOIN users ON users.chat_id = matches.chat_id AND users.cv_hash = matches.cv_hash
JOIN jobs ON jobs.job_id = matches.job_id
LEFT JOIN tailored ON tailored.chat_id = matches.chat_id AND tailored.job_id = matches.job_id
WHERE matches.chat_id = ?
"""


def active_jobs(chat_id: str, levels: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(_USER_JOBS, (chat_id,)).fetchall()
    now = today()
    jobs = [j for j in map(_user_job, rows) if not is_expired(j, now)]
    if levels:
        jobs = [j for j in jobs if j.get("Match_Level") in levels]
    jobs.sort(key=lambda x: int(x.get("Match_Score") or 0), reverse=True)
    return jobs


def new_job_ids(chat_id: str) -> List[str]:
    """Jobs scored in this user's most recent run."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT job_id FROM matches WHERE chat_id=? AND run_id=(SELECT MAX(run_id) FROM matches WHERE chat_id=?)",
            (chat_id, chat_id),
        ).fetchall()
    return [r["job_id"] for r in rows]


def last_run() -> Optional[str]:
    return get_meta("last_run")


# ---- Tailored CVs ----

def _row_to_tailored(row: sqlite3.Row) -> Dict[str, Any]:
    t = dict(row)
    t["job"] = json.loads(t["job"] or "{}")
    t["result"] = json.loads(t["result"] or "{}")
    t["applied"] = bool(t["applied"])
    return t


def get_tailored(chat_id: str, job_id: str = None, num: int = None) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        if num is not None:
            row = conn.execute("SELECT * FROM tailored WHERE chat_id=? AND num=?", (chat_id, num)).fetchone()
        else:
            row = conn.execute("SELECT * FROM tailored WHERE chat_id=? AND job_id=?", (chat_id, job_id)).fetchone()
    return _row_to_tailored(row) if row else None


def list_tailored(chat_id: str) -> List[Dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM tailored WHERE chat_id=? ORDER BY created DESC", (chat_id,)).fetchall()
    return [_row_to_tailored(r) for r in rows]


def reset_stuck_tailoring() -> None:
    """A restart mid-tailoring leaves rows on 'working' forever; mark them failed so they can be retried."""
    with _db() as conn:
        conn.execute("UPDATE tailored SET status='failed', error='Interrupted by a restart' WHERE status='working'")


def save_tailored(chat_id: str, job: Dict[str, Any], **fields) -> None:
    jid = str(job.get("Job_ID"))
    with _db() as conn:
        exists = conn.execute("SELECT 1 FROM tailored WHERE chat_id=? AND job_id=?", (chat_id, jid)).fetchone()
        if not exists:
            conn.execute("INSERT INTO tailored(chat_id, job_id, num, created) VALUES (?, ?, ?, ?)",
                         (chat_id, jid, job.get("Num"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        fields.setdefault("job", job)
        for key, value in fields.items():
            if key in ("job", "result"):
                value = json.dumps(value, ensure_ascii=False)
            elif key == "applied":
                value = int(bool(value))
            conn.execute(f"UPDATE tailored SET {key}=? WHERE chat_id=? AND job_id=?", (value, chat_id, jid))
