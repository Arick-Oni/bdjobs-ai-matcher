import os
import re
import sys
import html
import requests
from typing import List, Dict, Any, Optional, Tuple

from backend import jobs_db

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000  # Telegram hard limit is 4096
CLOSING_SOON_DAYS = 2
BUTTONS_PER_ROW = 5


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def is_configured() -> bool:
    # Users register by messaging the bot, so the bot token is all that's needed
    return bool(_token())


def is_telegram_chat(chat_id: str) -> bool:
    """False for the web-only owner placeholder, which has no Telegram chat behind it."""
    return bool(re.fullmatch(r"-?\d+", str(chat_id)))


def min_score() -> int:
    return int(os.environ.get("NOTIFY_MIN_SCORE", "70"))


def api(method: str, http_timeout: int = 15, files=None, **payload) -> Dict[str, Any]:
    url = TELEGRAM_API.format(token=_token(), method=method)
    if files:
        res = requests.post(url, data=payload, files=files, timeout=60)
    else:
        res = requests.post(url, json=payload, timeout=http_timeout)
    if not res.ok:
        # Don't raise_for_status: its message contains the URL, which contains the bot token
        raise RuntimeError(f"Telegram {method} failed ({res.status_code}): {res.text[:200]}")
    return res.json()


def download_file(file_id: str) -> bytes:
    info = api("getFile", file_id=file_id)["result"]
    res = requests.get(f"https://api.telegram.org/file/bot{_token()}/{info['file_path']}", timeout=60)
    if not res.ok:
        # Same as api(): keep the bot token out of error messages
        raise RuntimeError(f"Telegram file download failed ({res.status_code})")
    return res.content


def _remember(chat_id: str, res: Dict[str, Any]) -> None:
    """Records what was sent so it can be recalled later (python -m backend.recall)."""
    try:
        jobs_db.log_sent_message(chat_id, res["result"]["message_id"])
    except Exception as e:
        print(f"[Telegram] Could not record sent message: {e}")


