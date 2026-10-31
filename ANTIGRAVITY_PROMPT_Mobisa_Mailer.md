# Build prompt: Mobisa Mailer (private cold-email tool)

> **Paste this whole file into Antigravity as the task.** Build milestone by milestone (section 17). After each milestone, stop, show me what works, and wait for my OK before continuing. If anything in this spec is unclear or seems wrong, ask me before guessing.

---

## 0. Instructions for the agent

- You are building a **private, single-user desktop tool** for Mobisa (an AI creative agency) to send cold emails from its own Google Workspace inboxes. It is not a product for sale and has no login, no cloud hosting, no multi-user support.
- Target machine: **Windows 10/11**, Python **3.14** is installed. Work in a virtual environment. If any package fails to install on 3.14, tell me and switch the venv to Python 3.12 (`py -3.12 -m venv .venv`).
- Project root: **`D:\MobisaMailer\`**. Keep everything inside it.
- Code quality rules:
  - Type hints and short docstrings on every public function.
  - Use the `logging` module, never bare `print` in library code.
  - No secrets in code or logs. Never log OAuth tokens or full email bodies at INFO level.
  - Keep modules small and single-purpose, matching the file structure in section 3.
  - Every Gmail API call goes through `gmail_client.py`, so it can be swapped for a fake in tests.
  - Write pytest tests for all pure logic (section 16).
- Do not add features that are not in this spec (no open/click tracking, no HTML emails, no warm-up network, no AI writing). Where the spec leaves a small detail open, pick the simplest option and note it in `README.md`.

---

## 1. What the tool does

1. Imports leads from Excel/CSV.
2. Connects several Google Workspace inboxes through the Gmail API (OAuth).
3. Sends personalized **plain-text** emails in a multi-step sequence (default 3 steps: day 0, day 3, day 7). Follow-ups go **in the same Gmail thread**.
4. Spreads sending across inboxes with daily caps, a gradual **ramp-up** for new inboxes, random pauses between sends, and only during the **recipient's business hours**.
5. Watches the inboxes and **automatically stops** a lead's sequence when they reply, opt out ("stop") or bounce. Opted-out and bounced addresses go on a permanent suppression list.
6. Auto-pauses an inbox when something looks wrong (bounces, quota errors).
7. Everything is controlled from a **local Streamlit dashboard**, and a **background worker** does the sending. Windows Task Scheduler starts the worker every evening.

---

## 2. Stack

| Part | Choice |
|---|---|
| Language | Python 3.14 (fallback 3.12) |
| Dashboard | Streamlit |
| Database | SQLite (single file, WAL mode, foreign keys on) |
| Gmail | `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2` |
| Data files | `pandas`, `openpyxl` |
| Config | `PyYAML` |
| Timezones | standard `zoneinfo` + **`tzdata`** package (Windows has no system timezone database, so `tzdata` is required) |
| Tests | `pytest` |

`requirements.txt` (no exact pins unless an install fails; then pin what works):
```
streamlit
pandas
openpyxl
pyyaml
google-api-python-client
google-auth
google-auth-oauthlib
google-auth-httplib2
tzdata
pytest
```

---

## 3. File structure

```
D:\MobisaMailer\
├── app.py                     # Streamlit entry point: page navigation only
├── worker.py                  # Background sending worker: python worker.py --run
├── requirements.txt
├── settings.yaml              # User-editable settings (section 6)
├── README.md                  # Setup + daily use instructions (you write this)
├── .gitignore                 # secrets/, data/, logs/, exports/, .venv/
│
├── mailer\                    # Core package
│   ├── __init__.py
│   ├── config.py              # Load + validate settings.yaml into a dataclass
│   ├── db.py                  # SQLite connection, schema creation, all queries
│   ├── gmail_client.py        # OAuth, send, fetch messages, labels (the ONLY Gmail code)
│   ├── importer.py            # Excel/CSV import, cleaning, dedupe, suppression check
│   ├── templating.py          # Merge fields, spintax, footer, name cleanup, preview
│   ├── scheduling.py          # Recipient timezone, send windows, ramp caps, queue order
│   ├── engine.py              # One worker "tick": pick inbox + lead, send, record
│   ├── detection.py           # Classify inbox messages: reply / opt-out / bounce / auto-reply
│   ├── safety.py              # Auto-pause rules, rolling 24h counts, kill switch
│   ├── exporter.py            # Export leads / send log to Excel
│   └── seed.py                # Default rows: steps, templates, languages, timezones, category phrases
│
├── pages\                     # Streamlit pages (one file per page, section 13)
│   ├── 1_Overview.py
│   ├── 2_Import.py
│   ├── 3_Inboxes.py
│   ├── 4_Templates.py
│   ├── 5_Campaign.py
│   ├── 6_Leads.py
│   ├── 7_Replies.py
│   └── 8_Log.py
│
├── scripts\
│   ├── register_task.ps1      # Registers the Windows scheduled task (section 14)
│   ├── start_dashboard.bat    # Activates venv, runs: streamlit run app.py
│   └── start_worker.bat       # Activates venv, runs: python worker.py --run
│
├── tests\
│   ├── test_templating.py
│   ├── test_scheduling.py
│   ├── test_detection.py
│   ├── test_importer.py
│   ├── test_engine.py         # Uses FakeGmailClient
│   └── fakes.py               # FakeGmailClient implementing the same interface
│
├── secrets\                   # NEVER commit or share
│   ├── credentials.json       # Google OAuth client (user downloads it, section 5)
│   └── tokens\<email>.json    # One token per connected inbox
├── data\mailer.db
├── logs\mailer_YYYY-MM-DD.log
└── exports\
```

---

## 4. Database schema (`mailer/db.py`)

Create on first run if missing. Use ISO-8601 UTC strings for all timestamps.

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_state (          -- runtime flags
  key   TEXT PRIMARY KEY,                        -- e.g. paused_all, worker_heartbeat, worker_pid, last_detection_run
  value TEXT
);

CREATE TABLE IF NOT EXISTS inboxes (
  id               INTEGER PRIMARY KEY,
  email            TEXT NOT NULL UNIQUE,
  display_name     TEXT NOT NULL,              -- shown as sender name, e.g. "Beedu from Mobisa"
  sender_title     TEXT DEFAULT '',
  role             TEXT NOT NULL DEFAULT 'send' CHECK (role IN ('send','monitor')),  -- monitor = only read (e.g. unsubscribe alias inbox)
  active           INTEGER NOT NULL DEFAULT 1,
  paused_reason    TEXT,                       -- NULL when not paused
  paused_until     TEXT,                       -- NULL = until manually resumed
  ramp_start_date  TEXT NOT NULL,              -- date sending started
  ramp_start_cap   INTEGER NOT NULL DEFAULT 10,
  ramp_increment   INTEGER NOT NULL DEFAULT 5,
  ramp_every_days  INTEGER NOT NULL DEFAULT 3,
  max_cap          INTEGER NOT NULL DEFAULT 80,
  next_send_after  TEXT,                       -- earliest time this inbox may send again
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
  id               INTEGER PRIMARY KEY,
  email            TEXT NOT NULL UNIQUE,       -- lowercase, trimmed
  business_name    TEXT NOT NULL DEFAULT '',
  category         TEXT DEFAULT '',            -- raw category from the import file
  country          TEXT DEFAULT '',
  location         TEXT DEFAULT '',
  website          TEXT DEFAULT '',
  instagram        TEXT DEFAULT '',
  email_type       TEXT DEFAULT '',
  priority         INTEGER DEFAULT 5,          -- 1 best .. 5
  quality          TEXT DEFAULT '',            -- Good / Weak (Bad is never imported)
  language         TEXT NOT NULL DEFAULT 'en',
  custom_line      TEXT DEFAULT '',            -- optional per-lead personalization ({{custom_line}})
  inbox_id         INTEGER REFERENCES inboxes(id),  -- fixed for the whole sequence once step 1 is sent
  step             INTEGER NOT NULL DEFAULT 0, -- 0 = nothing sent yet; n = step n sent
  status           TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','in_sequence','replied','unsubscribed','bounced','finished','skipped')),
  thread_id        TEXT,
  first_subject    TEXT,                       -- subject of step 1, used for "Re: ..." follow-ups
  last_message_id  TEXT,                       -- RFC 5322 Message-ID header of the last sent email
  last_sent_at     TEXT,
  next_due_at      TEXT,                       -- when the next step may be sent
  import_id        INTEGER REFERENCES imports(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, next_due_at);
CREATE INDEX IF NOT EXISTS idx_leads_thread ON leads(thread_id);

CREATE TABLE IF NOT EXISTS steps (
  step_no     INTEGER PRIMARY KEY,             -- 1..N
  delay_days  INTEGER NOT NULL,                -- days after the previous step (step 1 = 0)
  active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS languages (
  code         TEXT PRIMARY KEY,               -- 'en', 'fr', later 'es','nl','pl','sv'
  name         TEXT NOT NULL,
  footer_text  TEXT NOT NULL                   -- opt-out footer in that language
);

CREATE TABLE IF NOT EXISTS templates (
  step_no   INTEGER NOT NULL REFERENCES steps(step_no),
  language  TEXT NOT NULL REFERENCES languages(code),
  subject   TEXT NOT NULL DEFAULT '',          -- only used for step 1
  body      TEXT NOT NULL,
  PRIMARY KEY (step_no, language)
);

CREATE TABLE IF NOT EXISTS country_settings (
  country   TEXT PRIMARY KEY,                  -- exact text as in the import file
  language  TEXT NOT NULL REFERENCES languages(code),
  timezone  TEXT NOT NULL,                     -- IANA name
  skip      INTEGER NOT NULL DEFAULT 0,        -- 1 = never email this country
  sort_order INTEGER NOT NULL DEFAULT 100      -- lower = sent earlier
);

CREATE TABLE IF NOT EXISTS category_phrases (
  raw        TEXT PRIMARY KEY,                 -- raw category text, lowercase
  phrase_en  TEXT NOT NULL,
  phrase_fr  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
  email     TEXT PRIMARY KEY,
  reason    TEXT NOT NULL CHECK (reason IN ('unsubscribed','bounced','manual')),
  added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS send_log (
  id               INTEGER PRIMARY KEY,
  ts               TEXT NOT NULL,
  inbox_id         INTEGER REFERENCES inboxes(id),
  lead_id          INTEGER REFERENCES leads(id),   -- NULL for test sends
  lead_email       TEXT NOT NULL,
  step_no          INTEGER NOT NULL,
  subject          TEXT,
  gmail_message_id TEXT,
  thread_id        TEXT,
  result           TEXT NOT NULL CHECK (result IN ('sent','dry_run','error','test')),
  error_text       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_send_once ON send_log(lead_id, step_no) WHERE result = 'sent';

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  inbox_id    INTEGER REFERENCES inboxes(id),
  lead_id     INTEGER REFERENCES leads(id),
  email       TEXT,
  type        TEXT NOT NULL CHECK (type IN ('reply','unsubscribe','bounce','auto_reply_ignored')),
  gmail_message_id TEXT UNIQUE,                -- prevents processing the same message twice
  subject     TEXT,
  snippet     TEXT,
  handled     INTEGER NOT NULL DEFAULT 0        -- ticked on the Replies page
);

CREATE TABLE IF NOT EXISTS imports (
  id              INTEGER PRIMARY KEY,
  filename        TEXT NOT NULL,
  sheet           TEXT,
  imported_count  INTEGER NOT NULL,
  skipped_json    TEXT NOT NULL,               -- {"reason": count}
  created_at      TEXT NOT NULL
);
```

