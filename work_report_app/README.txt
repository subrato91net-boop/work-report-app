═══════════════════════════════════════════════
  WORK REPORT SYSTEM V3 — PostgreSQL Version
  Imax Solution & Conneqtor Technology
═══════════════════════════════════════════════

WHAT'S DIFFERENT FROM V2
- Uses PostgreSQL instead of SQLite
- Data is permanently stored — never lost on restart
- Works perfectly on Render free plan
- Real BioTime attendance data

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
  FILES
═══════════════════════════════════════════════

  app.py              Main app + PostgreSQL + BioTime API
  requirements.txt    flask, gunicorn, requests, psycopg2-binary
  Procfile            For Render: web: gunicorn app:app
  templates/
    login.html
    form.html
    manager.html
    attendance.html
═══════════════════════════════════════════════
