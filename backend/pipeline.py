import os
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List

from backend import jobs_db, notifier
from backend.matcher import match_jobs_to_cv
from backend.scraper import fetch_all_jobs

# New scorings per user per run. A big backlog (for example a new user's first list) is spread over several runs
SCORE_LIMIT = int(os.environ.get("SCORE_LIMIT_PER_USER", "200"))

_user_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _user_lock(chat_id: str) -> threading.Lock:
    with _locks_guard:
        return _user_locks.setdefault(chat_id, threading.Lock())


def score_user(chat_id: str, run_id: str, limit: int = SCORE_LIMIT) -> Dict[str, int]:
    """Scores the open jobs this user has no score for yet, against their own CV."""
    user = jobs_db.get_user(chat_id)
    if not user or user["status"] != "active":
        return {"added": 0, "failed": 0}

    lock = _user_lock(chat_id)
    if not lock.acquire(blocking=False):
        print(f"[Pipeline] Scoring already running for {chat_id}, skipping.")
        return {"added": 0, "failed": 0}
    try:
        pending = jobs_db.pending_jobs(chat_id, user["cv_hash"], limit)
        if not pending:
            return {"added": 0, "failed": 0}
        scored = match_jobs_to_cv(user["cv_path"], pending)
        added = jobs_db.save_matches(chat_id, user["cv_hash"], scored, run_id)
        return {"added": added, "failed": len(scored) - added}
    finally:
        lock.release()


def refresh_user(chat_id: str) -> None:
    """Scores anything new for one user and sends them their list. Used after a CV upload and by /run."""
    run_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = score_user(chat_id, run_id)
    if summary["failed"]:
        notifier.send_message(
            f"⚠️ Gemini failed for {summary['failed']} job(s). They'll be retried next run.", chat_id)
    notifier.send_daily_digest(chat_id, added=summary["added"])


def run_all(sources: List[str], is_fresher: bool, deadline_today: bool,
            progress: Callable[[str, str], None]) -> Dict[str, Any]:
    """One scheduled run: scrape once for everyone, then score and send each active user their own list."""
    run_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    progress("Scraping Multi-Sites", f"Fetching live jobs from {', '.join(sources)}...")
    keywords, categories = jobs_db.search_terms()
    jobs = fetch_all_jobs(sources=sources, is_fresher=is_fresher, deadline_today=deadline_today,
                          linkedin_keywords=keywords, remotive_categories=categories)
    # Sources can list the same job twice (e.g. across BDJobs pages)
    jobs = list({str(j.get("Job_ID")): j for j in jobs}.values())
    added_ids = jobs_db.upsert_jobs(jobs)
    expired = jobs_db.expire_jobs()
    jobs_db.set_meta("last_run", run_id)

    users = jobs_db.active_users()
    summaries = {}
    for i, user in enumerate(users, 1):
        chat_id = user["chat_id"]
        progress("AI Matching", f"Scoring jobs for user {i} of {len(users)}...")
        try:
            summaries[chat_id] = score_user(chat_id, run_id)
            if summaries[chat_id]["failed"]:
                notifier.send_message(
                    f"⚠️ Gemini failed for {summaries[chat_id]['failed']} job(s) "
                    "(they'll be retried next run). Check the API key / quota.", chat_id)
        except Exception as e:
            print(f"[Pipeline] Scoring failed for {chat_id}: {e}")
            summaries[chat_id] = {"added": 0, "failed": 0}

    for user in users:
        chat_id = user["chat_id"]
        if not notifier.is_telegram_chat(chat_id):
            continue
        try:
            notifier.send_daily_digest(chat_id, added=summaries[chat_id]["added"], expired=expired)
        except Exception as e:
            print(f"[Pipeline] Digest failed for {chat_id}: {e}")

    return {"added": len(added_ids), "expired": expired, "active": jobs_db.job_count(), "users": len(users)}