`db.py` exposes plain functions (`get_conn()`, `init_db()`, and typed query helpers). The dashboard and worker run as separate processes on the same file, so keep transactions short and set `timeout=30` on connections.

---

## 5. Google / Gmail setup and auth (`mailer/gmail_client.py`)

**One-time setup (write these steps into README.md for me):**
1. Google Cloud Console: create project "Mobisa Mailer", enable **Gmail API**.
2. OAuth consent screen: user type **Internal** (Workspace), app name "Mobisa Mailer".
3. Credentials: create **OAuth client ID**, type **Desktop app**, download JSON, save as `secrets\credentials.json`.
4. Scopes: `https://www.googleapis.com/auth/gmail.send` and `https://www.googleapis.com/auth/gmail.modify`.

**Code:**
- `connect_inbox(email: str) -> str`: run `InstalledAppFlow.run_local_server(port=0, login_hint=email)`, then call `users.getProfile` and **verify the returned address equals `email`** (error if a different account was picked). Save token to `secrets\tokens\<email>.json`. Return the verified address.
- `get_service(email) -> Resource`: load token, refresh with `google.auth.transport.requests.Request()` when expired, save the refreshed token. If refresh fails, raise `AuthExpired` so the dashboard shows "Reconnect inbox".
- `send_message(email, to, subject, body, display_name, thread_id=None, in_reply_to=None, list_unsubscribe=None) -> SentResult`:
  - Build an RFC 5322 message with `email.message.EmailMessage`, plain text, UTF-8.
  - Headers: `From: "{display_name}" <{email}>`, `To`, `Subject`; when following up, also `In-Reply-To: <id>` and `References: <id>`; `List-Unsubscribe: <mailto:{list_unsubscribe}?subject=unsubscribe>`.
  - Send via `users.messages.send(userId='me', body={'raw': base64url, 'threadId': thread_id})`.
  - Then `users.messages.get(..., format='metadata', metadataHeaders=['Message-ID'])` to read the real Message-ID header.
  - Return `SentResult(gmail_message_id, thread_id, rfc_message_id)`.
