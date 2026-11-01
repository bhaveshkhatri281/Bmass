# Mobisa Mailer

Private, single-user desktop cold-emailing tool for **Mobisa** (AI creative agency) to send personalized plain-text cold emails from Google Workspace inboxes.

---

## 1. Quick Start & Daily Use

### Starting the Dashboard
Double-click `scripts\start_dashboard.bat` or run:
```cmd
.venv\Scripts\activate
streamlit run app.py
```
This opens the dashboard in your default browser (usually at `http://localhost:8501`).

### Running the Worker Manually
Double-click `scripts\start_worker.bat` or run:
```cmd
.venv\Scripts\activate
python worker.py --run
```
To run a single test tick:
```cmd
python worker.py --once
```

### Daily Routine
1. The scheduled task starts the worker at 18:00 IST. It sends between 18:00 and 00:30 IST, during each lead's own working hours.
2. Open **Overview** to check inboxes, caps and any red banner.
3. Open **Replies** to read replies and tick them as handled. Click **Open in Gmail** to answer.
4. Open **Log** to see what was sent and to export to Excel.

### Safety Switches
- **Dry run** (Campaign page): on by default. The worker only logs what it would send.
- **Test leads lock** (Campaign page): on by default. In LIVE mode, only test leads can be emailed, whatever is in the queue. Unlocking real leads asks you to type a confirmation.
- **Pause All Sending** (Overview page): stops sending at once. Replies and opt-outs are still detected.

### Test Leads
Add your own addresses under **Import > Add test leads**, or tick **These are test leads** when uploading a file. Addresses already in your real list are skipped, so a real lead can never become a test lead. **Leads > Remove all test leads** deletes them with their log rows and suppression entries, so you can test again. Real leads are never touched.

---

## 2. One-Time Google Cloud OAuth Setup

To enable Gmail sending and reply detection, set up Google Cloud OAuth:

