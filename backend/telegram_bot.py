import os
import re
import html
import time
import shutil
import threading
from typing import Callable, Dict, Any, Optional

from backend import jobs_db, notifier, tailor
from backend.matcher import extract_cv_text, suggest_search_terms, REMOTIVE_CATEGORIES

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
    ("delete", "Delete your account and all your data"),
    ("help", "Show commands"),
]
ADMIN_COMMANDS = ("users", "approve", "reject", "keywords", "remotive")

MAX_CV_BYTES = 10 * 1024 * 1024
MIN_CV_CHARS = 200  # Less text than this usually means a scanned PDF with no text layer

REQUESTED = (
    "👋 <b>Welcome to the Job Matcher.</b>\n"
    "Your request has been sent to the admin. You'll get a message here as soon as it's approved."
)
STILL_PENDING = "Your request is still waiting for approval. You'll hear from us here once it's approved."
REJECTED = "Sorry, your request to use the Job Matcher wasn't approved."
APPROVED = (
    "✅ <b>You're approved.</b>\n"
    "Send me your CV as a <b>PDF file</b> (attach it as a file, not as text) to get your job list."
)
APPROVED_WITH_CV = (
    "✅ <b>You're approved.</b>\n"
    "I'm using the CV you already sent. Your job list will arrive here shortly. "
    "Send a new PDF any time to replace it."
)
ASK_CV = "Please send your CV as a <b>PDF file</b> to get started."
DELETE_CONFIRM = (
    "⚠️ <b>Delete your account?</b>\n"
    "This removes your CV, your matches and all your tailored CVs. It can't be undone."
)


class Hooks:
    """Callbacks into the app (set by main.py) so the bot doesn't import FastAPI code."""
    start_run: Callable[[str], str] = lambda chat_id: "Not available."
    get_status: Callable[[], Dict[str, Any]] = lambda: {}
    start_tailor: Callable[[str, Dict[str, Any]], str] = lambda chat_id, job: "Not available."
    on_cv_ready: Callable[[str], None] = lambda chat_id: None


def is_admin(chat_id: str) -> bool:
    """The admin is the chat in TELEGRAM_CHAT_ID. They approve new users and can see everyone's status."""
    return chat_id == jobs_db.OWNER_ID and notifier.is_telegram_chat(chat_id)


def _command_name(text: str) -> str:
    parts = text.strip().lstrip("/").split(maxsplit=1)
    return parts[0].lower().split("@")[0] if parts else ""


def _help_text(chat_id: str = "") -> str:
    lines = ["🤖 <b>Job Matcher commands</b> (slash optional)"]
    lines += [f"/{name} — {desc}" for name, desc in COMMANDS]
    if is_admin(chat_id):
        lines += ["", "<b>Admin</b>",
                  "/users — everyone and their status",
                  "/approve 123 — approve a request (use the chat ID from /users)",
                  "/reject 123 — turn down a request",
                  "/keywords — what LinkedIn searches for each user",
                  "/keywords 123 junior architect, interior designer — set a user's LinkedIn searches",
                  "/remotive 123 design, data — set a user's Remotive categories"]
    return "\n".join(lines)


def _num(arg: str) -> Optional[int]:
    m = re.search(r"\d+", arg or "")
    return int(m.group()) if m else None


def _job_or_tailored(chat_id: str, num: int) -> Optional[Dict[str, Any]]:
    """Open job by number, or the stored snapshot if it already expired but this user tailored it."""
    job = jobs_db.get_job(num=num)
    if job:
        return job
    t = jobs_db.get_tailored(chat_id, num=num)
    return t["job"] if t else None


def do_tailor(num: Optional[int], chat_id: str) -> None:
    if num is None:
        notifier.send_message("Usage: <code>tailor 12</code> — the number is shown next to each job (#12).", chat_id)
        return
    job = jobs_db.get_job(num=num)
    if not job:
        notifier.send_message(f"No open job #{num}. It may have expired.", chat_id)
        return
    t = jobs_db.get_tailored(chat_id, job_id=job["Job_ID"])
    if t and t["status"] == "working":
        notifier.send_message(f"⏳ #{num} is already being tailored — it'll arrive shortly.", chat_id)
        return
    if t and t["status"] == "done":
        notifier.send_message(f"Already tailored #{num} — sending it again. (Use <code>cv {num}</code> any time.)", chat_id)
        notifier.send_tailored_result(t, chat_id)
        return
    notifier.send_message(Hooks.start_tailor(chat_id, job), chat_id)


