import re
import html
import time
import threading
from typing import Callable, Dict, Any, Optional

from backend import jobs_db, notifier, tailor

COMMANDS = [
    ("high", "High-fit jobs"),
    ("medium", "Medium-fit jobs"),
    ("low", "Low-fit jobs"),
    ("new", "Jobs added in the last run"),
    ("closing", "Good matches closing within 2 days"),
    ("search", "Search titles/companies, e.g. /search python"),
    ("tailor", "Tailor your CV to a job, e.g. /tailor 12"),
    ("cv", "Re-send a tailored CV, e.g. /cv 12"),
    ("cover", "Cover letter for a tailored job, e.g. /cover 12"),
    ("applied", "Mark a job as applied, e.g. /applied 12"),
    ("tailored", "Your tailored CVs and applications"),
    ("run", "Fetch & match jobs now"),
    ("status", "Last run and job counts"),
    ("help", "Show commands"),
]


class Hooks:
    """Callbacks into the app (set by main.py) so the bot doesn't import FastAPI code."""
    start_run: Callable[[], str] = lambda: "Not available."
    get_status: Callable[[], Dict[str, Any]] = lambda: {}
    start_tailor: Callable[[Dict[str, Any]], str] = lambda job: "Not available."


def _help_text() -> str:
    lines = ["🤖 <b>Job Matcher commands</b> (slash optional)"]
    lines += [f"/{name} — {desc}" for name, desc in COMMANDS]
    return "\n".join(lines)


def _num(arg: str) -> Optional[int]:
    m = re.search(r"\d+", arg or "")
    return int(m.group()) if m else None


def _job_or_tailored(num: int) -> Optional[Dict[str, Any]]:
    """Open job by number, or the stored snapshot if it already expired but was tailored."""
    job = jobs_db.get_job(num=num)
    if job:
        return job
    t = jobs_db.get_tailored(num=num)
    return t["job"] if t else None


def do_tailor(num: Optional[int], chat_id: str) -> None:
    if num is None:
        notifier.send_message("Usage: <code>tailor 12</code> — the number is shown next to each job (#12).", chat_id)
        return
    job = jobs_db.get_job(num=num)
    if not job:
        notifier.send_message(f"No open job #{num}. It may have expired.", chat_id)
        return
    t = jobs_db.get_tailored(job_id=job["Job_ID"])
    if t and t["status"] == "working":
        notifier.send_message(f"⏳ #{num} is already being tailored — it'll arrive shortly.", chat_id)
        return
    if t and t["status"] == "done":
        notifier.send_message(f"Already tailored #{num} — sending it again. (Use <code>cv {num}</code> any time.)", chat_id)
        notifier.send_tailored_result(t, chat_id)
        return
    notifier.send_message(Hooks.start_tailor(job), chat_id)


def do_cv(num: Optional[int], chat_id: str) -> None:
    t = jobs_db.get_tailored(num=num) if num is not None else None
    if not t or t["status"] != "done":
        notifier.send_message(f"No tailored CV for #{num}. Send <code>tailor {num}</code> first.", chat_id)
        return
    notifier.send_tailored_result(t, chat_id)


def do_cover(num: Optional[int], chat_id: str) -> None:
    t = jobs_db.get_tailored(num=num) if num is not None else None
    if not t or t["status"] != "done":
        notifier.send_message(f"Tailor the CV for #{num} first: <code>tailor {num}</code>", chat_id)
        return
    if not t.get("cover_letter"):
        notifier.send_message(f"📝 Writing a cover letter for #{num}…", chat_id)
    letter = tailor.cover_letter_for(t["job_id"])
    notifier.send_message(f"📝 <b>Cover letter — #{num} {html.escape(t['job'].get('Title', ''))}</b>\n\n{html.escape(letter)}", chat_id)


def do_applied(num: Optional[int], chat_id: str) -> None:
    job = _job_or_tailored(num) if num is not None else None
    if not job:
        notifier.send_message(f"No job #{num}.", chat_id)
        return
    jobs_db.save_tailored(job, applied=True, **({} if jobs_db.get_tailored(job_id=job["Job_ID"]) else {"status": "none"}))
    notifier.send_message(f"✅ Marked #{num} {html.escape(job.get('Title', ''))} as applied. Good luck!", chat_id)