- `list_recent_messages(email, query) -> list[MessageMeta]`: `users.messages.list` with paging, then `users.messages.get` (format `metadata` with headers From, To, Subject, Auto-Submitted, X-Autoreply, X-Autorespond, Precedence, X-Failed-Recipients; plus `snippet`, `threadId`).
- `get_message_text(email, msg_id) -> str`: format `full`, decode text/plain parts (used only for bounce parsing).
- `ensure_label(email, name) -> label_id` and `add_label(email, msg_id, label_id)`. Labels: `Mobisa/Replied`, `Mobisa/Unsubscribed`, `Mobisa/Bounced`.
- Map Gmail errors to custom exceptions: `RateLimited` (429, `rateLimitExceeded`, `userRateLimitExceeded`), `QuotaExceeded` (`dailyLimitExceeded`, "Daily user sending limit exceeded"), `AuthExpired`, `PermanentSendError` (invalid recipient etc.), `TransientError` (5xx, network). Retry `TransientError` up to 3 times with exponential backoff.

`tests/fakes.py` contains `FakeGmailClient` with the same functions, storing sent messages in memory and letting tests inject replies and bounces.

---

## 6. Settings (`settings.yaml`, loaded by `mailer/config.py`)

```yaml
sender:
  default_display_name: "Beedu from Mobisa"
  sender_title: ""          # filled later
  website: ""               # filled later
  booking_link: ""          # filled later
  portfolio_link: ""        # filled later
  business_address: ""      # required before dry_run can be turned off
  list_unsubscribe_address: "unsubscribe@mobisa.in"

mode:
  dry_run: true             # default ON. Sends nothing, only logs.

worker:
  app_window_ist: { start: "18:00", end: "00:30" }   # worker only sends inside this local (IST) window
  recipient_window: { start: "09:00", end: "17:00", weekdays_only: true }
  delay_between_sends_seconds: { min: 90, max: 240 } # per inbox
  detection_every_minutes: 10
  idle_sleep_seconds: 5
  hard_daily_cap: 150       # absolute max per inbox per rolling 24h, overrides any ramp/max setting

safety:
  bounce_pause_threshold: 3   # bounces ...
  bounce_pause_window: 50     # ... within the inbox's last N sends -> pause inbox
  pause_on_quota_hours: 24

import:
  skip_quality: ["Bad"]
```