def do_cv(num: Optional[int], chat_id: str) -> None:
    t = jobs_db.get_tailored(chat_id, num=num) if num is not None else None
    if not t or t["status"] != "done":
        notifier.send_message(f"No tailored CV for #{num}. Send <code>tailor {num}</code> first.", chat_id)
        return
    notifier.send_tailored_result(t, chat_id)


def do_cover(num: Optional[int], chat_id: str) -> None:
    t = jobs_db.get_tailored(chat_id, num=num) if num is not None else None
    if not t or t["status"] != "done":
        notifier.send_message(f"Tailor the CV for #{num} first: <code>tailor {num}</code>", chat_id)
        return
    if not t.get("cover_letter"):
        notifier.send_message(f"📝 Writing a cover letter for #{num}…", chat_id)
    letter = tailor.cover_letter_for(chat_id, t["job_id"])
    notifier.send_message(f"📝 <b>Cover letter — #{num} {html.escape(t['job'].get('Title', ''))}</b>\n\n{html.escape(letter)}", chat_id)


def do_applied(num: Optional[int], chat_id: str) -> None:
    job = _job_or_tailored(chat_id, num) if num is not None else None
    if not job:
        notifier.send_message(f"No job #{num}.", chat_id)
        return
    existing = jobs_db.get_tailored(chat_id, job_id=job["Job_ID"])
    jobs_db.save_tailored(chat_id, job, applied=True, **({} if existing else {"status": "none"}))
    notifier.send_message(f"✅ Marked #{num} {html.escape(job.get('Title', ''))} as applied. Good luck!", chat_id)


