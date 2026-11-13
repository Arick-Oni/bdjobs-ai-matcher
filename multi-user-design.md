# Multi-user support for the BDJobs matcher: design

Status: proposal. Nothing is implemented yet.
Scope: `bdjobs_matcher_app` (FastAPI backend, Telegram bot, SQLite `jobs.db`, Docker on the Debian server).

## 1. Current state (single user)

| Area | Today | Where |
|---|---|---|
| CV | One file, `backend/cv_store/current_cv.pdf` | `main.py` `CURRENT_CV_FILE`, `get_active_cv_path()` |
| Telegram | Bot answers only `TELEGRAM_CHAT_ID` | `telegram_bot.py` `poll_loop()` (drops other chats) |
| Notifications | Sent to `TELEGRAM_CHAT_ID` | `notifier.py` `_chat_id()` |
| Jobs | One shared `jobs` table, one Gemini score per job | `jobs_db.py` schema |
| CV change | `cv_hash` in `meta`. A different hash **deletes all rows in `jobs`** and re-scores everything | `jobs_db.py` `update()`, `split_known()` |
| Tailoring | Uses the one CV; results in `data/tailored/` and a global `tailored` table | `tailor.py`, `jobs_db.py` |
| Schedule | `SCHEDULE_TIMES` runs one scrape, one scoring pass, one notification | `main.py` scheduler |

The per-job score is the core problem: a score only makes sense against one CV, and the code stores it on the job row.

## 2. Goals

- Several people each register a CV through Telegram.
- Each person gets a daily digest of jobs ranked against **their** CV only.
- Users can run tailor/cover/applied actions on their own tracked jobs.
- Scraping stays shared, so each site is fetched once per run.
- Gemini cost grows as little as possible per user.

Non-goals (for the first version): a web UI per user, payments, and matching by any criteria other than the CV.

## 3. Proposed data model

Keep `jobs` as the shared catalogue of scraped listings. Move everything that depends on a CV into per-user tables.

```
users(
  chat_id TEXT PRIMARY KEY,        -- Telegram chat id, the identity
  display_name TEXT,
  cv_path TEXT,                    -- cv_store/<chat_id>.pdf
  cv_hash TEXT,
  status TEXT,                     -- pending | active | paused | deleted
  created_at TEXT,
  consented_at TEXT,
  daily_top_n INTEGER DEFAULT 10,
  min_score INTEGER DEFAULT 60
)

jobs(job_id PK, num, data, first_seen, last_seen)        -- shared, no score fields

user_matches(
  chat_id, job_id, cv_hash,        -- cv_hash so a CV change invalidates only that user's rows
  score, level, reason, scored_at,
  PRIMARY KEY (chat_id, job_id)
)

user_tailored(                     -- replaces the global tailored table
  chat_id, job_id, status, error, data, updated_at,
  PRIMARY KEY (chat_id, job_id)
)

user_applied(chat_id, job_id, applied_at, PRIMARY KEY (chat_id, job_id))
```

Notes:
- `num` (the `#12` shown in Telegram) stays global on `jobs`. A user's list is just a filtered view.
- `data/tailored/` files get a per-user subfolder, `data/tailored/<chat_id>/`.
- `cv_store/` becomes `cv_store/<chat_id>.pdf`. Keep the current `current_cv.pdf` as the owner's record during the migration.

## 4. Matching flow

Per scheduled run:

1. **Scrape once** for all `SCHEDULE_SOURCES`, as today. Refresh listing details in `jobs`, drop expired ones.
2. **Find the active users**: `status = 'active'`.
3. **For each user**, find jobs that have no `user_matches` row with the user's current `cv_hash`.
4. **Score only those pairs** with Gemini, batched as today (`GEMINI_BATCH_SIZE`). Each batch sends one user's CV summary plus jobs. Do not mix CVs in one prompt.
5. **Write** `user_matches` rows, then build that user's digest: top `daily_top_n` jobs with score ≥ `min_score`, excluding jobs already sent to them.
6. **Send** the digest to the user's `chat_id`. One user failing must not stop the others.

