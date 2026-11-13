"""
Deletes job lists the bot sent to one user. Telegram only allows this for 48 hours after sending.

    docker exec -it job-matcher python -m backend.recall CHAT_ID          # messages recorded since logging began
    docker exec -it job-matcher python -m backend.recall CHAT_ID --scan   # also find older lists by scanning the chat

--scan exists for lists sent before message IDs were recorded. The Bot API can't read a chat, so it walks message IDs
backwards from now. Bot messages with buttons (job lists) are deleted, with the buttonless Gemini warning sent right
before each digest. It stops after --runs digests, or at the first other buttonless bot message (approval / welcome).
A digest that had no warning before it can't be told apart from older buttoned messages (access requests), so set
--runs to the number of digests you want gone and check the output. User messages are never touched.
A silent placeholder is sent and removed first, to learn the newest message ID.
"""
import sys
import time
import argparse

from backend import jobs_db, notifier

MAX_SCAN = 3000


def _call(method: str, **payload) -> str:
    """'ok', or the Telegram error description. Waits out rate limits."""
    while True:
        try:
            notifier.api(method, **payload)
            return "ok"
        except RuntimeError as e:
            msg = str(e)
            if "(429)" in msg:
                time.sleep(5)
                continue
            return msg.lower()


def delete_recorded(chat_id: str) -> int:
    ids = jobs_db.sent_message_ids(chat_id)
    deleted = [m for m in ids if _call("deleteMessage", chat_id=chat_id, message_id=m) == "ok"]
    jobs_db.forget_sent_messages(chat_id, ids)  # Older than 48h can't be deleted any more either way
    return len(deleted)


def scan_and_delete(chat_id: str, max_scan: int = MAX_SCAN, runs: int = 1) -> int:
    """
    Deletes the newest `runs` digests. A digest is a block of bot messages with buttons, plus the buttonless note
    (Gemini warning) sent just before it. Older buttoned messages, such as the admin's access requests, are left alone.
    """
    res = notifier.api("sendMessage", chat_id=chat_id, text="…", disable_notification=True)
    newest = res["result"]["message_id"]
    _call("deleteMessage", chat_id=chat_id, message_id=newest)

    deleted, previous_bot, done = 0, None, 0  # previous_bot: the later bot message seen, 'list' or 'plain'
    for mid in range(newest - 1, max(newest - max_scan, 0), -1):
        # Check before touching it: editing strips buttons, which would damage a message we then leave in place
        if done >= runs:
            print(f"Stopped after {done} digest(s).")
            break
        outcome = _call("editMessageReplyMarkup", chat_id=chat_id, message_id=mid)  # no markup: strips buttons
        if outcome == "ok":
            kind = "list"
        elif "not modified" in outcome:
            kind = "plain"
        else:
            continue  # the user's own message, another chat's ID, or nothing there
        if kind == "plain" and previous_bot != "list":
            print(f"Stopped at message {mid}: a bot message from before the lists.")
            break
        if _call("deleteMessage", chat_id=chat_id, message_id=mid) == "ok":
            deleted += 1
            print(f"Deleted message {mid} ({'job list' if kind == 'list' else 'note before a list'})")
        if kind == "plain":
            done += 1  # The note opens a digest, so this digest is finished
        previous_bot = kind
        time.sleep(0.05)
    return deleted


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("chat_id")
    p.add_argument("--scan", action="store_true", help="also find lists sent before message IDs were recorded")
    p.add_argument("--max", type=int, default=MAX_SCAN, help=f"message IDs to scan back (default {MAX_SCAN})")
    p.add_argument("--runs", type=int, default=1, help="how many of the newest digests to delete (default 1)")
    p.add_argument("--allow-admin", action="store_true",
                   help="allow the admin chat (its approval requests have buttons too, so check what was deleted)")
    a = p.parse_args()
    if a.chat_id == jobs_db.OWNER_ID and not a.allow_admin:
        sys.exit("That's the admin chat. Add --allow-admin if you really mean it.")
    if not jobs_db.get_user(a.chat_id):
        sys.exit(f"No user {a.chat_id}.")
    n = delete_recorded(a.chat_id)
    if a.scan:
        n += scan_and_delete(a.chat_id, a.max, a.runs)
    print(f"Deleted {n} message(s) in chat {a.chat_id}.")


if __name__ == "__main__":
    main()