1. **Google Cloud Console**: Go to [console.cloud.google.com](https://console.cloud.google.com), create a new project named **`Mobisa Mailer`**, and enable the **Gmail API**.
2. **OAuth consent screen**:
   - Set User Type to **Internal** (accessible by your Google Workspace accounts).
   - Set App Name to **`Mobisa Mailer`**.
   - Add support email and developer contact email.
3. **Scopes**: Add the following scopes:
   - `https://www.googleapis.com/auth/gmail.send`
   - `https://www.googleapis.com/auth/gmail.modify`
4. **Create Credentials**:
   - Go to **APIs & Services > Credentials**.
   - Click **Create Credentials > OAuth client ID**.
   - Application type: **Desktop app**.
   - Name: **`Mobisa Mailer Client`**.
   - Click **Create**, then download the client JSON file.
5. **Save Credentials**:
   - Place the downloaded JSON file into `secrets\credentials.json`.
   - Never commit or share files inside `secrets\`.

### Connecting an inbox

1. Start the dashboard on the PC that will run the worker. The Google sign-in opens in that PC's browser.
2. Open **Inboxes**, fill in **Add New Inbox**, and click **Add Inbox**.
3. Click **Connect Account** and sign in as that exact address. Picking a different account is rejected. The sign-in times out after 5 minutes.
4. Use **Send Test Email to Myself** to confirm delivery. It always sends for real, even in dry run. It is logged as `result='test'` and does not count against caps.

### Unsubscribe alias

`sender.list_unsubscribe_address` is the alias printed in the `List-Unsubscribe` header. `sender.unsubscribe_inbox` in `settings.yaml` is the connected inbox that actually receives it. Google does not tell the app which inbox holds an alias, so set this by hand.

---

## 3. Windows Power & Task Scheduling Settings

- **Prevent Sleep When Plugged In**: Because sending happens during the IST evening window (18:00 – 00:30), Windows must not go to sleep while the worker is running.
  - Open **Windows Settings > System > Power & sleep**.
  - Under **When plugged in, PC goes to sleep after**, set to **Never**.
- **Automated Scheduling**:
  - Run `scripts\register_task.ps1` in PowerShell to automatically register the Windows Scheduled Task to run daily at 18:00 IST.
  - The task runs only while you are logged in, and no password is stored. If the PC was off at 18:00, it starts as soon as possible afterwards.
  - To check it, open **Task Scheduler** and look for **Mobisa Mailer Worker**. Right-click it and choose **Run** to start it by hand.
  - If PowerShell blocks the script, run this once instead: `powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1`

---

## 4. Architecture & Notes

- **Data Directory**: SQLite database is stored at `data\mailer.db` with WAL mode enabled for concurrent dashboard and worker access.
- **Tokens**: Per-inbox OAuth tokens are securely kept in `secrets\tokens\<email>.json`.
- **Logs**: Daily logs are placed in `logs\mailer_YYYY-MM-DD.log` and kept for 30 days. The dashboard and the worker share the same file.
- **Configuration**: Settings are stored in `settings.yaml` and can be edited in the dashboard.
- **Import**: The Good and Weak sheets are read as that quality when the file has no Status column. Empty or non-numeric priority becomes 5. The Import page preselects the Good sheet.
- **Name cleanup**: Double quotes are removed anywhere in a business name, not only around it. Single quotes are removed only at the ends. Text in `(...)` or `[...]` is removed. Legal suffixes at the end of a name are removed before merging: SAS, SASU, SARL, EURL, SA, SCI, SNC, Ltd, Limited, LLC, Inc, GmbH, BV, AB, Srl, SL, with or without dots or brackets. At the start of a name, only the French forms SARL, SAS, SASU and EURL are removed, because `AB` and `SA` are also ordinary brand words there. Names in **Templates > Name exceptions** (`names.keep_suffix`) keep their bracketed text and legal form.
- **Footer**: One footer per language, `{{sender_name}} · {{us_address}}`. `{{us_address}}` prints `business_address` for United States leads and `website` without `https://` for everyone else.
- **Category phrases**: The seed covers every raw category in the merged lead file. A lead without a matching phrase uses its raw category text, and the preview shows a warning. Other languages use `phrase_en`.
- **New steps**: A new step starts inactive and cannot be activated until its `en` template exists.
- **Lead files**: Keep them in `data\`. `*.xlsx`, `*.xls`, `*.csv` and `exports\` are gitignored and must never be committed.

---

## 5. Decisions Where the Spec Was Changed or Unclear

- **Sequence timing**: step delays are 0, 3, 4 days after the previous step, so emails go out on day 0, day 3 and day 7.
- **Dry run**: `dry_run` rows count toward caps, use the random delay between sends, and skip that lead and step for 24 hours. Leads themselves stay unchanged. In LIVE mode, old `dry_run` rows do not count toward caps.
- **Worker**: Settings are read again on every loop, so dashboard changes apply within seconds. If `settings.yaml` cannot be read, the last good settings stay in use. `--once` takes the same lock as `--run`.
- **Country skip**: A country marked `skip` stops new sends and follow-ups for leads already imported. Language changes apply to future imports only.
- **Going LIVE**: Blocked while the business address, portfolio link, website or unsubscribe address is empty. The worker checks this too, so editing `settings.yaml` by hand cannot skip it.
- **Test leads lock**: Added on top of the spec as `mode.test_leads_only`, on by default, with a new `leads.is_test` column. Existing databases gain the column on the next start.
- **US reservation**: Added on top of the spec as `worker.us_reservation`. From the start of the app window until `until_ist`, new non-US leads may use only the part of each inbox's cap that is not reserved. The reserved part is rounded up, so 50% of 15 holds back 8. After `until_ist`, any country can use it. With no US lead queued, nothing is held back. Follow-ups are never held back, but they count toward the cap.
- **Detection during a pause**: Detection keeps running during a global pause, unlike the spec's loop, so opt-outs are never missed.
- **Failed sends**: A transient error waits for the normal gap and retries. Any other Gmail error pauses the inbox for 1 hour, because more sends would fail too and would burn through leads.
- **Bounce pause**: Resuming an inbox forgives earlier bounces, otherwise it would pause again at once.
- **Bounces from unknown addresses**: Logged, with nothing suppressed.
- **US states**: The state is the last part of the location before the country. Washington DC and places like Washingtonville, New York use Eastern time.
- **Opt-out detection**: uses the message body only, never the subject or snippet. Quoted text is stripped first: lines starting with `>`, and everything after `On ... wrote:`, `Le ... a écrit :`, `-----Original Message-----`, or an Outlook `From:` / `De :` header block. Lines repeating our own footer are ignored too. It is an opt-out if the remaining text has a clear phrase as whole words: `unsubscribe`, `remove me`, `opt out`, `stop emailing`, `stop sending`, `take me off`, `désabonner`, `désinscrire`, `ne plus recevoir`. It is also an opt-out if the remaining text is 8 words or fewer and contains the word `stop`, except in `stop by`, `stop in`, `stop at`, `stop over` or `stop past`. It is also an opt-out if the first non-empty line is only `stop`, `unsubscribe`, `remove`, `stopp` or `arrêter`, ignoring case and punctuation, even with a long signature below. Anything else is a normal reply, which still stops the sequence but does not suppress the address.
- **Send windows**: the rules stay as specified, so Australia never overlaps the app window and Hawaii barely does. The Overview page warns about this.
- **Dry-run safety**: only an explicit `dry_run: false` goes live. A blank or unreadable value stays in dry run.
- **Permanent send errors**: only a rejected recipient address suppresses a lead. Other 400/404 errors are logged and the lead stays as it is.
- **Retries**: a transient error is retried 3 times after the first attempt, as the spec says. Before each resend, `send_message` searches `in:sent to:<recipient> subject:"<subject>" newer_than:1h`. A hit that also matches the exact subject, recipient, `In-Reply-To`, thread and send time counts as sent, and its ids are returned. If that search fails too, the error is raised and nothing is resent.