Cost: the expensive step is the Gemini call per (user, new job). With N users and J new jobs per day, that is about N×J scored pairs, against J today. Mitigations, in order of impact:
- **Pre-filter** with cheap rules before Gemini: keyword and title overlap between the job and the user's extracted skills, and location or remote preference. Only send the plausible pairs.
- **Cache the CV summary**, not the raw PDF text, so each batch sends less text.
- **Cap per user per day**, for example 150 scored pairs, so one large user list cannot exhaust the Gemini quota.
- Keep the existing Gemini key rotation (`GEMINI_API_KEYS`) in mind: all users share the same quota.

## 5. Telegram: registration and access

Flow for a new user:
1. User sends `/start` to the bot. The bot creates a `users` row with `status = pending` and replies with a short privacy notice and the consent question.
2. User replies yes, and the row becomes `consented_at = now`.
3. User sends their CV as a PDF document. The bot checks the file type and size, saves it to `cv_store/<chat_id>.pdf`, extracts the text, and sets `status = active`.
4. The bot confirms and says how to use the commands.

Access rules:
- Only `status = active` users get digests and can use tailor/cover/applied.
- `/stop` sets `paused`. `/delete` removes the CV file, the user's `user_matches`/`user_tailored`/`user_applied` rows, and the row in `users`. Confirm before deleting.
- An optional allowlist (`ALLOWED_CHAT_IDS` or an invite code) limits who can register, so strangers do not use your Gemini quota.
- The owner keeps admin commands (`/users`, `/pause <id>`), restricted to `ADMIN_CHAT_ID`.

Telegram limits to keep in mind: the bot can message only users who have started it, and document uploads have a size cap (about 20 MB via the Bot API).

## 6. Privacy and safety

- A CV contains a name, phone number, email and work history. Store the minimum: the PDF and extracted text, nothing else.
- Store the consent timestamp and a notice that says what is stored and for how long.
- Provide `/delete` and make it complete (file, DB rows, tailored outputs).
- Do not log CV text or Gemini prompts with personal data.
- Keep the Docker volume (`./backend/cv_store`) on the server out of any public share, and check its permissions.
- Tailored CVs must never be sent to another user. Every query on `user_*` tables filters by `chat_id`.
- The fact-check step in `tailor.py` stays in place for everyone.

## 7. Migration plan

1. Add the new tables next to the old ones. Do not drop anything yet.
2. Create a `users` row for your own `TELEGRAM_CHAT_ID`, pointing at the existing `current_cv.pdf`. Copy existing scores into `user_matches` with your `cv_hash`. Copy `tailored` and applied state into the `user_*` tables.
3. Switch the scheduler and the bot to the new flow, with only your user active. Run one full cycle and compare the digest with the current output.
4. Only then remove the old `jobs` score columns and the `cv_hash` delete logic.
5. Back up `jobs.db` before each step (the server has about 77 GB free, so a copy is cheap).

Known bug to fix in step 4: `jobs_db.update()` deletes every job when the CV file changes. With multiple users, one user's CV change must never delete shared jobs.

## 8. Build order

1. **Schema and migration** (step 3 above). Test on a copy of `jobs.db`.
2. **Per-user scoring** in `matcher.py` and `main.py`. Still only one user, output unchanged.
3. **Telegram registration** (`/start`, consent, CV upload, `/stop`, `/delete`).
4. **Multi-user digests** with the pre-filter and per-user caps.
5. **Admin commands** and the allowlist.
6. **Per-user tailor/applied** (`user_tailored`, `user_applied`).

Each step can be shipped and checked on its own. Steps 1 and 2 need no new users.

## 9. Open questions

- How many users are you expecting? This sets whether the pre-filter is needed in the first version.
- Should new users get all three sources, or can they choose?
- Should registration be open, or invite-only?
- Who is the admin, and should the owner see other users' CVs? (Recommended: no. Admin sees counts and status only.)
- Do tailored CVs and cover letters need to be per user? (Recommended: yes, since they contain personal data.)
- Hosting: keep the single Docker container on the Debian server, or split the bot into its own service?
