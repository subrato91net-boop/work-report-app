═══════════════════════════════════════════════════════════════
  Work Report System V4 — Google Sheets Backend
═══════════════════════════════════════════════════════════════

Same UI as V3 (PostgreSQL), but all work reports are stored in
and read from a Google Sheet instead of a PostgreSQL database.
Attendance is still fetched live from BioTime API.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  STEP 1 — Create a Google Service Account
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Go to https://console.cloud.google.com
2. Create a new project (or use an existing one)
3. Enable the "Google Sheets API" and "Google Drive API"
4. Go to IAM & Admin → Service Accounts → Create Service Account
5. Give it a name (e.g. "work-report-bot")
6. Click "Create and Continue" → Skip optional steps → Done
7. Click the new service account → Keys tab → Add Key → JSON
8. A credentials.json file will download

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  STEP 2 — Create and Share your Google Sheet
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Go to https://sheets.google.com and create a new spreadsheet
2. Name the first sheet tab exactly: reports
3. Copy the Spreadsheet ID from the URL:
     https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
4. Share the spreadsheet with the service account email
   (found in credentials.json under "client_email")
   — give it "Editor" access

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  STEP 3A — Run Locally
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Place credentials.json in the same folder as app.py
2. Install dependencies:
     pip install -r requirements.txt
3. Set environment variable:
     export SPREADSHEET_ID="your-sheet-id-here"
4. Run:
     python app.py
5. Open: http://127.0.0.1:5000

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  STEP 3B — Deploy on Render
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Push this folder to a GitHub repository
2. Create a new Web Service on Render → connect the repo
3. Build command:  pip install -r requirements.txt
   Start command:  gunicorn app:app
4. Add these Environment Variables in Render dashboard:

   SPREADSHEET_ID          → your-sheet-id-here
   GOOGLE_CREDENTIALS_JSON → (paste the entire contents of
                              credentials.json as one line)

   NOTE: To paste JSON as env var, open credentials.json in a
   text editor, select all, copy, and paste directly into the
   Render env var value field. Render handles multiline values.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LOGIN CREDENTIALS (unchanged from V3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Manager:   manager    / manager123
  Asif:      asif       / 1002123456
  Kartick:   kartick    / 1003123456
  Sukumar:   sukumar    / 1004123456
  Ashim:     ashim      / 1005123456
  Sujata:    sujata     / 1012123456
  Gourab:    gourab     / 2001123456
  Subrato:   subrato    / 1013123456
  Pritam:    pritam     / 2002123456

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  GOOGLE SHEET STRUCTURE (auto-created on first run)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Sheet tab name: reports
  Columns (row 1 headers, created automatically):
    id | timestamp | emp_code | emp_name | company |
    date | work_type | client_name | location |
    summary | remarks | status

  Each employee form submission adds one new row.
  You can view, filter, and edit the sheet directly in Google
  Sheets at any time — the app reads live from the sheet.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  WHAT CHANGED FROM V3 (PostgreSQL → Google Sheets)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✅ Replaced psycopg2 with gspread + google-auth
  ✅ Removed DATABASE_URL / PostgreSQL setup
  ✅ Added SPREADSHEET_ID + GOOGLE_CREDENTIALS_JSON env vars
  ✅ All DB queries replaced with Sheet read/write helpers
  ✅ Auto-creates sheet headers on first run
  ✅ All HTML templates are IDENTICAL to V3
  ✅ BioTime attendance integration unchanged
  ✅ CSV export unchanged
