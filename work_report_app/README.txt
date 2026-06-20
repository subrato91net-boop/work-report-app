NEW IN THIS UPDATE (v3.3)
- Mini CRM — Clients layer:
    Every sales visit's client is now linked to a "company" record.
    New manager tab → "Clients": see every client, total visit count,
    last visit date, and a freshness badge (Active / 14-30 days /
    30+ days no contact). Filter by "no visit in 30/60/90+ days" to
    spot accounts going cold.
    Click into any client to see its full profile (industry, contact
    person, phone, address, notes — all editable by the manager) plus
    a complete visit timeline pulled from every salesperson who's
    visited them.
    Existing sales visit data is migrated automatically on first
    startup — every distinct client name already in your database
    gets its own company record, no data lost, no manual SQL needed.

NEW IN THIS UPDATE (v3.2)
- Job assignment is now fully flexible:
    Manager can assign a job to multiple employees AND/OR multiple
    supervisors in one go. Either side can be left as "N/A" — a job
    can go to employees only, supervisors only, or both.
    Anyone listed as an employee OR a supervisor on a job can see it
    under "My Jobs" (read-only). The page tags whether you're viewing
    as Employee, Supervisor, or Both on each job card.
- Brand new visual design across every page:
    Deep navy + signal-amber color system, card-based layout,
    "job ticket" style status badges, fully responsive —
    bottom tab bar on mobile, top tab bar + wider tables on desktop.
- WhatsApp notifications and AI report suggestions were requested but
  not included in this build (they need your own WhatsApp Business API
  account and AI API key respectively — ask whenever you're ready to
  set those up and they can be added next).

PREVIOUSLY ADDED (v3.1)
- Manager → "Assign Jobs" tab, Employee → "My Jobs" tab
- Employee report form: big "Job details" field, small "Remarks" field,
  required Supervisor dropdown shown to the manager in Work Reports

═══════════════════════════════════════════════
  WORK REPORT SYSTEM V3 — PostgreSQL Version
  Imax Solution & Conneqtor Technology
═══════════════════════════════════════════════

WHAT'S DIFFERENT FROM V2
- Uses PostgreSQL instead of SQLite
- Data is permanently stored — never lost on restart
- Works perfectly on Render free plan
- Real BioTime attendance data
- No Google Sheets / Google credentials anywhere — pure PostgreSQL

NEW IN THIS UPDATE
- Manager → "Assign Jobs" tab:
    Assign a job to any employee, with supervisor, job title,
    description, location, company, start/end date, and status.
    All assigned jobs are listed and filterable by employee/status.
- Employee → "My Jobs" tab:
    Employees can VIEW (read-only) every job assigned to them —
    job title, description, supervisor, company, location, dates, status.
    Employees cannot edit assigned jobs; only the manager assigns/manages them.
- Employee → Work Report form:
    "Job details" is now a large textarea (the main field).
    "Remarks" is now a small, optional field.
    Employees now pick their Supervisor from a dropdown when
    submitting a daily report; the supervisor's name is saved with
    the report and shown to the manager in the Work Reports table.

═══════════════════════════════════════════════
  DEPLOY TO RENDER — STEP BY STEP
═══════════════════════════════════════════════

STEP 1 — Create PostgreSQL database on Render
  → Render dashboard → New + → PostgreSQL
  → Name: work-report-db
  → Region: Oregon (US West)  ← same as your app
  → Plan: Free
  → Click Create Database
  → Wait 1-2 minutes
  → Click on the database → copy "Internal Database URL"

STEP 2 — Set Environment Variable in your Web Service
  → Go to your work-report-app service on Render
  → Click Environment (left sidebar)
  → Click Add Environment Variable
  → Key:   DATABASE_URL
  → Value: (paste the Internal Database URL you copied)
  → Click Save Changes

STEP 3 — Upload these V3 files to GitLab
  → Replace all old files with these new ones
  → Commit and push

STEP 4 — Redeploy on Render
  → Render will auto-detect the GitLab change
  → OR click Manual Deploy
  → Wait 2-3 minutes
  → Your app is live with permanent database!

NOTE: If you're upgrading an existing Render deployment (database
already has data), this app automatically adds the new supervisor
columns and the jobs table (with multi-assignee columns) on startup
— no manual SQL needed. Old single-assignee job rows, if any existed
from a previous version, are migrated automatically too.

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
  ROUTES
═══════════════════════════════════════════════

  /form          Employee — submit daily work report (with supervisor)
  /my-jobs       Employee — view jobs assigned to them (read-only)
  /manager       Manager  — view/filter all work reports
  /assign-job    Manager  — assign new jobs + view/filter all assigned jobs
  /attendance    Manager  — BioTime attendance dashboard
  /manager/clients               Manager — mini CRM: all clients, freshness filters
  /manager/clients/<id>          Manager — client profile + full visit timeline

═══════════════════════════════════════════════
  FILES
═══════════════════════════════════════════════

  app.py              Main app + PostgreSQL + BioTime API
  requirements.txt    flask, gunicorn, requests, psycopg2-binary
  Procfile            For Render: web: gunicorn app:app
  templates/
    login.html
    form.html          Employee report form (job details + remarks + supervisor)
    my_jobs.html        Employee — view assigned jobs (read-only)
    manager.html        Manager — work reports table
    assign_job.html      Manager — assign jobs + all-jobs table
    attendance.html      Manager — attendance dashboard
    manager_clients.html       Manager — mini CRM client list
    manager_client_detail.html  Manager — client profile + visit timeline
═══════════════════════════════════════════════
