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

---

## 4. Architecture & Notes

- **Data Directory**: SQLite database is stored at `data\mailer.db` with WAL mode enabled for concurrent dashboard and worker access.
- **Tokens**: Per-inbox OAuth tokens are securely kept in `secrets\tokens\<email>.json`.
- **Logs**: Daily rotating logs are placed in `logs\mailer_YYYY-MM-DD.log`.
- **Configuration**: Settings are stored in `settings.yaml` and can be edited in the dashboard.
- **Lead files**: Keep them in `data\`. `*.xlsx`, `*.xls`, `*.csv` and `exports\` are gitignored and must never be committed.

---

## 5. Decisions Where the Spec Was Changed or Unclear

- **Sequence timing**: step delays are 0, 3, 4 days after the previous step, so emails go out on day 0, day 3 and day 7.
- **Dry run**: `dry_run` rows count toward caps, use the random delay between sends, and skip that lead and step for 24 hours. Leads themselves stay unchanged.
- **Opt-out detection**: uses the message body only, never the subject or snippet. Quoted text is stripped first: lines starting with `>`, and everything after `On ... wrote:`, `Le ... a écrit :` or `-----Original Message-----`. Whole-word match on `stop`, `unsubscribe`, `remove me`, `opt out`, `désabonner`, `désinscrire`, `ne plus recevoir`. Bare `remove` is not an opt-out. If unsure, it is a normal reply.
- **Send windows**: the rules stay as specified, so Australia never overlaps the app window and Hawaii barely does. The Overview page warns about this.
- **Dry-run safety**: only an explicit `dry_run: false` goes live. A blank or unreadable value stays in dry run.
- **Permanent send errors**: only a rejected recipient address suppresses a lead. Other 400/404 errors are logged and the lead stays as it is.
- **Retries**: a transient error is retried 3 times after the first attempt, as the spec says. If Gmail accepted a send but the response was lost, that retry can duplicate the email.