`config.py` validates: times parse, min ≤ max, caps positive. On the Campaign page, block turning `dry_run` off if `business_address` is empty.

Editable from the dashboard: everything in `sender` and `mode`, the caps and delays. Save back to the YAML.

---

## 7. Seed data (`mailer/seed.py`, inserted only if tables are empty)

**Steps:** `(1, 0)`, `(2, 3)`, `(3, 7)`. The number of steps is editable in the dashboard (add/remove/change delay).

**Languages:**
- `en`: footer `{{sender_name}}, Mobisa · {{business_address}}\nReply "stop" to opt out.`
- `fr`: footer `{{sender_name}}, Mobisa · {{business_address}}\nRépondez « stop » pour ne plus recevoir nos e-mails.`
- The Templates page has an "Add language" button (code, name, footer).

**Country settings** (language, timezone, sort order; Germany skip = 1):

| Country | Lang | Timezone | Order |
|---|---|---|---|
| United States | en | America/New_York (see US states rule) | 1 |
| United Kingdom | en | Europe/London | 2 |
| Canada | en | America/Toronto | 3 |
| Australia | en | Australia/Sydney | 4 |
| Ireland | en | Europe/Dublin | 5 |
| Netherlands | en | Europe/Amsterdam | 6 |
| Sweden | en | Europe/Stockholm | 7 |
| Denmark | en | Europe/Copenhagen | 8 |
| Norway | en | Europe/Oslo | 9 |
| Finland | en | Europe/Helsinki | 10 |
| Iceland | en | Atlantic/Reykjavik | 11 |
| Switzerland | en | Europe/Zurich | 12 |
| Belgium | fr | Europe/Brussels | 13 |
| Spain | en | Europe/Madrid | 14 |
| Italy | en | Europe/Rome | 15 |
| Portugal | en | Europe/Lisbon | 16 |
| Poland | en | Europe/Warsaw | 17 |
| Austria | en | Europe/Vienna | 18 |
| Czechia | en | Europe/Prague | 19 |
| Slovakia | en | Europe/Bratislava | 20 |
| Hungary | en | Europe/Budapest | 21 |
| Greece | en | Europe/Athens | 22 |
| Cyprus | en | Asia/Nicosia | 23 |
| Romania | en | Europe/Bucharest | 24 |
| Bulgaria | en | Europe/Sofia | 25 |
| Croatia | en | Europe/Zagreb | 26 |
| Serbia | en | Europe/Belgrade | 27 |
| Estonia | en | Europe/Tallinn | 28 |
| Latvia | en | Europe/Riga | 29 |
| Lithuania | en | Europe/Vilnius | 30 |
| Ukraine | en | Europe/Kyiv | 31 |
| Moldova | en | Europe/Chisinau | 32 |
| Monaco | fr | Europe/Monaco | 33 |
| France | fr | Europe/Paris | 40 |
| Germany | en | Europe/Berlin | 99, **skip = 1** |

