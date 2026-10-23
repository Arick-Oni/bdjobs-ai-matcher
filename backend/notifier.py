import os
import sys
import html
import requests
from typing import List, Dict, Any, Optional

from backend import jobs_db

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000  # Telegram hard limit is 4096
CLOSING_SOON_DAYS = 2


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def is_configured() -> bool:
    return bool(_token() and _chat_id())


def min_score() -> int:
    return int(os.environ.get("NOTIFY_MIN_SCORE", "70"))


def api(method: str, http_timeout: int = 15, **payload) -> Dict[str, Any]:
    res = requests.post(TELEGRAM_API.format(token=_token(), method=method), json=payload, timeout=http_timeout)
    if not res.ok:
        # Don't raise_for_status: its message contains the URL, which contains the bot token
        raise RuntimeError(f"Telegram {method} failed ({res.status_code}): {res.text[:200]}")
    return res.json()


def send_message(text: str, chat_id: Optional[str] = None) -> None:
    api("sendMessage", chat_id=chat_id or _chat_id(), text=text, parse_mode="HTML", disable_web_page_preview=True)


def send_long(parts: List[str], chat_id: Optional[str] = None) -> int:
    """Joins parts with blank lines, splitting into as few messages as fit. Returns message count."""
    chunks, current = [], ""
    for part in parts:
        if current and len(current) + len(part) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part
    if current:
        chunks.append(current)
    for chunk in chunks:
        send_message(chunk, chat_id)
    return len(chunks)


def format_job(j: Dict[str, Any], new_ids: set = frozenset()) -> str:
    e = lambda v: html.escape(str(v or ""))
    tags = ""
    if str(j.get("Job_ID")) in new_ids:
        tags += "🆕 "
    left = jobs_db.days_left(j)
    if left is not None and left <= CLOSING_SOON_DAYS:
        tags += "⏰ "
    if left is None:
        deadline = e(j.get("Deadline"))
    elif left == 0:
        deadline = f"{e(j.get('Deadline'))} (<b>today</b>)"
    else:
        deadline = f"{e(j.get('Deadline'))} ({left}d left)"
    lines = [
        f"{tags}<b>{e(j.get('Match_Score'))}%</b> · <a href=\"{e(j.get('Job_URL'))}\">{e(j.get('Title'))}</a>",
        f"🏢 {e(j.get('Company'))} · 📍 {e(j.get('Location'))}",
        f"🗓 {deadline} · {e(j.get('Source'))}",
    ]
    if j.get("Match_Reason"):
        lines.append(f"<i>{e(j.get('Match_Reason'))}</i>")
    return "\n".join(lines)


def send_job_list(title: str, jobs: List[Dict[str, Any]], chat_id: Optional[str] = None, empty: str = "Nothing here right now.") -> None:
    if not jobs:
        send_message(f"{title}\n\n{empty}", chat_id)
        return
    new_ids = set(jobs_db.last_added_ids())
    send_long([f"{title} ({len(jobs)})"] + [format_job(j, new_ids) for j in jobs], chat_id)


def send_daily_digest(summary: Dict[str, Any]) -> None:
    if not is_configured():
        print("[Telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping digest.")
        return

    active = jobs_db.active_jobs()
    top = [j for j in active if int(j.get("Match_Score") or 0) >= min_score()]
    medium = [j for j in active if j.get("Match_Level") == "MEDIUM"]
    closing = [j for j in top if (jobs_db.days_left(j) is not None and jobs_db.days_left(j) <= CLOSING_SOON_DAYS)]
    new_ids = set(jobs_db.last_added_ids())

    header = (
        f"📋 <b>Daily job list — {jobs_db.today():%b %d}</b>\n"
        f"🆕 {summary.get('added', 0)} new · ❌ {summary.get('expired', 0)} expired & removed · "
        f"⏰ {len(closing)} closing within {CLOSING_SOON_DAYS}d\n"
        f"Showing {len(top)} open jobs scored ≥ {min_score()}."
    )
    # New first, then by score, so today's additions are at the top
    top.sort(key=lambda j: (str(j.get("Job_ID")) not in new_ids, -int(j.get("Match_Score") or 0)))
    footer = f"/medium for {len(medium)} medium matches · /closing · /help"
    count = send_long([header] + [format_job(j, new_ids) for j in top] + [footer])
    print(f"[Telegram] Daily digest sent: {len(top)} jobs in {count} message(s).")


def print_chat_ids() -> None:
    """Lists chat IDs that have messaged the bot. Send your bot any message first."""
    res = requests.get(TELEGRAM_API.format(token=_token(), method="getUpdates"), timeout=15)
    if not res.ok:
        print(f"getUpdates failed ({res.status_code}): {res.text[:200]}")
        return
    chats = {}
    for upd in res.json().get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start / send it 'hi', then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


if __name__ == "__main__":
    # python -m backend.notifier chat-id   -> find your chat ID (stop the container first: the bot is polling)
    # python -m backend.notifier test      -> send a test message
    from dotenv import load_dotenv
    load_dotenv()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if not _token():
        sys.exit("TELEGRAM_BOT_TOKEN is not set in .env")
    if cmd == "chat-id":
        print_chat_ids()
    else:
        if not _chat_id():
            sys.exit("TELEGRAM_CHAT_ID is not set in .env (run: python -m backend.notifier chat-id)")
        send_message("✅ AI Job Matcher is connected. You'll get new job matches here.")
        print("Test message sent.")