def do_tailored_list(chat_id: str) -> None:
    rows = [t for t in jobs_db.list_tailored(chat_id) if t["status"] in ("done", "working", "failed") or t["applied"]]
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
                               jobs_db.active_jobs(chat_id, levels), chat_id)
    elif cmd == "new":
        new_ids = set(jobs_db.new_job_ids(chat_id))
        notifier.send_job_list("🆕 <b>Added in the last run</b>",
                               [j for j in jobs_db.active_jobs(chat_id) if str(j.get("Job_ID")) in new_ids], chat_id)
    elif cmd == "closing":
        jobs = [j for j in jobs_db.active_jobs(chat_id)
                if int(j.get("Match_Score") or 0) >= notifier.min_score()
                and jobs_db.days_left(j) is not None and jobs_db.days_left(j) <= notifier.CLOSING_SOON_DAYS]
        jobs.sort(key=lambda j: jobs_db.days_left(j))
        notifier.send_job_list("⏰ <b>Closing soon</b>", jobs, chat_id)
    elif cmd == "search":
        if not arg:
            notifier.send_message("Usage: /search python", chat_id)
            return
        q = arg.lower()
        jobs = [j for j in jobs_db.active_jobs(chat_id)
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
        notifier.send_message(Hooks.start_run(chat_id), chat_id)
    elif cmd == "status":
        st = Hooks.get_status()
        active = jobs_db.active_jobs(chat_id)
        counts = {lvl: sum(1 for j in active if j.get("Match_Level") == lvl) for lvl in ("HIGH", "MEDIUM", "LOW")}
        notifier.send_message(
            f"📊 <b>Status</b>\n"
            f"Last run: {jobs_db.last_run() or 'never'}\n"
            f"Now: {st.get('step')} — {st.get('message')}\n"
            f"Your open matches: {len(active)} (🔥 {counts['HIGH']} high · {counts['MEDIUM']} medium · {counts['LOW']} low)",
            chat_id,
        )
    elif cmd == "delete":
        ask_delete(chat_id)
    elif cmd in ADMIN_COMMANDS and is_admin(chat_id):
        admin_command(cmd, arg, chat_id)
    else:
        notifier.send_message(_help_text(chat_id), chat_id)


def ask_delete(chat_id: str) -> None:
    if is_admin(chat_id):
        notifier.send_message("The admin account can't be deleted from the bot.", chat_id)
        return
    if jobs_db.get_user(chat_id) is None:
        notifier.send_message("There's no account to delete.", chat_id)
        return
    notifier.send_message(DELETE_CONFIRM, chat_id,
                          [[{"text": "🗑 Yes, delete everything", "callback_data": "delete:yes"},
                            {"text": "Cancel", "callback_data": "delete:no"}]])


def delete_account(chat_id: str) -> None:
    if is_admin(chat_id) or jobs_db.get_user(chat_id) is None:
        return
    for path in jobs_db.delete_user(chat_id):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as e:
            print(f"[Telegram Bot] Could not remove {path}: {e}")
    shutil.rmtree(os.path.join(tailor.TAILORED_DIR, str(chat_id)), ignore_errors=True)
    notifier.send_message("🗑 Your account and data have been deleted. Message me again if you want to register later.", chat_id)


def _user_label(user: Dict[str, Any]) -> str:
    return html.escape(user.get("name") or "no name")


def stored_cv_text(user: Dict[str, Any]) -> Optional[str]:
    """Text of a CV the user already sent, if the file is still there, unchanged and readable."""
    path = user.get("cv_path")
    if not path or not os.path.exists(path) or jobs_db.file_hash(path) != user.get("cv_hash"):
        return None
    try:
        text = extract_cv_text(path)
    except Exception:
        return None
    return text if len(text) >= MIN_CV_CHARS else None


def activate_with_stored_cv(user: Dict[str, Any]) -> bool:
    """Skips asking for a CV again when the user already sent one (e.g. before they were approved)."""
    text = stored_cv_text(user)
    if text is None:
        return False
    chat_id = user["chat_id"]
    jobs_db.set_user_cv(chat_id, user["cv_path"], user["cv_hash"])
    notifier.send_message(APPROVED_WITH_CV, chat_id)
    update_search_terms(chat_id, text)
    Hooks.on_cv_ready(chat_id)
    return True


def decide(action: str, chat_id: str) -> None:
    """Approves or rejects a request. Used by the admin's buttons and /approve, /reject."""
    user = jobs_db.get_user(chat_id)
    if not user:
        notifier.send_message(f"No user with chat ID {html.escape(chat_id)}.", jobs_db.OWNER_ID)
        return
    if action == "approve":
        if user["status"] == "active":
            notifier.send_message(f"{html.escape(chat_id)} is already approved (active).", jobs_db.OWNER_ID)
            return
        if activate_with_stored_cv(user):
            notifier.send_message(
                f"✅ Approved {_user_label(user)} (<code>{html.escape(chat_id)}</code>) using the CV they already sent. "
                "Their list is on the way.", jobs_db.OWNER_ID)
            return
        if user["status"] == "awaiting_cv":
            notifier.send_message(f"{html.escape(chat_id)} is already approved and still needs to send a CV.", jobs_db.OWNER_ID)
            return
        jobs_db.set_user_status(chat_id, "awaiting_cv")
        notifier.send_message(APPROVED, chat_id)
        notifier.send_message(f"✅ Approved {_user_label(user)} (<code>{html.escape(chat_id)}</code>). They've been asked for a CV.", jobs_db.OWNER_ID)
    else:
        jobs_db.set_user_status(chat_id, "rejected")
        notifier.send_message(REJECTED, chat_id)
        notifier.send_message(f"❌ Rejected {_user_label(user)} (<code>{html.escape(chat_id)}</code>).", jobs_db.OWNER_ID)


def _search_line(user: Dict[str, Any]) -> str:
    s = jobs_db.get_search(user["chat_id"])
    name = "You" if user["chat_id"] == jobs_db.OWNER_ID else _user_label(user)
    return (f"<b>{name}</b> (<code>{html.escape(user['chat_id'])}</code>)\n"
            f"  LinkedIn: {html.escape(', '.join(s['keywords']) or '—')}\n"
            f"  Remotive: {html.escape(', '.join(s['remotive']) or '—')}")


def search_command(cmd: str, arg: str, chat_id: str) -> None:
    """/keywords [ID [a, b, ...]] and /remotive ID a, b: view or set what the scrapers search for a user."""
    target, _, rest = (arg or "").strip().partition(" ")
    if not target:
        lines = ["🔎 <b>What the scrapers search for</b>"] + [_search_line(u) for u in jobs_db.active_users()]
        keywords, categories = jobs_db.search_terms()
        lines.append(f"\nNext scrape: {len(keywords)} LinkedIn searches, Remotive: {html.escape(', '.join(categories) or 'defaults')}")
        notifier.send_message("\n".join(lines), chat_id)
        return
    user = jobs_db.get_user(target)
    if not user:
        notifier.send_message(f"No user with chat ID {html.escape(target)}.", chat_id)
        return
    items = [i.strip() for i in rest.split(",") if i.strip()]
    if items and cmd == "keywords":
        jobs_db.set_search(target, keywords=items)
    elif items:
        bad = [c for c in items if c.lower() not in REMOTIVE_CATEGORIES]
        if bad:
            notifier.send_message(f"Unknown Remotive categories: {html.escape(', '.join(bad))}\n"
                                  f"Choose from: {', '.join(REMOTIVE_CATEGORIES)}", chat_id)
            return
        jobs_db.set_search(target, remotive=items)
    notifier.send_message(("✅ Saved. Takes effect from the next scrape.\n" if items else "") + _search_line(jobs_db.get_user(target)), chat_id)


def admin_command(cmd: str, arg: str, chat_id: str) -> None:
    if cmd == "users":
        users = jobs_db.list_users()
        if not users:
            notifier.send_message("No one has messaged the bot yet.", chat_id)
            return
        labels = {"pending": "⏳ waiting", "awaiting_cv": "📤 approved, no CV", "active": "✅ active", "rejected": "❌ rejected"}
        lines = [f"👥 <b>Users ({len(users)})</b>"]
        for u in users[:60]:
            lines.append(f"{labels.get(u['status'], u['status'])} · {_user_label(u)} · <code>{html.escape(u['chat_id'])}</code>")
        if any(u["status"] == "pending" for u in users):
            lines.append("\nApprove with <code>approve CHAT_ID</code>, or use the buttons on the request.")
        if any(u["status"] == "active" for u in users):
            lines.append("Users can delete their own account with /delete.")
        notifier.send_message("\n".join(lines), chat_id)
    elif cmd in ("keywords", "remotive"):
        search_command(cmd, arg, chat_id)
    elif cmd in ("approve", "reject"):
        target = re.sub(r"\D", "", arg or "")  # tolerate a stray "-" or spaces; chat IDs are digits
        if not target:
            notifier.send_message(f"Usage: <code>{cmd} 123456789</code> (the chat ID from /users)", chat_id)
            return
        decide("approve" if cmd == "approve" else "reject", target)


def store_cv(doc: Dict[str, Any], chat_id: str) -> None:
    """Downloads a CV, checks it has readable text, and makes it this user's CV. Runs off the polling thread."""
    data = notifier.download_file(doc["file_id"])
    if not data.startswith(b"%PDF"):
        raise ValueError("That file isn't a valid PDF.")

    path = jobs_db.cv_path_for(chat_id)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        text = extract_cv_text(tmp)
    except Exception:
        os.remove(tmp)
        raise
    if len(text) < MIN_CV_CHARS:
        os.remove(tmp)
        raise ValueError("I couldn't read enough text from that PDF. If it's a scan or an image, please send a text-based PDF.")

    os.replace(tmp, path)
    jobs_db.set_user_cv(chat_id, path, jobs_db.file_hash(path))
    notifier.send_message("✅ CV saved. I'm matching open jobs to it now, and your list will arrive here shortly.", chat_id)
    update_search_terms(chat_id, text)
    Hooks.on_cv_ready(chat_id)


def update_search_terms(chat_id: str, cv_text: str) -> None:
    """What LinkedIn/Remotive should search for this user from the next scrape on. Keeps the old terms on failure."""
    try:
        terms = suggest_search_terms(cv_text)
        if terms["keywords"]:
            jobs_db.set_search(chat_id, terms["keywords"], terms["remotive"])
            print(f"[Telegram Bot] Search terms for {chat_id}: {terms}")
    except Exception as e:
        print(f"[Telegram Bot] Could not suggest search terms for {chat_id}: {e}")


def receive_cv(doc: Dict[str, Any], chat_id: str) -> None:
    name = str(doc.get("file_name") or "")
    if doc.get("mime_type") != "application/pdf" and not name.lower().endswith(".pdf"):
        notifier.send_message("That's not a PDF. Please send your CV as a PDF file.", chat_id)
        return
    if int(doc.get("file_size") or 0) > MAX_CV_BYTES:
        notifier.send_message("That file is over 10 MB. Please send a smaller PDF.", chat_id)
        return
    notifier.send_message("📥 Got your CV, reading it now…", chat_id)
    _dispatch(chat_id, store_cv, doc, chat_id)


def request_access(msg: Dict[str, Any], chat_id: str) -> None:
    """A stranger's first message: they wait for the admin to approve them."""
    sender = msg.get("from") or {}
    name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)
    if sender.get("username"):
        name = f"{name} (@{sender['username']})".strip()
    jobs_db.create_user(chat_id, status="pending", name=name or chat_id)
    notifier.send_message(REQUESTED, chat_id)
    try:
        notifier.send_message(
            f"🆕 <b>New access request</b>\n{html.escape(name or 'No name')}\n<code>{html.escape(chat_id)}</code>",
            jobs_db.OWNER_ID,
            [[{"text": "✅ Approve", "callback_data": f"approve:{chat_id}"},
              {"text": "❌ Reject", "callback_data": f"reject:{chat_id}"}]],
        )
    except Exception as e:
        print(f"[Telegram Bot] Could not notify admin of request from {chat_id}: {e}")