Unknown countries on import: language `en`, timezone `Europe/London`, order 100, and show them in the import summary so I can fix them.

**US states rule (`scheduling.py`):** `location` looks like `"Texas, United States"`. Parse the state and map:
- Pacific (`America/Los_Angeles`): California, Washington, Oregon, Nevada
- Mountain (`America/Denver`): Colorado, Utah, New Mexico, Wyoming, Montana, Idaho
- Arizona (`America/Phoenix`): Arizona
- Central (`America/Chicago`): Texas, Illinois, Minnesota, Wisconsin, Missouri, Iowa, Kansas, Nebraska, Oklahoma, Arkansas, Louisiana, Mississippi, Alabama, Tennessee, North Dakota, South Dakota
- Alaska (`America/Anchorage`), Hawaii (`Pacific/Honolulu`)
- Everything else / unknown: `America/New_York`

**Category phrases** (match on the lowercased raw category; if not found, try the part before the first `;`; if still not found, use that part lowercased for both languages):

| raw | phrase_en | phrase_fr |
|---|---|---|
| food & beverage | food & drink | alimentaire |
| beauty & personal care | beauty | beauté |
| fashion & accessories | fashion | mode |
| home & lifestyle | home & lifestyle | maison & déco |
| gadgets & practical accessories | gadget | accessoires |
| fitness, sport & outdoor | fitness | sport |
| skincare | skincare | soin de la peau |
| haircare / haircare & grooming / hair care | haircare | soin capillaire |
| bath & body care / bath, body & personal care / skin & body care / body & personal care / soap, bath & body | body care | soin du corps |
| fragrance | fragrance | parfum |
| makeup / cosmetics & beauty products / makeup & beauty tools | makeup | maquillage |
| nails / nail care | nail | ongles |
| oral care | oral care | hygiène bucco-dentaire |

(Each row with slashes = several `raw` keys with the same phrases.) The Templates page has an editable table for this.

**Draft templates** (placeholder content. I will rewrite them later; they must exist so the tool is testable):

Step 1, en, subject: `{quick question|idea} for {{business_name}}`
```
Hi {{business_name}} team,

{{custom_line}}Who handles your Meta and TikTok ads? I make AI UGC video ads for {{category}} brands: fresh hooks and angles to test, at a fraction of creator costs.

Want me to send a 15-second sample made for {{business_name}}?
```
Step 2, en:
```
Quick bump on this. Happy to make the sample, it takes 30 seconds to watch.
```
Step 3, en:
```
Last note from me. Here is some of my work: {{portfolio_link}}
If ads are not your area, could you forward this to whoever runs them?
```
Step 1, fr, subject: `{petite question|une idée} pour {{business_name}}`
```
Bonjour l'équipe {{business_name}},

{{custom_line}}Qui s'occupe de vos publicités Meta et TikTok ? Je crée des vidéos publicitaires UGC par IA pour les marques {{category}} : de nouvelles accroches à tester, pour une fraction du coût des créateurs.

Je vous envoie un exemple de 15 secondes fait pour {{business_name}} ?
```
Step 2, fr:
```
Je me permets de relancer. L'exemple ne prend que 30 secondes à regarder.
```
Step 3, fr:
```
Dernier message de ma part. Quelques réalisations : {{portfolio_link}}
Si la publicité n'est pas votre domaine, pourriez-vous transférer ce message à la bonne personne ?
```

---

## 8. Templating (`mailer/templating.py`)

- **Merge fields:** double braces `{{field}}`. Allowed: `business_name`, `category`, `country`, `sender_name`, `sender_title`, `website`, `booking_link`, `portfolio_link`, `business_address`, `custom_line`.
  - `category` = the phrase from `category_phrases` in the lead's language.
  - `sender_name` = the sending inbox's `display_name`.
  - `custom_line`: if non-empty, render it followed by a blank line; if empty, render nothing (no stray blank line).
  - Unknown field in a template = validation error shown on the Templates page; saving is blocked.