def send_message(text: str, chat_id: Optional[str] = None, buttons: Optional[List[List[Dict[str, str]]]] = None) -> None:
    payload = {"chat_id": chat_id or _chat_id(), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    _remember(payload["chat_id"], api("sendMessage", **payload))


def send_document(path: str, caption: str = "", chat_id: Optional[str] = None,
                  buttons: Optional[List[List[Dict[str, str]]]] = None) -> None:
    import json
    payload = {"chat_id": chat_id or _chat_id(), "caption": caption[:1000], "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    with open(path, "rb") as f:
        res = api("sendDocument", files={"document": (os.path.basename(path), f, "application/pdf")}, **payload)
    _remember(payload["chat_id"], res)


def tailor_buttons(nums: List[int]) -> List[List[Dict[str, str]]]:
    btns = [{"text": f"✍️ {n}", "callback_data": f"tailor:{n}"} for n in nums]
    return [btns[i:i + BUTTONS_PER_ROW] for i in range(0, len(btns), BUTTONS_PER_ROW)]


def send_long(parts: List[Tuple[str, Optional[int]]], chat_id: Optional[str] = None) -> int:
    """
    Joins (text, job_num) parts with blank lines into as few messages as fit. Each message gets a row of
    ✍️ buttons for the jobs in it. Returns message count.
    """
    chunks: List[Tuple[str, List[int]]] = []
    current, nums = "", []
    for text, num in parts:
        if current and len(current) + len(text) + 2 > MAX_MESSAGE_LEN:
            chunks.append((current, nums))
            current, nums = text, []
        else:
            current = f"{current}\n\n{text}" if current else text
        if num is not None:
            nums.append(num)
    if current:
        chunks.append((current, nums))
    for text, chunk_nums in chunks:
        send_message(text, chat_id, tailor_buttons(chunk_nums) if chunk_nums else None)
    return len(chunks)


def format_job(j: Dict[str, Any], new_ids: set = frozenset()) -> str:
    e = lambda v: html.escape(str(v or ""))
    tags = ""
    if str(j.get("Job_ID")) in new_ids:
        tags += "🆕 "
    left = jobs_db.days_left(j)
    if left is not None and left <= CLOSING_SOON_DAYS:
        tags += "⏰ "
    if j.get("Applied"):
        tags += "✅ "
    elif j.get("Tailor_Status") == "done":
        tags += "📄 "
    if left is None:
        deadline = e(j.get("Deadline"))
    elif left == 0:
        deadline = f"{e(j.get('Deadline'))} (<b>today</b>)"
    else:
        deadline = f"{e(j.get('Deadline'))} ({left}d left)"
    lines = [
        f"<b>#{j.get('Num')}</b> {tags}<b>{e(j.get('Match_Score'))}%</b> · <a href=\"{e(j.get('Job_URL'))}\">{e(j.get('Title'))}</a>",
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
    new_ids = set(jobs_db.new_job_ids(chat_id or jobs_db.OWNER_ID))
    send_long([(f"{title} ({len(jobs)})", None)] + [(format_job(j, new_ids), j.get("Num")) for j in jobs], chat_id)


def send_daily_digest(chat_id: str, added: int = 0, expired: Optional[int] = None) -> None:
    """Sends one user their list: only jobs scored against their own CV."""
    if not is_configured():
        print("[Telegram] TELEGRAM_BOT_TOKEN not set, skipping digest.")
        return

    active = jobs_db.active_jobs(chat_id)
    top = [j for j in active if int(j.get("Match_Score") or 0) >= min_score()]
    medium = [j for j in active if j.get("Match_Level") == "MEDIUM"]
    closing = [j for j in top if (jobs_db.days_left(j) is not None and jobs_db.days_left(j) <= CLOSING_SOON_DAYS)]
    new_ids = set(jobs_db.new_job_ids(chat_id))
    expired_part = f" · ❌ {expired} expired & removed" if expired is not None else ""

    header = (
        f"📋 <b>Daily job list — {jobs_db.today():%b %d}</b>\n"
        f"🆕 {added} new{expired_part} · "
        f"⏰ {len(closing)} closing within {CLOSING_SOON_DAYS}d\n"
        f"Showing {len(top)} open jobs scored ≥ {min_score()}. Tap ✍️ <i>number</i> to tailor your CV."
    )
    # New first, then by score, so today's additions are at the top
    top.sort(key=lambda j: (str(j.get("Job_ID")) not in new_ids, -int(j.get("Match_Score") or 0)))
    footer = f"/medium for {len(medium)} medium matches · /closing · /help"
    count = send_long([(header, None)] + [(format_job(j, new_ids), j.get("Num")) for j in top] + [(footer, None)], chat_id)
    print(f"[Telegram] Digest for {chat_id} sent: {len(top)} jobs in {count} message(s).")


def send_tailored_result(t: Dict[str, Any], chat_id: Optional[str] = None) -> None:
    e = lambda v: html.escape(str(v or ""))
    job, r, num = t["job"], t["result"], t.get("num")
    before, after = r.get("keywords_before", {}), r.get("keywords_after", {})

    send_document(
        t["pdf_path"],
        caption=f"📄 <b>#{num} {e(job.get('Title'))}</b> · {e(job.get('Company'))}\n"
                f"Keyword match: {before.get('percent', 0)}% → <b>{after.get('percent', 0)}%</b>",
        chat_id=chat_id,
    )

    lines = [f"✍️ <b>What I changed for #{num}</b>"]
    lines += [f"• {e(c)}" for c in r.get("changes", [])[:8]]
    if r.get("gaps"):
        lines.append("\n⚠️ <b>Gaps</b> (job asks, CV doesn't show — address in cover letter/interview)")
        lines += [f"• {e(g)}" for g in r["gaps"][:6]]
    if r.get("removed_claims"):
        lines.append(f"\n🛡 Fact-check removed {len(r['removed_claims'])} unsupported statement(s):")
        lines += [f"• <s>{e(c.get('text'))[:120]}</s>" for c in r["removed_claims"][:4]]
    if after.get("missing"):
        lines.append(f"\n🔎 Keywords still missing: {e(', '.join(after['missing'][:12]))}")

    buttons = [[
        {"text": "📝 Cover letter", "callback_data": f"cover:{num}"},
        {"text": "✅ Mark applied", "callback_data": f"applied:{num}"},
    ]]
    if job.get("Job_URL"):
        buttons.append([{"text": "🔗 Open job posting", "url": job["Job_URL"]}])
    send_message("\n".join(lines)[:MAX_MESSAGE_LEN], chat_id, buttons)


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