def handle_message(msg: Dict[str, Any], chat_id: str) -> None:
    """New users need admin approval, then a CV, before any other feature works."""
    doc = msg.get("document")
    text = msg.get("text") or ""
    if jobs_db.get_user(chat_id) is None:
        request_access(msg, chat_id)
        return
    user = jobs_db.get_user(chat_id)

    if user["status"] == "pending":
        notifier.send_message(STILL_PENDING, chat_id)
        return
    if user["status"] == "rejected":
        notifier.send_message(REJECTED, chat_id)
        return
    if text and _command_name(text) == "delete":
        ask_delete(chat_id)  # Works at any stage, so a user can leave before or after they register
        return
    if doc:
        receive_cv(doc, chat_id)  # Also how an active user replaces their CV
        return
    if text and is_admin(chat_id) and _command_name(text) in ADMIN_COMMANDS:
        handle_command(text, chat_id)  # Admins don't need a CV to manage users
        return
    if user["status"] != "active":
        notifier.send_message(ASK_CV, chat_id)
        return
    if text:
        handle_command(text, chat_id)


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

    if action in ("approve", "reject") and is_admin(chat_id):
        target = re.sub(r"\D", "", arg or "")
        if target:
            decide(action, target)
        return
    if action == "delete":
        if arg == "yes":
            delete_account(chat_id)
        else:
            notifier.send_message("Cancelled. Your account is unchanged.", chat_id)
        return

    user = jobs_db.get_user(chat_id)
    if not user or user["status"] != "active":
        return
    if action == "tailor":
        do_tailor(num, chat_id)
    elif action == "cover":
        do_cover(num, chat_id)
    elif action == "applied":
        do_applied(num, chat_id)


