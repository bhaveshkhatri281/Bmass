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

