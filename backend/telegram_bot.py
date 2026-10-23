import time
import threading
from typing import Callable, Dict, Any

from backend import jobs_db, notifier

COMMANDS = [
    ("high", "High-fit jobs"),
    ("medium", "Medium-fit jobs"),
    ("low", "Low-fit jobs"),
    ("new", "Jobs added in the last run"),
    ("closing", "Good matches closing within 2 days"),
    ("search", "Search titles/companies, e.g. /search python"),
    ("run", "Fetch & match jobs now"),
    ("status", "Last run and job counts"),
    ("help", "Show commands"),
]


def _help_text() -> str:
    lines = ["🤖 <b>Job Matcher commands</b> (slash optional)"]
    lines += [f"/{name} — {desc}" for name, desc in COMMANDS]
    return "\n".join(lines)


def handle_command(text: str, chat_id: str, start_run: Callable[[], str], get_status: Callable[[], Dict[str, Any]]) -> None:
    parts = text.strip().lstrip("/").split(maxsplit=1)
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]  # "/high@JoblessMatcher_bot" in group chats
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("high", "medium", "low"):
        level = cmd.upper()
        levels = [level, "IRRELEVANT"] if level == "LOW" else [level]
        notifier.send_job_list(f"🔥 <b>{level.title()} matches</b>" if level == "HIGH" else f"<b>{level.title()} matches</b>",
                               jobs_db.active_jobs(levels), chat_id)
    elif cmd == "new":
        new_ids = set(jobs_db.last_added_ids())
        notifier.send_job_list("🆕 <b>Added in the last run</b>",
                               [j for j in jobs_db.active_jobs() if str(j.get("Job_ID")) in new_ids], chat_id)
    elif cmd == "closing":
        jobs = [j for j in jobs_db.active_jobs()
                if int(j.get("Match_Score") or 0) >= notifier.min_score()
                and jobs_db.days_left(j) is not None and jobs_db.days_left(j) <= notifier.CLOSING_SOON_DAYS]
        jobs.sort(key=lambda j: jobs_db.days_left(j))
        notifier.send_job_list("⏰ <b>Closing soon</b>", jobs, chat_id)
    elif cmd == "search":
        if not arg:
            notifier.send_message("Usage: /search python", chat_id)
            return
        q = arg.lower()
        jobs = [j for j in jobs_db.active_jobs()
                if q in str(j.get("Title", "")).lower() or q in str(j.get("Company", "")).lower()]
        notifier.send_job_list(f"🔎 <b>Results for “{arg}”</b>", jobs, chat_id, empty="No open jobs match that.")
    elif cmd == "run":
        notifier.send_message(start_run(), chat_id)
    elif cmd == "status":
        st = get_status()
        active = jobs_db.active_jobs()
        counts = {lvl: sum(1 for j in active if j.get("Match_Level") == lvl) for lvl in ("HIGH", "MEDIUM", "LOW")}
        notifier.send_message(
            f"📊 <b>Status</b>\n"
            f"Last run: {jobs_db.last_run() or 'never'}\n"
            f"Now: {st.get('step')} — {st.get('message')}\n"
            f"Open jobs: {len(active)} (🔥 {counts['HIGH']} high · {counts['MEDIUM']} medium · {counts['LOW']} low)",
            chat_id,
        )
    else:
        notifier.send_message(_help_text(), chat_id)


def poll_loop(start_run: Callable[[], str], get_status: Callable[[], Dict[str, Any]]) -> None:
    allowed = notifier._chat_id()
    try:
        notifier.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except Exception as e:
        print(f"[Telegram Bot] setMyCommands failed: {e}")

    offset = None
    print("[Telegram Bot] Listening for commands...")
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = notifier.api("getUpdates", http_timeout=60, **payload).get("result", [])
        except Exception as e:
            print(f"[Telegram Bot] getUpdates error: {e}")
            time.sleep(10)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if chat_id != allowed or not text:
                continue  # Only answer the configured owner
            try:
                handle_command(text, chat_id, start_run, get_status)
            except Exception as e:
                print(f"[Telegram Bot] Command '{text}' failed: {e}")
                try:
                    notifier.send_message(f"⚠️ Command failed: {e}", chat_id)
                except Exception:
                    pass


def start(start_run: Callable[[], str], get_status: Callable[[], Dict[str, Any]]) -> None:
    if not notifier.is_configured():
        print("[Telegram Bot] Not configured, bot commands disabled.")
        return
    threading.Thread(target=poll_loop, args=(start_run, get_status), daemon=True, name="telegram-bot").start()