def _dispatch(chat_id: str, fn, *args) -> None:
    # Long actions (cover letters, CV parsing) run off the polling thread so the bot stays responsive
    def run():
        try:
            fn(*args)
        except Exception as e:
            print(f"[Telegram Bot] Action failed for {chat_id}: {e}")
            try:
                notifier.send_message(f"⚠️ Failed: {html.escape(str(e))[:300]}", chat_id)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


def poll_loop() -> None:
    try:
        notifier.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except Exception as e:
        print(f"[Telegram Bot] setMyCommands failed: {e}")

    offset = None
    print("[Telegram Bot] Listening for messages...")
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
                chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
                if chat_id:
                    _dispatch(chat_id, handle_callback, cq)
                continue
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            # Private chats only: a group would see other people's job lists and CVs
            if chat.get("type") != "private" or not chat_id:
                continue
            if not (msg.get("text") or msg.get("document")):
                continue
            _dispatch(chat_id, handle_message, msg, chat_id)


def start(start_run: Callable[[str], str], get_status: Callable[[], Dict[str, Any]],
          start_tailor: Callable[[str, Dict[str, Any]], str], on_cv_ready: Callable[[str], None]) -> None:
    Hooks.start_run, Hooks.get_status, Hooks.start_tailor, Hooks.on_cv_ready = start_run, get_status, start_tailor, on_cv_ready
    if not notifier.is_configured():
        print("[Telegram Bot] Not configured, bot commands disabled.")
        return
    threading.Thread(target=poll_loop, daemon=True, name="telegram-bot").start()
