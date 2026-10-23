import os
import json
import hashlib
import threading
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "data", "jobs_db.json")

# Jobs without a deadline (LinkedIn, Remotive) expire once they haven't shown up in a scrape for this long
STALE_DAYS = int(os.environ.get("STALE_DAYS", "7"))

_lock = threading.RLock()


def today() -> date:
    return datetime.now().date()


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _load() -> Dict[str, Any]:
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cv_hash": None, "last_run": None, "jobs": {}}


def _save(db: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_FILE)


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
    except ValueError:
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
    with _lock:
        db = _load()
        if db.get("cv_hash") != file_hash(cv_path):
            return [], scraped
        known, new = [], []
        for j in scraped:
            (known if str(j.get("Job_ID")) in db["jobs"] else new).append(j)
        return known, new


def update(scraped: List[Dict[str, Any]], scored_new: List[Dict[str, Any]], cv_path: str) -> Dict[str, Any]:
    """
    Merges a run into the DB: refreshes listing details of known jobs, adds newly scored jobs,
    and drops expired ones. Returns a summary of what changed.
    """
    with _lock:
        db = _load()
        cv_hash = file_hash(cv_path)
        if db.get("cv_hash") != cv_hash:
            db = {"cv_hash": cv_hash, "last_run": None, "jobs": {}}

        now = today()
        today_str = now.isoformat()
        jobs = db["jobs"]

        for j in scraped:
            jid = str(j.get("Job_ID"))
            if jid in jobs:
                # Listing details (deadline etc.) may have changed; keep the score fields
                score_fields = {k: jobs[jid][k] for k in ("Match_Score", "Match_Level", "Match_Reason", "AI_Scored", "First_Seen") if k in jobs[jid]}
                jobs[jid] = {**j, **score_fields, "Last_Seen": today_str}

        added = []
        for j in scored_new:
            # Keyword-fallback results aren't stored, so Gemini retries them next run
            if not j.get("AI_Scored"):
                continue
            jid = str(j.get("Job_ID"))
            jobs[jid] = {**j, "First_Seen": today_str, "Last_Seen": today_str}
            added.append(jid)

        expired = [jid for jid, j in jobs.items() if is_expired(j, now)]
        for jid in expired:
            del jobs[jid]

        db["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db["last_added"] = added
        _save(db)
        return {"added": len(added), "expired": len(expired), "active": len(jobs)}


def active_jobs(levels: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    with _lock:
        db = _load()
    now = today()
    jobs = [j for j in db["jobs"].values() if not is_expired(j, now)]
    if levels:
        jobs = [j for j in jobs if j.get("Match_Level") in levels]
    jobs.sort(key=lambda x: x.get("Match_Score", 0), reverse=True)
    return jobs


def last_added_ids() -> List[str]:
    with _lock:
        return _load().get("last_added", [])


def last_run() -> Optional[str]:
    with _lock:
        return _load().get("last_run")