- **Spintax:** single braces `{option a|option b|option c}`. Resolve **deterministically per lead and step** (seed = hash of `email + step_no`), so the same lead always gets the same version (preview = what is sent).
- **Business name cleanup** before merging: strip `®`, `™`, `©`, emojis and surrounding quotes; collapse spaces; if the whole name is uppercase and longer than 4 characters, convert to Title Case.
- **Footer:** append `\n\n` + the language's footer (merged).
- **Follow-up subject:** steps ≥ 2 use `Re: ` + `first_subject` (their subject field is ignored and greyed out in the UI).
- **Link warning:** if the step 1 body contains `http`, `www.` or `.com/`, show a warning on save (still allowed).
- `render(lead, step_no, inbox, settings) -> RenderedEmail(subject, body, language, warnings)`. Fallback: if no template exists for the lead's language, use `en` and add a warning.
- `preview_random_lead(step_no, language)` for the Templates page.

---

## 9. Import (`mailer/importer.py`, page 2)

- Accept `.xlsx` (choose sheet) and `.csv`.
- Column mapping UI with these defaults (from Mobisa's merged sheet): `Professional email`, `Business name`, `Product category`, `Country`, `Location`, `Website`, `Company Instagram`, `Email type`, `Priority`, `Status` (= quality). Only email is required.
- Per row: lowercase + trim email; validate with regex `^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$`.
- Skip, with counted reasons: invalid email; already in `leads`; in `suppression`; quality in `skip_quality`; country with `skip = 1`; duplicate inside the same file.
- Set `language` from `country_settings` (unknown country, then `en`).
- Show summary (imported, skipped per reason, unknown countries) and save a row in `imports`.
- Import in one transaction; 15,000 rows must import in under ~10 seconds.

---

## 10. Scheduling rules (`mailer/scheduling.py`)

- `recipient_tz(lead) -> ZoneInfo` (country table, plus the US state rule).
- `in_recipient_window(lead, now_utc) -> bool`: recipient local time within `recipient_window` and (if `weekdays_only`) Monday to Friday.
- `in_app_window(now_utc) -> bool`: IST (`Asia/Kolkata`) time within `app_window_ist`. Must handle windows crossing midnight (18:00 to 00:30).
- `todays_cap(inbox, today) -> int`: `min(max_cap, hard_daily_cap, ramp_start_cap + ramp_increment * floor(days_since(ramp_start_date) / ramp_every_days))`.
- `sent_last_24h(inbox) -> int`: count `send_log` rows with `result='sent'` for the inbox in the **rolling last 24 hours** (Gmail limits are rolling). Inbox may send only while `sent_last_24h < todays_cap`.
- Queue order for **new** leads (`status='queued'`): country `sort_order`, then `priority` ascending, then quality (Good before Weak), then `id`.
- **Follow-ups first:** leads with `status='in_sequence'` and `next_due_at <= now` and next step active are always sent before new leads from the same inbox.

---

## 11. The worker (`worker.py` + `mailer/engine.py`)

`python worker.py --run` runs a **long-lived loop** until the app window ends, then exits. `python worker.py --once` does a single tick (used in tests and for debugging).

**Single-instance lock:** on start, write `worker_pid` and `worker_heartbeat` into `app_state`. If another live worker is found (heartbeat < 60 s old and PID alive), exit immediately. Update the heartbeat every loop.

**Loop:**
```
while in_app_window(now):
    if app_state.paused_all: sleep(idle); continue
    if minutes_since(last_detection_run) >= detection_every_minutes:
        detection.run_all_inboxes(); safety.evaluate_all_inboxes()
    sent_something = engine.tick()
    sleep(idle_sleep_seconds if not sent_something else 1)
exit
```

**`engine.tick()`** sends **at most one email** per call:
1. Candidate inboxes: `role='send'`, `active=1`, not paused (or `paused_until` passed, then auto-resume and clear the reason), token valid, `next_send_after <= now`, `sent_last_24h < todays_cap`.
2. Go round-robin across candidate inboxes (remember the last used inbox in `app_state`).
3. For the chosen inbox, pick the first lead that is **in its recipient window**:
   - a) due follow-ups assigned to this inbox (step + 1 ≤ number of active steps), else
   - b) the next new lead in queue order (assign `inbox_id` to this inbox at send time).
   - Skip any lead whose email is in `suppression` (and mark it `skipped`).
4. Render (section 8). Check `send_log` again so the same lead + step was never sent (`uq_send_once` also enforces it).
5. If `dry_run`: write `send_log` with `result='dry_run'`, **do not** change lead step/status, set `next_send_after = now + 5 s`, return.
6. Else send via `gmail_client.send_message`: for step ≥ 2 pass `thread_id`, `in_reply_to=last_message_id` and subject `Re: first_subject`.
7. On success, in one transaction:
   - write `send_log` (`sent`);
   - lead: `step += 1`, `status='in_sequence'`, `thread_id`, `last_message_id`, `last_sent_at`, `first_subject` (when step 1);
   - `next_due_at = now + delay_days of the next step` (or, if this was the last step, `now + 4 days` after which a cleanup marks it `finished`);
   - inbox `next_send_after = now + random(min, max)` seconds.
