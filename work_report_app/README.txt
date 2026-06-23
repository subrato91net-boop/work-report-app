NEW IN THIS UPDATE (v4.0 — two-company fix + employee auto-sync)
────────────────────────────────────────────────────────────────
BUG FIX — conneqtortech attendance now loads correctly
  Root cause in v41: company key was labelled "CONNEQTORTECHNOLOGY" (all caps)
  in BIOTIME_COMPANIES but the fetch logic filtered by "needed_companies" from
  EMPLOYEES which used "conneqtortech" (lowercase). The mismatch meant the
  conneqtortech BioTime tenant was silently skipped on every fetch.

  Fix: renamed the key and internal label to "conneqtortech" everywhere so
  BIOTIME_COMPANIES["conneqtortech"]["company"] == EMPLOYEES[...]["company"].
  A one-time DB migration runs on startup to patch any rows with the old label.

NEW FEATURE — Employee auto-sync from BioTime
  Any emp_code that shows up in BioTime attendance but has no account yet in
  your users table gets created automatically:
  • Real name is pulled from BioTime's /personnel/api/employees/ endpoint
  • A username + temp password are generated
  • The new account is logged to /biotime-sync-log (manager only)
  • The manager can then share the temp password with the employee and update
    it from Manage Users

NEW FEATURE — Deduplication of transactions
  If both company tenants ever return the same underlying BioTime record (same
  "id"), it is counted only once so attendance hours are never double-counted.

NEW FEATURE — BioTime sync log route
  /biotime-sync-log  → shows auto-created users with their temp passwords
  (manager-only; linked from the /debug-biotime page)

ENVIRONMENT VARIABLES — add these for conneqtortech:
  BIOTIME_URL_CONNEQTOR    = https://conneqtortech.itimedev.minervaiot.com
  BIOTIME_EMAIL_CONNEQTOR  = presales@conneqtortech.com
  BIOTIME_PASS_CONNEQTOR   = Y@jh_ro@562
  BIOTIME_COMPANY_CONNEQTOR= conneqtortech

NEW IN THIS UPDATE (v3.9)
- Attendance page — fully redesigned with dual-tab layout:
    "Summary" tab (default): same processed daily/weekly/monthly views as before,
    now enhanced with employee code shown under name, cleaner hours bar, and
    a "View" button per row that opens a popup showing ALL individual punch
    timestamps for that employee on that day (time, check-in/out state, GPS,
    terminal — no more guessing from just first/last punch).
    "Raw Punches" tab: brand-new. Shows every single raw transaction record
    pulled from BioTime for the selected date range — emp code, full name,
    exact punch time, punch state badge (Check In / Check Out / Break / OT),
    source (App vs Device badge), terminal name, GPS address, coordinates,
    and upload time. Live search bar filters by employee code or name instantly.
    Capped at 2000 records per page load for performance.
- Stats row: added "Records Loaded" count card so you can immediately see
    how many raw BioTime records were fetched for the date range.
- Filter bar: "Apply" button is now orange (more visible), labels use
    consistent uppercase styling.
- All existing functionality (Daily / Weekly / Monthly views, filters,
    CSV export, BioTime debug, sidebar navigation) unchanged.

NEW IN THIS UPDATE (v3.8)
- Manager Dashboard (Command Center): new "🏠 Dashboard" tab — now the
    manager's default landing page after login. One glance shows today's
    report completion, active jobs, pending TA approvals, clients needing
    follow-up, sales visits logged today, and recent activity feed.

NEW IN THIS UPDATE (v3.4)
- Installable mobile app (PWA): add to home screen on Android/iPhone.

NEW IN THIS UPDATE (v3.3)
- Mini CRM — Clients layer with full visit timelines and freshness tracking.

NEW IN THIS UPDATE (v3.2)
- Job assignment: multi-employee + multi-supervisor per job.
- Brand new visual design (navy + amber, card-based, responsive).

═══════════════════════════════════════════════
  WORK REPORT SYSTEM V3.9 — PostgreSQL Version
  Imax Solution & Conneqtor Technology
═══════════════════════════════════════════════

WHAT'S IN THIS VERSION
- Flask app with PostgreSQL (Render-compatible)
- BioTime Cloud 2.0 JWT attendance integration
- Work reports with custom form builder
- Job assignment (multi-employee, multi-supervisor)
- Sales visits + Mini CRM (clients)
- TA reports with approval workflow
- Support reports
- Challan / delivery note PDFs
- Stock upload (CSV/Excel)
- Employee profiles + departments
- Push notifications (PWA)
- Manager accounts management
- Data delete manager

═══════════════════════════════════════════════
  DEPLOY TO RENDER — STEP BY STEP
═══════════════════════════════════════════════

STEP 1 — Create PostgreSQL database on Render
  → Render dashboard → New + → PostgreSQL
  → Name: work-report-db
  → Region: Oregon (US West)
  → Plan: Free
  → Click Create Database → copy "Internal Database URL"

STEP 2 — Set Environment Variables in your Web Service
  → Go to your web service on Render
  → Click Environment (left sidebar)
  → Add:
      DATABASE_URL  = (Internal Database URL from step 1)

  Optional — override BioTime credentials:
      BIOTIME_URL_IMAXSOL    = https://imaxsol.itimedev.minervaiot.com
      BIOTIME_EMAIL_IMAXSOL  = presales@conneqtortech.com
      BIOTIME_PASS_IMAXSOL   = Y@jh_ro@562
      BIOTIME_COMPANY_IMAXSOL= imaxsol

STEP 3 — Upload files to GitLab / GitHub
  → Replace all old files with these new ones
  → Commit and push

STEP 4 — Redeploy on Render (auto or Manual Deploy)

═══════════════════════════════════════════════
  LOGIN CREDENTIALS
═══════════════════════════════════════════════

MANAGER
  Username : manager
  Password : manager123

EMPLOYEES
  Username : asif      Password : 1002123456
  Username : kartick   Password : 1003123456
  Username : sukumar   Password : 1004123456
  Username : ashim     Password : 1005123456
  Username : sujata    Password : 1012123456
  Username : gourab    Password : 2001123456
  Username : subrato   Password : 1013123456
  Username : pritam    Password : 2002123456

═══════════════════════════════════════════════
  KEY ROUTES
═══════════════════════════════════════════════

  /              → Redirect based on role
  /login         Employee / Manager login
  /dashboard     Manager command centre
  /attendance    BioTime attendance (Summary + Raw Punches tabs)
  /manager       Work reports table
  /assign-job    Job assignment + all jobs
  /form          Employee: submit daily work report
  /my-jobs       Employee: view assigned jobs
  /manager/clients           Mini CRM
  /manager/clients/<id>      Client profile
  /challan                   Delivery challans
  /manager/form-builder      Custom form fields
  /manager/users             User management
  /manager/admins            Admin account management
  /manager/delete-manager    Data delete tool
  /debug-biotime             BioTime live diagnostic

