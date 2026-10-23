import os
import sys
import json
import html
import requests
from typing import List, Dict, Any

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000  # Telegram hard limit is 4096
SEEN_CAP = 5000         # Keep the seen-IDs file from growing forever

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NOTIFIED_FILE = os.path.join(BASE_DIR, "data", "notified_jobs.json")


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def is_configured() -> bool:
    return bool(_token() and _chat_id())


def send_message(text: str) -> None:
    res = requests.post(
        TELEGRAM_API.format(token=_token(), method="sendMessage"),
        json={
            "chat_id": _chat_id(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if not res.ok:
        # Don't raise_for_status: its message contains the URL, which contains the bot token
        raise RuntimeError(f"Telegram sendMessage failed ({res.status_code}): {res.text[:200]}")


def _load_seen() -> List[str]:
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_seen(seen: List[str]) -> None:
    os.makedirs(os.path.dirname(NOTIFIED_FILE), exist_ok=True)
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-SEEN_CAP:], f)


def _format_job(j: Dict[str, Any]) -> str:
    e = lambda v: html.escape(str(v or ""))
    lines = [
        f"<b>{e(j.get('Match_Score'))}%</b> · <a href=\"{e(j.get('Job_URL'))}\">{e(j.get('Title'))}</a>",
        f"🏢 {e(j.get('Company'))} · 📍 {e(j.get('Location'))}",
        f"🗓 Deadline: {e(j.get('Deadline'))} · {e(j.get('Source'))}",
    ]
    if j.get("Match_Reason"):
        lines.append(f"<i>{e(j.get('Match_Reason'))}</i>")
    return "\n".join(lines)


def notify_new_matches(jobs: List[Dict[str, Any]]) -> int:
    """
    Sends jobs that Gemini scored at or above NOTIFY_MIN_SCORE and that were never sent before.
    Returns the number of jobs sent.
    """
    if not is_configured():
        print("[Telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping notification.")
        return 0

    min_score = int(os.environ.get("NOTIFY_MIN_SCORE", "70"))
    seen = _load_seen()
    seen_set = set(seen)

    new_jobs = [
        j for j in jobs
        if j.get("AI_Scored")
        and int(j.get("Match_Score") or 0) >= min_score
        and str(j.get("Job_ID")) not in seen_set
    ]
    if not new_jobs:
        print("[Telegram] No new matches to send.")
        return 0

    new_jobs.sort(key=lambda x: x.get("Match_Score", 0), reverse=True)

    header = f"🎯 <b>{len(new_jobs)} new job match{'es' if len(new_jobs) != 1 else ''}</b> (score ≥ {min_score})"
    chunks, current = [], header
    for j in new_jobs:
        block = _format_job(j)
        if len(current) + len(block) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = block
        else:
            current += "\n\n" + block
    chunks.append(current)

    for chunk in chunks:
        send_message(chunk)

    seen.extend(str(j.get("Job_ID")) for j in new_jobs)
    _save_seen(seen)
    print(f"[Telegram] Sent {len(new_jobs)} new matches in {len(chunks)} message(s).")
    return len(new_jobs)


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
    # python -m backend.notifier chat-id   -> find your chat ID
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