8. On error: write `send_log` (`error`, message). `RateLimited`: pause the inbox 1 hour. `QuotaExceeded`: pause for `pause_on_quota_hours`. `AuthExpired`: pause with reason "Reconnect inbox". `PermanentSendError`: mark the lead `bounced` + add to suppression. Never crash the loop.
9. Cleanup (once per detection run): leads `in_sequence` where the last step was sent and `next_due_at <= now` become `finished`.

---

## 12. Detection (`mailer/detection.py`)

For every connected inbox (both `send` and `monitor` roles), query Gmail with `in:inbox newer_than:3d -from:me` (plus, for the inbox that receives `list_unsubscribe_address`, the same query includes messages to that alias). Skip messages whose `gmail_message_id` already exists in `events`.

Classify each message, in this order:
1. **Bounce:** From contains `mailer-daemon@` or `postmaster@`, or subject matches `Delivery Status Notification|Undeliverable|Mail delivery failed|Address not found|Returned mail`. Failed recipient = header `X-Failed-Recipients`, else the first email address in the message text that matches a lead. Then: lead `bounced`, add `suppression(bounced)`, label `Mobisa/Bounced`, event `bounce`.
2. **Auto-reply (ignore):** header `Auto-Submitted` present and not `no`, or `X-Autoreply`/`X-Autorespond` present, or `Precedence` in (`auto_reply`, `bulk`), or subject matches `Automatic reply|Auto-reply|Out of office|Réponse automatique|Absence`. Event `auto_reply_ignored`; the sequence continues.
3. **Match to a lead:** by `threadId` = `leads.thread_id`; else by sender address = `leads.email`; else (message to the unsubscribe alias) by sender address. No match: ignore.
4. **Opt-out:** message to the unsubscribe alias, or subject + snippet (case-insensitive) contains `stop`, `unsubscribe`, `remove me`, `remove`, `opt out`, `désabonner`, `désinscrire`, `ne plus recevoir`, `retirez`. Then lead `unsubscribed`, `suppression(unsubscribed)`, label `Mobisa/Unsubscribed`, event `unsubscribe`.
5. **Reply:** anything else from the lead. Lead `replied` (sequence stops), label `Mobisa/Replied`, event `reply` with subject and snippet.

Update `app_state.last_detection_run`.

---

## 13. Dashboard (Streamlit, `app.py` + `pages\`)

Title "Mobisa Mailer". On every page, show a top banner: **DRY RUN** (yellow) or **LIVE** (green), plus a red banner for any paused inbox or a stopped worker (heartbeat > 2 min old during the app window). Use `st.cache_resource` for the DB connection and keep pages fast with 15,000+ leads (paginate tables, 100 rows per page).