def do_tailored_list(chat_id: str) -> None:
    rows = [t for t in jobs_db.list_tailored() if t["status"] in ("done", "working", "failed") or t["applied"]]
    if not rows:
        notifier.send_message("No tailored CVs yet. Tap ✍️ under any job, or send <code>tailor 12</code>.", chat_id)
        return
    icon = {"done": "📄", "working": "⏳", "failed": "❌", "none": "•"}
    lines = ["🗂 <b>Your tailored CVs & applications</b>"]
    for t in rows[:40]:
        j = t["job"]
        lines.append(f"{icon.get(t['status'], '•')}{' ✅' if t['applied'] else ''} <b>#{t['num']}</b> "
                     f"{html.escape(j.get('Title', ''))} · {html.escape(j.get('Company', ''))}")
    lines.append("\n📄 tailored · ✅ applied · <code>cv N</code> to re-send · <code>cover N</code> for a letter")
    notifier.send_message("\n".join(lines), chat_id)


def handle_command(text: str, chat_id: str) -> None:
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
        notifier.send_job_list(f"🔎 <b>Results for “{html.escape(arg)}”</b>", jobs, chat_id, empty="No open jobs match that.")
    elif cmd == "tailor":
        do_tailor(_num(arg), chat_id)
    elif cmd == "cv":
        do_cv(_num(arg), chat_id)
    elif cmd == "cover":
        do_cover(_num(arg), chat_id)
    elif cmd == "applied":
        do_applied(_num(arg), chat_id)
    elif cmd == "tailored":
        do_tailored_list(chat_id)
    elif cmd == "run":
        notifier.send_message(Hooks.start_run(), chat_id)
    elif cmd == "status":
        st = Hooks.get_status()
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


def handle_callback(cq: Dict[str, Any]) -> None:
    data = cq.get("data", "")
    chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
    action, _, arg = data.partition(":")
    num = _num(arg)
    toast = {"tailor": f"Tailoring #{num}…", "cover": "Writing cover letter…", "applied": "Marked as applied"}.get(action, "")
    try:
        notifier.api("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
    except Exception:
        pass
    if action == "tailor":
        do_tailor(num, chat_id)
    elif action == "cover":
        do_cover(num, chat_id)
    elif action == "applied":
        do_applied(num, chat_id)


def _dispatch(fn, *args) -> None:
    # Long actions (cover letters) run off the polling thread so the bot stays responsive
    def run():
        try:
            fn(*args)
        except Exception as e:
            print(f"[Telegram Bot] Action failed: {e}")
            try:
                notifier.send_message(f"⚠️ Failed: {html.escape(str(e))[:300]}")
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


def poll_loop() -> None:
    allowed = notifier._chat_id()
    try:
        notifier.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except Exception as e:
        print(f"[Telegram Bot] setMyCommands failed: {e}")

    offset = None
    print("[Telegram Bot] Listening for commands...")
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = notifier.api("getUpdates", http_timeout=60, **payload).get("result", [])
        except Exception as e:
            print(f"[Telegram Bot] getUpdates error: {e}")
            time.sleep(10)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            if "callback_query" in upd:
                cq = upd["callback_query"]
                if str((cq.get("from") or {}).get("id", "")) == allowed or \
                        str(((cq.get("message") or {}).get("chat") or {}).get("id", "")) == allowed:
                    _dispatch(handle_callback, cq)
                continue
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if chat_id != allowed or not text:
                continue  # Only answer the configured owner
            _dispatch(handle_command, text, chat_id)


def start(start_run: Callable[[], str], get_status: Callable[[], Dict[str, Any]],
          start_tailor: Callable[[Dict[str, Any]], str]) -> None:
    Hooks.start_run, Hooks.get_status, Hooks.start_tailor = start_run, get_status, start_tailor
    if not notifier.is_configured():
        print("[Telegram Bot] Not configured, bot commands disabled.")
        return
    threading.Thread(target=poll_loop, daemon=True, name="telegram-bot").start()