1. **Overview:** per inbox: sent last 24h / today's cap, status (active / paused + reason), next send time. Totals: queued, in sequence, replied, unsubscribed, bounced, finished. Today's replies count. Buttons: **Start worker now** (launches `worker.py --run` as a detached background process via `subprocess.Popen` with `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`), **Pause all / Resume all**, **Run detection now**.
2. **Import:** upload, sheet picker, column mapping, summary (section 9), history of past imports.
3. **Inboxes:** add inbox (email, display name, title, role), **Connect** (OAuth), reconnect, edit ramp settings and max cap, pause/resume, show today's cap and the ramp schedule for the next 30 days as a small table. Show which inbox receives the unsubscribe alias.
4. **Templates:** tabs per step and language; subject (step 1 only) and body editors; merge-field list; validation errors; link warning; **Preview** with a random real lead (rendered subject + body + footer); **Send test to myself** (sends the rendered email to an address I type, even in dry-run, logged in `send_log` with `result='test'` and no lead, not counted against caps); steps editor (add/remove step, delay days); languages editor; category phrase table editor.
5. **Campaign:** dry-run toggle (blocked if `business_address` is empty, with a confirmation dialog when switching to LIVE); sender settings (website, booking link, portfolio, address, unsubscribe address); worker windows, delays, caps; country table editor (language, timezone, skip, sort order).
6. **Leads:** filters (status, country, quality, inbox, step, search by email or name), paginated table, row actions: skip, add to suppression, reset to queued (only if never sent), edit `custom_line`.
7. **Replies:** list of `reply` and `unsubscribe` events, newest first: lead name, email, country, snippet, time, link `https://mail.google.com/mail/u/<inbox>/#all/<thread_id>`. Mark as handled (checkbox, stored in `events.handled`).
8. **Log:** `send_log` table with filters (inbox, result, date range); export buttons: send log to Excel, leads (with status) to Excel, saved in `exports\` with a timestamp.

---

## 14. Windows automation (`scripts\`)

- `start_dashboard.bat`: `cd /d D:\MobisaMailer`, activate `.venv`, `streamlit run app.py`.
- `start_worker.bat`: `cd /d D:\MobisaMailer`, activate `.venv`, `python worker.py --run`.
- `register_task.ps1`: registers the task **"Mobisa Mailer Worker"** with `Register-ScheduledTask`:
  - Trigger: daily at **18:00**.
  - Action: `D:\MobisaMailer\.venv\Scripts\pythonw.exe D:\MobisaMailer\worker.py --run`, working directory `D:\MobisaMailer`.
  - Settings: `-StartWhenAvailable`, `-ExecutionTimeLimit (New-TimeSpan -Hours 7)`, `-MultipleInstances IgnoreNew`, `-DontStopIfGoingOnBatteries`, `-AllowStartIfOnBatteries`.
  - Run only when the user is logged on (no stored password).
- README: tell me to set Windows power settings so the PC does not sleep while plugged in.

---

## 15. Logging

- `logging` with a daily file `logs\mailer_YYYY-MM-DD.log` (keep 30 days) plus console.
- INFO: worker start/stop, each send (inbox, lead email, step, result), each detected event, each pause/resume.
- DEBUG: rendered subjects, API request ids. Never tokens; never full bodies above DEBUG.

---

## 16. Tests (`tests\`, pytest, no network)

- **templating:** merge fields, unknown field error, empty `custom_line` leaves no blank line, deterministic spintax, name cleanup cases (`"Yes She Will!" Organics®`, `LUXE SKIN`), French template + footer, `Re:` subject on follow-ups, language fallback.
- **scheduling:** ramp cap math (day 0, 3, 6 ... reaching max), hard cap, app window across midnight, recipient window (weekend excluded; New York vs Los Angeles vs Paris for the same UTC time), US state parsing, queue order.
- **detection:** bounce with `X-Failed-Recipients`; bounce parsed from the text; out-of-office ignored; French "stop" opt-out; normal reply by threadId; reply by sender address in a new thread; already-processed message skipped.
- **importer:** column mapping, invalid email, duplicates inside the file, suppression skip, Germany skip, unknown country reported.
- **engine (FakeGmailClient):** dry-run changes nothing on leads; live send sets thread id and next_due_at; follow-up uses the same thread and `In-Reply-To`; follow-ups take priority over new leads; a replied lead gets no more emails; suppressed lead never sent; cap reached means the inbox is skipped; `QuotaExceeded` pauses the inbox; the same lead + step is never sent twice.

---

## 17. Milestones (stop after each one and wait for my OK)

1. **Skeleton + DB:** folder structure, venv, requirements, `config.py`, `db.py` schema, `seed.py`, empty Streamlit app with the 8 pages and banners. *Done when:* `streamlit run app.py` opens and the DB file is created with seed data.
2. **Gmail auth + test send:** README Google setup steps, `gmail_client.py`, Inboxes page connect flow, "Send test to myself". *Done when:* I connect one inbox and receive a test email.
3. **Import + templates:** importer + Import page, templating + Templates page with preview. *Done when:* I import my merged Excel file (about 13,500 rows, sheet "Good" or "All leads") and preview English and French emails for random leads.
4. **Engine (dry run):** scheduling, engine, worker loop, Overview + Campaign pages. *Done when:* a dry-run session logs sends with correct inbox rotation, caps, random gaps and send windows, and nothing is actually sent.
5. **Live + threading:** live sending, follow-ups in thread. *Done when:* using my own test addresses as leads, step 1 and forced follow-ups (I can set delays to 0 for the test) arrive in one thread.
6. **Detection + safety:** detection, labels, suppression, auto-pause, Replies page. *Done when:* a normal reply stops the sequence, "stop" suppresses, a fake address bounces and is suppressed, and an out-of-office is ignored.
7. **Leads + Log pages, exports, Windows task:** remaining pages, exporter, scripts, README complete. *Done when:* the scheduled task starts the worker at 18:00 and exports open in Excel.
8. **Tests green:** all tests in section 16 pass (`pytest -q`).

---

## 18. Out of scope (do not build)

Open/click tracking, HTML emails, attachments, warm-up network, AI-written emails, CRM integrations, cloud deployment, multiple users or logins.
