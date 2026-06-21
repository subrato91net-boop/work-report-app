from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from datetime import datetime, timedelta
import os, csv, io, hashlib, secrets, string
import requests as req

# ══════════════════════════════════════════
#  PostgreSQL via psycopg2
# ══════════════════════════════════════════
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = "workreport_v3_secret_2026"

# ══════════════════════════════════════════
#  DATABASE URL
#  On Render: set environment variable DATABASE_URL
#  Locally:   paste your Render PostgreSQL URL below
# ══════════════════════════════════════════
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "YOUR_RENDER_POSTGRESQL_INTERNAL_URL_HERE"
)

# Render gives URL starting with postgres:// — psycopg2 needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ══════════════════════════════════════════
#  BIOTIME API CONFIG
# ══════════════════════════════════════════
BIOTIME_URL      = "https://imaxsol.itimedev.minervaiot.com"
BIOTIME_EMAIL    = "presales@conneqtortech.com"
BIOTIME_PASSWORD = "Y@jh_ro@562"
BIOTIME_COMPANY  = "imaxsol"
PUNCH_START_HOUR = 6
PUNCH_END_HOUR   = 23

# ══════════════════════════════════════════
#  USER SYSTEM (DB-BACKED, with legacy seed data)
#  EMPLOYEES / USERNAME_MAP are kept as live globals,
#  refreshed from the `users` table, so every existing
#  call site (EMPLOYEES[...], EMPLOYEES.items(), etc.)
#  keeps working unmodified.
# ══════════════════════════════════════════
SEED_EMPLOYEES = {
    "1002": {"name": "Sayed Asif Ismail",   "company": "imaxsol",             "username": "asif",    "password": "1002123456"},
    "1003": {"name": "Kartick Mondal",       "company": "imaxsol",             "username": "kartick", "password": "1003123456"},
    "1004": {"name": "Sukumar Mondal",       "company": "imaxsol",             "username": "sukumar", "password": "1004123456"},
    "1005": {"name": "Ashim Kayal",          "company": "imaxsol",             "username": "ashim",   "password": "1005123456"},
    "1012": {"name": "Sujata Pahari",        "company": "imaxsol",             "username": "sujata",  "password": "1012123456"},
    "2001": {"name": "Gourab Kumar Das",     "company": "imaxsol",             "username": "gourab",  "password": "2001123456"},
    "1013": {"name": "Subrato Halder",       "company": "imaxsol",             "username": "subrato", "password": "1013123456"},
    "2002": {"name": "Pritam Pal",           "company": "CONNEQTORTECHNOLOGY", "username": "pritam",  "password": "2002123456"},
}
MANAGERS = {"manager": {"password": "manager123", "name": "Manager"}}

EMPLOYEES    = {}   # populated by refresh_employees() below
USERNAME_MAP = {}

def hash_password(raw):
    """Salted SHA-256. Stored as 'salt$hash'."""
    salt = secrets.token_hex(8)
    h    = hashlib.sha256((salt + raw).encode()).hexdigest()
    return f"{salt}${h}"

def verify_password(raw, stored):
    if not stored or "$" not in stored:
        return False
    salt, h = stored.split("$", 1)
    return hashlib.sha256((salt + raw).encode()).hexdigest() == h

def generate_temp_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def init_users_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            emp_code        TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            username        TEXT UNIQUE NOT NULL,
            password_hash   TEXT NOT NULL,
            company         TEXT,
            is_active       BOOLEAN DEFAULT TRUE,
            can_work_report BOOLEAN DEFAULT TRUE,
            can_sales_visit BOOLEAN DEFAULT TRUE,
            can_my_jobs     BOOLEAN DEFAULT TRUE,
            can_ta          BOOLEAN DEFAULT TRUE,
            created_at      TEXT,
            created_by      TEXT
        )
    """)
    # In case upgrading from an even older partial schema
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_work_report BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_sales_visit BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_my_jobs BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_ta BOOLEAN DEFAULT TRUE")
    conn.commit()

    # One-time seed: only runs if the table is empty, so existing
    # deployments/logins are never disrupted.
    cur.execute("SELECT COUNT(*) AS c FROM users")
    if cur.fetchone()["c"] == 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for code, info in SEED_EMPLOYEES.items():
            cur.execute("""
                INSERT INTO users (emp_code, name, username, password_hash, company,
                                    is_active, can_work_report, can_sales_visit, can_my_jobs, can_ta,
                                    created_at, created_by)
                VALUES (%s,%s,%s,%s,%s, TRUE, TRUE, TRUE, TRUE, TRUE, %s, 'system-seed')
                ON CONFLICT (emp_code) DO NOTHING
            """, (code, info["name"], info["username"], hash_password(info["password"]), info["company"], now))
        conn.commit()
        print(f"✅ Seeded {len(SEED_EMPLOYEES)} users into users table")

    cur.close(); conn.close()

def refresh_employees():
    """Reload the EMPLOYEES / USERNAME_MAP globals from the users table.
    Keeps the dict-shaped 'EMPLOYEES[code][\"name\"/\"company\"/\"password\"]' contract
    used throughout the rest of the app, for active users only."""
    global EMPLOYEES, USERNAME_MAP
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT emp_code, name, username, password_hash, company,
               is_active, can_work_report, can_sales_visit, can_my_jobs, can_ta
        FROM users WHERE is_active = TRUE
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    new_employees = {}
    new_username_map = {}
    for r in rows:
        new_employees[r["emp_code"]] = {
            "name": r["name"], "company": r["company"] or "",
            "username": r["username"], "password_hash": r["password_hash"],
            "can_work_report": r["can_work_report"], "can_sales_visit": r["can_sales_visit"],
            "can_my_jobs": r["can_my_jobs"], "can_ta": r["can_ta"],
        }
        new_username_map[r["username"]] = r["emp_code"]
    EMPLOYEES = new_employees
    USERNAME_MAP = new_username_map

# ══════════════════════════════════════════
#  DATABASE HELPERS
# ══════════════════════════════════════════
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id          SERIAL PRIMARY KEY,
            timestamp   TEXT,
            emp_code    TEXT,
            emp_name    TEXT,
            company     TEXT,
            date        TEXT,
            work_type   TEXT,
            client_name TEXT,
            location    TEXT,
            summary     TEXT,
            remarks     TEXT,
            status      TEXT,
            supervisor_code TEXT,
            supervisor_name TEXT
        )
    """)
    # Add supervisor columns if upgrading from an older table that lacks them
    cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS supervisor_code TEXT")
    cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS supervisor_name TEXT")

    # ── Review workflow (employee submits Completed -> locked -> manager Approve/Reject) ──
    # review_status: 'Draft' (editable) | 'Awaiting Review' (locked) | 'Approved' (locked) | 'Rejected' (editable again)
    cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'Draft'")
    cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS reject_reason TEXT")
    cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS reviewed_by TEXT")
    cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS reviewed_at TEXT")
    # Backfill existing rows based on their current work-status, so nothing already
    # submitted as Completed suddenly becomes uneditable without explanation.
    cur.execute("""
        UPDATE reports SET review_status =
            CASE WHEN LOWER(status) IN ('done','completed') THEN 'Awaiting Review' ELSE 'Draft' END
        WHERE review_status IS NULL
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              SERIAL PRIMARY KEY,
            created_at      TEXT,
            assigned_by     TEXT,
            emp_codes       TEXT,
            emp_names       TEXT,
            supervisor_codes TEXT,
            supervisor_names TEXT,
            company         TEXT,
            job_title       TEXT,
            job_description TEXT,
            location        TEXT,
            start_date      TEXT,
            end_date        TEXT,
            status          TEXT DEFAULT 'Open'
        )
    """)
    # Migrate from old single-assignee schema if present
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS emp_codes TEXT")
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS emp_names TEXT")
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS supervisor_codes TEXT")
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS supervisor_names TEXT")
    cur.execute("""
        UPDATE jobs SET emp_codes = emp_code, emp_names = emp_name
        WHERE emp_codes IS NULL AND emp_code IS NOT NULL
    """) if _column_exists(cur, "jobs", "emp_code") else None
    cur.execute("""
        UPDATE jobs SET supervisor_codes = supervisor_code, supervisor_names = supervisor_name
        WHERE supervisor_codes IS NULL AND supervisor_code IS NOT NULL
    """) if _column_exists(cur, "jobs", "supervisor_code") else None

    # Add service_report and last_edited columns if upgrading from older schema
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS service_report TEXT")
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_edited TEXT")
    # Employee edit workflow columns
    cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'Normal'")
    # job_edit_requests: employee proposes changes, manager finalizes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS job_edit_requests (
            id                  SERIAL PRIMARY KEY,
            job_id              INTEGER NOT NULL,
            submitted_at        TEXT,
            submitted_by        TEXT,
            submitted_code      TEXT,
            -- proposed values (only fields employee can change)
            prop_status         TEXT,
            prop_job_description TEXT,
            prop_service_report  TEXT,
            prop_location        TEXT,
            prop_end_date        TEXT,
            employee_note        TEXT,
            -- review
            review_status       TEXT DEFAULT 'Pending',
            reviewed_at         TEXT,
            reviewed_by         TEXT,
            manager_note        TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def _column_exists(cur, table, column):
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
    """, (table, column))
    return cur.fetchone() is not None

# ══════════════════════════════════════════
#  BIOTIME API — JWT TOKEN (cached)
# ══════════════════════════════════════════
_biotime_token  = None
_token_expiry   = None

def get_biotime_token():
    global _biotime_token, _token_expiry
    if _biotime_token and _token_expiry and datetime.now() < _token_expiry:
        return _biotime_token
    try:
        res = req.post(
            f"{BIOTIME_URL}/jwt-api-token-auth/",
            json={"company": BIOTIME_COMPANY, "email": BIOTIME_EMAIL, "password": BIOTIME_PASSWORD},
            timeout=15
        )
        if res.status_code == 200:
            data = res.json()
            _biotime_token = data.get("token") or data.get("access")
            _token_expiry  = datetime.now() + timedelta(hours=23)
            return _biotime_token
    except Exception as e:
        print(f"BioTime auth error: {e}")
    return None

# ══════════════════════════════════════════
#  BIOTIME — FETCH ALL TRANSACTIONS (paginated)
# ══════════════════════════════════════════
def fetch_transactions(date_from, date_to):
    token = get_biotime_token()
    if not token:
        return []
    headers  = {"Authorization": f"JWT {token}"}
    all_data = []
    url      = f"{BIOTIME_URL}/iclock/api/transactions/"
    params   = {
        "start_time": f"{date_from} {PUNCH_START_HOUR:02d}:00:00",
        "end_time":   f"{date_to} {PUNCH_END_HOUR:02d}:59:59",
        "page_size":  500,
    }
    while url:
        try:
            res = req.get(url, headers=headers, params=params, timeout=20)
            if res.status_code == 200:
                data     = res.json()
                all_data.extend(data.get("data", []))
                url      = data.get("next")
                params   = {}
            else:
                break
        except Exception as e:
            print(f"BioTime fetch error: {e}")
            break
    return all_data

# ══════════════════════════════════════════
#  PROCESS TRANSACTIONS → ATTENDANCE
# ══════════════════════════════════════════
def process_attendance(transactions, date_from, date_to):
    from collections import defaultdict
    punch_map = defaultdict(list)
    for t in transactions:
        emp_code   = str(t.get("emp_code", ""))
        punch_time = t.get("punch_time", "")
        if not emp_code or not punch_time or emp_code not in EMPLOYEES:
            continue
        try:
            dt = datetime.strptime(punch_time[:19], "%Y-%m-%d %H:%M:%S")
            if PUNCH_START_HOUR <= dt.hour <= PUNCH_END_HOUR:
                punch_map[(emp_code, dt.strftime("%Y-%m-%d"))].append(dt)
        except:
            continue

    records = []
    today   = datetime.now().strftime("%Y-%m-%d")
    for emp_code, emp_info in EMPLOYEES.items():
        for date_str in get_date_range(date_from, date_to):
            punches = sorted(punch_map.get((emp_code, date_str), []))
            if punches:
                check_in  = punches[0]
                check_out = punches[-1] if len(punches) > 1 else None
                hours     = round((check_out - check_in).seconds / 3600, 1) if check_out else 0
                status    = "Half Day" if 0 < hours < 5 else "Present"
                records.append({
                    "emp_code":    emp_code,
                    "emp_name":    emp_info["name"],
                    "company":     emp_info["company"],
                    "date":        date_str,
                    "check_in":    check_in.strftime("%H:%M"),
                    "check_out":   check_out.strftime("%H:%M") if check_out else "—",
                    "hours":       hours,
                    "punch_count": len(punches),
                    "status":      status,
                })
            elif date_str <= today:
                records.append({
                    "emp_code": emp_code, "emp_name": emp_info["name"],
                    "company":  emp_info["company"], "date": date_str,
                    "check_in": "—", "check_out": "—",
                    "hours": 0, "punch_count": 0, "status": "Absent",
                })
    return sorted(records, key=lambda x: (x["date"], x["emp_name"]), reverse=True)

def get_date_range(date_from, date_to):
    dates, cur, end = [], datetime.strptime(date_from, "%Y-%m-%d"), datetime.strptime(date_to, "%Y-%m-%d")
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates

def determine_status(hours):
    return "Half Day" if 0 < hours < 5 else "Present"

# ══════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════
def logged_in():  return "username" in session
def is_manager(): return session.get("role") == "manager"
def get_emp_code(): return session.get("emp_code")

def has_perm(perm_key):
    """Manager (super admin) always passes. Employees are gated by their
    session-cached permission flags, set at login time."""
    if is_manager():
        return True
    return session.get("perms", {}).get(perm_key, True)

def codes_to_names(codes_str):
    """'1002,1003' -> 'Sayed Asif Ismail, Kartick Mondal'"""
    if not codes_str:
        return ""
    codes = [c.strip() for c in codes_str.split(",") if c.strip()]
    names = [EMPLOYEES[c]["name"] for c in codes if c in EMPLOYEES]
    return ", ".join(names)

def parse_codes(codes_str):
    if not codes_str:
        return []
    return [c.strip() for c in codes_str.split(",") if c.strip()]

# ══════════════════════════════════════════
#  ROUTES — AUTH
# ══════════════════════════════════════════
@app.route("/")
def index():
    if not logged_in(): return redirect(url_for("login"))
    if is_manager(): return redirect(url_for("dashboard"))
    # Land the employee on the first feature they actually have access to
    if has_perm("work_report"): return redirect(url_for("employee_form"))
    if has_perm("sales_visit"): return redirect(url_for("sales_visit"))
    if has_perm("my_jobs"):     return redirect(url_for("my_jobs"))
    if has_perm("ta"):          return redirect(url_for("ta_report"))
    return redirect(url_for("no_access"))

@app.route("/dashboard")
def dashboard():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))

    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db(); cur = conn.cursor()

    # ── Work reports: today's submission completion ──
    cur.execute("SELECT COUNT(DISTINCT emp_code) AS c FROM reports WHERE date=%s", (today,))
    submitted_today = cur.fetchone()["c"]
    total_active_employees = len(EMPLOYEES)  # live in-memory dict, already filtered to active

    submitted_codes = set()
    cur.execute("SELECT DISTINCT emp_code FROM reports WHERE date=%s", (today,))
    for r in cur.fetchall():
        submitted_codes.add(r["emp_code"])
    missing_names = sorted(
        info["name"] for code, info in EMPLOYEES.items()
        if info.get("can_work_report") and code not in submitted_codes
    )

    cur.execute("SELECT COUNT(*) AS c FROM reports")
    total_reports = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM reports WHERE LOWER(status) IN ('done','completed')")
    completed_reports = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM reports WHERE LOWER(status)='pending'")
    pending_reports = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM reports WHERE review_status='Awaiting Review'")
    awaiting_review_count = cur.fetchone()["c"]

    # ── Jobs: status breakdown ──
    cur.execute("""
        SELECT LOWER(COALESCE(status,'open')) AS s, COUNT(*) AS c
        FROM jobs GROUP BY LOWER(COALESCE(status,'open'))
    """)
    job_counts = {"open": 0, "in progress": 0, "done": 0, "on hold": 0}
    for r in cur.fetchall():
        if r["s"] in job_counts:
            job_counts[r["s"]] = r["c"]
    cur.execute("SELECT COUNT(*) AS c FROM jobs")
    total_jobs = cur.fetchone()["c"]

    # ── TA reports: pending approvals ──
    cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(expense_cost),0) AS amt FROM ta_reports WHERE approval_status='Not Approved'")
    ta_row = cur.fetchone()
    pending_ta_count = ta_row["c"]
    pending_ta_amount = float(ta_row["amt"])
    cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(expense_cost),0) AS amt FROM ta_reports WHERE payment_status='Due'")
    due_row = cur.fetchone()
    due_ta_count = due_row["c"]
    due_ta_amount = float(due_row["amt"])

    # ── Clients: stale accounts (no visit in 30+ days) ──
    cur.execute("""
        SELECT c.id, c.name, MAX(v.visit_date) AS last_visit
        FROM companies c
        LEFT JOIN sales_visits v ON v.company_id = c.id
        GROUP BY c.id, c.name
    """)
    today_d = datetime.now().date()
    stale_clients = []
    total_clients = 0
    for row in cur.fetchall():
        total_clients += 1
        if row["last_visit"]:
            try:
                d = datetime.strptime(row["last_visit"], "%Y-%m-%d").date()
                days = (today_d - d).days
            except Exception:
                days = None
        else:
            days = None
        if days is None or days >= 30:
            stale_clients.append({"id": row["id"], "name": row["name"], "days": days})
    stale_clients.sort(key=lambda x: (x["days"] is not None, x["days"] or 9999), reverse=True)

    # ── Sales visits today ──
    cur.execute("SELECT COUNT(*) AS c FROM sales_visits WHERE visit_date=%s", (today,))
    visits_today = cur.fetchone()["c"]

    # ── Recent activity feed (latest across reports, jobs, ta) ──
    activity = []
    cur.execute("SELECT timestamp, emp_name, status FROM reports ORDER BY timestamp DESC LIMIT 5")
    for r in cur.fetchall():
        activity.append({"ts": r["timestamp"], "text": f"{r['emp_name']} submitted a work report", "tag": "report"})
    cur.execute("SELECT created_at, job_title, emp_names FROM jobs ORDER BY created_at DESC LIMIT 5")
    for r in cur.fetchall():
        who = r["emp_names"] or "unassigned"
        activity.append({"ts": r["created_at"], "text": f"Job \"{r['job_title']}\" assigned to {who}", "tag": "job"})
    cur.execute("SELECT timestamp, emp_name, expense_cost FROM ta_reports ORDER BY timestamp DESC LIMIT 5")
    for r in cur.fetchall():
        activity.append({"ts": r["timestamp"], "text": f"{r['emp_name']} submitted a travel expense (₹{float(r['expense_cost'] or 0):.0f})", "tag": "ta"})
    activity = [a for a in activity if a["ts"]]
    activity.sort(key=lambda a: a["ts"], reverse=True)
    activity = activity[:8]

    cur.close(); conn.close()

    completion_pct = round((submitted_today / total_active_employees) * 100) if total_active_employees else 0

    return render_template(
        "dashboard.html",
        today=today,
        submitted_today=submitted_today,
        total_active_employees=total_active_employees,
        completion_pct=completion_pct,
        missing_names=missing_names,
        total_reports=total_reports, completed_reports=completed_reports, pending_reports=pending_reports,
        awaiting_review_count=awaiting_review_count,
        job_counts=job_counts, total_jobs=total_jobs,
        pending_ta_count=pending_ta_count, pending_ta_amount=pending_ta_amount,
        due_ta_count=due_ta_count, due_ta_amount=due_ta_amount,
        stale_clients=stale_clients[:6], stale_clients_total=len(stale_clients), total_clients=total_clients,
        visits_today=visits_today,
        activity=activity,
    )

@app.route("/no-access")
def no_access():
    if not logged_in(): return redirect(url_for("login"))
    return render_template("no_access.html", name=session.get("name", ""))

@app.route("/login", methods=["GET","POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username","").strip().lower()
        password = request.form.get("password","").strip()
        if username in MANAGERS and MANAGERS[username]["password"] == password:
            session.update({"username":username,"name":MANAGERS[username]["name"],"role":"manager"})
            return redirect(url_for("index"))
        refresh_employees()
        emp_code = USERNAME_MAP.get(username)
        if emp_code:
            emp = EMPLOYEES[emp_code]
            if verify_password(password, emp["password_hash"]):
                session.update({
                    "username": username, "name": emp["name"], "role": "employee",
                    "emp_code": emp_code, "company": emp["company"],
                    "perms": {
                        "work_report": emp["can_work_report"],
                        "sales_visit": emp["can_sales_visit"],
                        "my_jobs":     emp["can_my_jobs"],
                        "ta":          emp["can_ta"],
                        "support":     emp.get("can_support", True),
                    },
                })
                return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE FORM
# ══════════════════════════════════════════
@app.route("/form", methods=["GET","POST"])
def employee_form():
    if not logged_in() or is_manager(): return redirect(url_for("index"))
    if not has_perm("work_report"): return redirect(url_for("no_access"))
    success = False
    lock_error = False
    if request.method == "POST":
        sup_code = request.form.get("supervisor_code", "")
        sup_name = EMPLOYEES.get(sup_code, {}).get("name", "")
        new_status = request.form.get("status")
        # A Completed submission is itself the review request: it locks immediately.
        # Pending/Partial stay editable drafts. Resubmitting a Rejected report as
        # Completed sends it back into the review queue.
        new_review_status = "Awaiting Review" if (new_status or "").lower() in ("done", "completed") else "Draft"
        edit_id = request.form.get("edit_id", "").strip()

        conn = get_db(); cur = conn.cursor()
        if edit_id:
            # Only allow editing rows that are still unlocked (Draft or Rejected) and
            # belong to this employee. The WHERE clause is the actual security boundary
            # here, not just the UI hiding the edit button.
            cur.execute("""
                UPDATE reports
                SET company=%s, date=%s, work_type=%s, client_name=%s, location=%s,
                    summary=%s, remarks=%s, status=%s, supervisor_code=%s, supervisor_name=%s,
                    review_status=%s, reject_reason=NULL
                WHERE id=%s AND emp_code=%s AND review_status IN ('Draft','Rejected')
            """, (
                session.get("company",""), request.form.get("date"), request.form.get("work_type"),
                request.form.get("client_name"), request.form.get("location"),
                request.form.get("summary"), request.form.get("remarks"),
                new_status, sup_code, sup_name, new_review_status,
                edit_id, get_emp_code(),
            ))
            if cur.rowcount == 0:
                lock_error = True
            else:
                success = True
        else:
            cur.execute("""
                INSERT INTO reports
                (timestamp,emp_code,emp_name,company,date,work_type,client_name,location,summary,remarks,status,supervisor_code,supervisor_name,review_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                get_emp_code(), session["name"], session.get("company",""),
                request.form.get("date"), request.form.get("work_type"),
                request.form.get("client_name"), request.form.get("location"),
                request.form.get("summary"), request.form.get("remarks"),
                new_status, sup_code, sup_name, new_review_status,
            ))
            success = True
        conn.commit(); cur.close(); conn.close()

    # filters for "my reports" history (own reports only)
    f_wtype  = request.args.get("wtype", "")
    f_status = request.args.get("status", "")
    f_from   = request.args.get("from_d", "")
    f_to     = request.args.get("to_d", "")
    f_search = request.args.get("search", "")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM reports WHERE emp_code=%s"
    params = [get_emp_code()]
    if f_wtype:  query += " AND work_type=%s";        params.append(f_wtype)
    if f_status: query += " AND LOWER(status)=%s";    params.append(f_status.lower())
    if f_from:   query += " AND date>=%s";             params.append(f_from)
    if f_to:     query += " AND date<=%s";             params.append(f_to)
    if f_search:
        query += " AND (client_name ILIKE %s OR location ILIKE %s OR summary ILIKE %s OR remarks ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s, s]
    query += " ORDER BY timestamp DESC"
    cur.execute(query, params)
    recent = cur.fetchall()
    cur.close(); conn.close()

    today     = datetime.now().strftime("%Y-%m-%d")
    att_today = []
    try:
        txns      = fetch_transactions(today, today)
        att_today = [a for a in process_attendance(txns, today, today) if a["emp_code"] == get_emp_code()]
    except: pass

    # supervisor choices = everyone except the logged-in employee
    supervisor_choices = [
        {"code": code, "name": info["name"]}
        for code, info in EMPLOYEES.items() if code != get_emp_code()
    ]

    return render_template("form.html", name=session["name"], success=success, lock_error=lock_error, recent=recent,
                            att_today=att_today, supervisor_choices=supervisor_choices,
                            record_count=len(recent), perms=session.get("perms", {}),
                            filters={"wtype": f_wtype, "status": f_status, "from_d": f_from, "to_d": f_to, "search": f_search})

# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE: VIEW ASSIGNED JOBS
# ══════════════════════════════════════════
@app.route("/my-jobs")
def my_jobs():
    if not logged_in() or is_manager(): return redirect(url_for("index"))
    if not has_perm("my_jobs"): return redirect(url_for("no_access"))
    code = get_emp_code()

    # filters
    f_status = request.args.get("status", "")
    f_search = request.args.get("search", "")
    f_from   = request.args.get("from_d", "")
    f_to     = request.args.get("to_d", "")

    conn = get_db(); cur = conn.cursor()
    query  = """
        SELECT * FROM jobs
        WHERE ((',' || emp_codes || ',') LIKE %s
           OR  (',' || supervisor_codes || ',') LIKE %s)
    """
    params = [f"%,{code},%", f"%,{code},%"]
    if f_status: query += " AND LOWER(status)=LOWER(%s)"; params.append(f_status)
    if f_from:   query += " AND start_date>=%s";           params.append(f_from)
    if f_to:     query += " AND start_date<=%s";           params.append(f_to)
    if f_search:
        s = f"%{f_search}%"
        query += " AND (job_title ILIKE %s OR company ILIKE %s OR location ILIKE %s)"
        params += [s, s, s]
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    jobs = cur.fetchall()

    # which job IDs have a pending edit from this employee
    cur.execute("""
        SELECT job_id FROM job_edit_requests
        WHERE submitted_code=%s AND review_status='Pending'
    """, (code,))
    pending_ids = {r["job_id"] for r in cur.fetchall()}

    # recent reviewed results to show notification banners
    cur.execute("""
        SELECT jer.*, j.job_title FROM job_edit_requests jer
        JOIN jobs j ON j.id=jer.job_id
        WHERE jer.submitted_code=%s AND jer.review_status IN ('Finalized','Declined')
        ORDER BY jer.reviewed_at DESC LIMIT 5
    """, (code,))
    recent_results = cur.fetchall()

    cur.close(); conn.close()

    enriched = []
    for j in jobs:
        is_emp = code in parse_codes(j.get("emp_codes"))
        is_sup = code in parse_codes(j.get("supervisor_codes"))
        role = "Both" if (is_emp and is_sup) else ("Supervisor" if is_sup else "Employee")
        enriched.append({**j, "viewer_role": role, "has_pending": j["id"] in pending_ids})

    employee_choices = [{"code": c, "name": i["name"], "company": i["company"]} for c, i in EMPLOYEES.items()]

    return render_template("my_jobs.html",
        name=session["name"], jobs=enriched,
        record_count=len(enriched), perms=session.get("perms", {}),
        employee_choices=employee_choices,
        recent_results=recent_results,
        submitted=request.args.get("submitted") == "1",
        filters={"status": f_status, "search": f_search, "from_d": f_from, "to_d": f_to},
    )

# ══════════════════════════════════════════
#  ROUTES — MANAGER WORK REPORTS
# ══════════════════════════════════════════
@app.route("/manager")
def manager_view():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    emp    = request.args.get("emp","")
    wtype  = request.args.get("wtype","")
    status = request.args.get("status","")
    review = request.args.get("review","")
    from_d = request.args.get("from_d","")
    to_d   = request.args.get("to_d","")
    search = request.args.get("search","")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM reports WHERE 1=1"
    params = []
    if emp:    query += " AND emp_name=%s";                              params.append(emp)
    if wtype:  query += " AND work_type=%s";                             params.append(wtype)
    if status: query += " AND LOWER(status)=%s";                         params.append(status.lower())
    if review: query += " AND review_status=%s";                         params.append(review)
    if from_d: query += " AND date>=%s";                                 params.append(from_d)
    if to_d:   query += " AND date<=%s";                                 params.append(to_d)
    if search:
        query += " AND (client_name ILIKE %s OR location ILIKE %s OR summary ILIKE %s OR remarks ILIKE %s)"
        s = f"%{search}%"; params += [s,s,s,s]
    query += " ORDER BY timestamp DESC"
    cur.execute(query, params); reports = cur.fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT COUNT(*) FROM reports");                                             total     = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM reports WHERE LOWER(status) IN ('done','completed')"); completed = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM reports WHERE LOWER(status)='pending'");               pending   = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM reports WHERE LOWER(status)='partial'");               partial   = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(DISTINCT emp_code) FROM reports WHERE date=%s", (today,));     today_ct  = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM reports WHERE review_status='Awaiting Review'");       awaiting_review = cur.fetchone()["count"]
    cur.execute("SELECT DISTINCT emp_name FROM reports ORDER BY emp_name");                  emp_list  = [r["emp_name"] for r in cur.fetchall()]
    cur.close(); conn.close()

    return render_template("manager.html",
        reports=reports, emp_list=emp_list,
        total=total, completed=completed, pending=pending, partial=partial, today_ct=today_ct,
        awaiting_review=awaiting_review,
        filters={"emp":emp,"wtype":wtype,"status":status,"review":review,"from_d":from_d,"to_d":to_d,"search":search},
        record_count=len(reports)
    )

@app.route("/manager/reports/<int:report_id>/approve", methods=["POST"])
def approve_report(report_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE reports SET review_status='Approved', reject_reason=NULL,
                            reviewed_by=%s, reviewed_at=%s
        WHERE id=%s AND review_status='Awaiting Review'
    """, (session.get("name","Manager"), now, report_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(request.referrer or url_for("manager_view"))

@app.route("/manager/reports/<int:report_id>/reject", methods=["POST"])
def reject_report(report_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    reason = request.form.get("reject_reason", "").strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE reports SET review_status='Rejected', reject_reason=%s,
                            reviewed_by=%s, reviewed_at=%s
        WHERE id=%s AND review_status='Awaiting Review'
    """, (reason, session.get("name","Manager"), now, report_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(request.referrer or url_for("manager_view"))

# ══════════════════════════════════════════
#  ROUTES — MANAGER: ASSIGN JOBS
# ══════════════════════════════════════════
@app.route("/assign-job", methods=["GET", "POST"])
def assign_job():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    success = False
    error   = None

    if request.method == "POST":
        emp_codes = request.form.getlist("emp_codes")     # multi-select, may be empty / contain "NA"
        sup_codes = request.form.getlist("supervisor_codes")

        emp_codes = [c for c in emp_codes if c and c != "NA"]
        sup_codes = [c for c in sup_codes if c and c != "NA"]

        if not emp_codes and not sup_codes:
            error = "Select at least one employee or supervisor (or choose N/A for the one you skip)."
        else:
            emp_names = [EMPLOYEES[c]["name"] for c in emp_codes if c in EMPLOYEES]
            sup_names = [EMPLOYEES[c]["name"] for c in sup_codes if c in EMPLOYEES]

            # default company: first matched employee/supervisor's company, if not typed manually
            default_company = ""
            if emp_codes and emp_codes[0] in EMPLOYEES:
                default_company = EMPLOYEES[emp_codes[0]]["company"]
            elif sup_codes and sup_codes[0] in EMPLOYEES:
                default_company = EMPLOYEES[sup_codes[0]]["company"]

            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                INSERT INTO jobs
                (created_at, assigned_by, emp_codes, emp_names, supervisor_codes, supervisor_names,
                 company, job_title, job_description, location, start_date, end_date, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                session.get("name", "Manager"),
                ",".join(emp_codes), ", ".join(emp_names),
                ",".join(sup_codes), ", ".join(sup_names),
                request.form.get("company") or default_company,
                request.form.get("job_title"),
                request.form.get("job_description"),
                request.form.get("location"),
                request.form.get("start_date"),
                request.form.get("end_date"),
                request.form.get("status") or "Open",
            ))
            conn.commit(); cur.close(); conn.close()
            success = True

    # filters for the "All Assigned Jobs" list
    f_emp    = request.args.get("emp", "")
    f_status = request.args.get("status", "")
    f_search = request.args.get("search", "")
    f_from   = request.args.get("from_d", "")
    f_to     = request.args.get("to_d", "")
    f_review = request.args.get("review", "")

    conn  = get_db(); cur = conn.cursor()
    query = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if f_emp:    query += " AND (emp_names ILIKE %s OR supervisor_names ILIKE %s)"; params += [f"%{f_emp}%", f"%{f_emp}%"]
    if f_status: query += " AND LOWER(status)=LOWER(%s)"; params.append(f_status)
    if f_from:   query += " AND start_date>=%s";           params.append(f_from)
    if f_to:     query += " AND start_date<=%s";           params.append(f_to)
    if f_review == "pending": query += " AND review_status='Pending Employee Edit'"
    if f_search:
        s = f"%{f_search}%"
        query += " AND (job_title ILIKE %s OR company ILIKE %s OR location ILIKE %s OR job_description ILIKE %s)"
        params += [s, s, s, s]
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    jobs = cur.fetchall()

    # pending edit requests count (for badge)
    cur.execute("SELECT COUNT(*) AS c FROM job_edit_requests WHERE review_status='Pending'")
    pending_edits_ct = cur.fetchone()["c"]

    # per-job pending edit requests (to show inline)
    cur.execute("""
        SELECT jer.*, j.job_title, j.job_description AS orig_desc, j.status AS orig_status,
               j.service_report AS orig_report, j.location AS orig_location, j.end_date AS orig_end_date
        FROM job_edit_requests jer
        JOIN jobs j ON j.id = jer.job_id
        WHERE jer.review_status='Pending'
        ORDER BY jer.submitted_at DESC
    """)
    pending_requests = {r["job_id"]: r for r in cur.fetchall()}
    cur.close(); conn.close()

    employee_choices = [{"code": c, "name": i["name"], "company": i["company"]} for c, i in EMPLOYEES.items()]

    edited   = request.args.get("edited")   == "1"
    finalized= request.args.get("finalized")== "1"
    declined = request.args.get("declined") == "1"
    return render_template("assign_job.html",
        success=success, error=error, edited=edited,
        finalized=finalized, declined=declined,
        employee_choices=employee_choices,
        jobs=jobs, record_count=len(jobs),
        pending_edits_ct=pending_edits_ct,
        pending_requests=pending_requests,
        filters={"emp": f_emp, "status": f_status, "search": f_search,
                 "from_d": f_from, "to_d": f_to, "review": f_review}
    )


# ══════════════════════════════════════════
#  ROUTES — MANAGER: EDIT JOB (modal POST)
# ══════════════════════════════════════════
@app.route("/edit-job/<int:job_id>", methods=["POST"])
def edit_job(job_id):
    if not logged_in() or not is_manager():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403

    emp_codes = request.form.getlist("emp_codes")
    sup_codes = request.form.getlist("supervisor_codes")
    emp_codes = [c for c in emp_codes if c and c != "NA"]
    sup_codes = [c for c in sup_codes if c and c != "NA"]

    emp_names = [EMPLOYEES[c]["name"] for c in emp_codes if c in EMPLOYEES]
    sup_names = [EMPLOYEES[c]["name"] for c in sup_codes if c in EMPLOYEES]

    company       = request.form.get("company", "")
    job_title     = request.form.get("job_title", "")
    job_desc      = request.form.get("job_description", "")
    location      = request.form.get("location", "")
    start_date    = request.form.get("start_date", "")
    end_date      = request.form.get("end_date", "")
    status        = request.form.get("status", "Open")
    service_report= request.form.get("service_report", "")

    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE jobs SET
            emp_codes        = %s,
            emp_names        = %s,
            supervisor_codes = %s,
            supervisor_names = %s,
            company          = %s,
            job_title        = %s,
            job_description  = %s,
            location         = %s,
            start_date       = %s,
            end_date         = %s,
            status           = %s,
            service_report   = %s,
            last_edited      = %s
        WHERE id = %s
    """, (
        ",".join(emp_codes), ", ".join(emp_names),
        ",".join(sup_codes), ", ".join(sup_names),
        company, job_title, job_desc, location,
        start_date, end_date, status, service_report,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        job_id,
    ))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("assign_job") + "?edited=1")

@app.route("/jobs/bulk-status", methods=["POST"])
def bulk_job_status():
    if not logged_in() or not is_manager():
        return jsonify({"ok": False, "error": "Unauthorised"}), 403

    ids = request.form.getlist("selected_ids")
    ids = [int(i) for i in ids if i.isdigit()]
    new_status = request.form.get("bulk_status", "")

    valid_statuses = {"Open", "In Progress", "Done", "On Hold"}
    if ids and new_status in valid_statuses:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            UPDATE jobs SET status=%s, last_edited=%s
            WHERE id = ANY(%s)
        """, (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ids))
        conn.commit(); cur.close(); conn.close()

    return redirect(url_for("assign_job", **{k: v for k, v in request.args.items()}))

# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE: SUBMIT JOB EDIT REQUEST
# ══════════════════════════════════════════
@app.route("/my-jobs/edit/<int:job_id>", methods=["POST"])
def employee_submit_job_edit(job_id):
    if not logged_in() or is_manager():
        return redirect(url_for("login"))
    if not has_perm("my_jobs"):
        return redirect(url_for("no_access"))
    code = get_emp_code()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))
    job = cur.fetchone()
    if not job:
        cur.close(); conn.close()
        return redirect(url_for("my_jobs"))
    emp_codes = parse_codes(job.get("emp_codes"))
    sup_codes = parse_codes(job.get("supervisor_codes"))
    if code not in emp_codes and code not in sup_codes:
        cur.close(); conn.close()
        return redirect(url_for("no_access"))
    cur.execute(
        "DELETE FROM job_edit_requests WHERE job_id=%s AND submitted_code=%s AND review_status='Pending'",
        (job_id, code)
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO job_edit_requests
        (job_id, submitted_at, submitted_by, submitted_code,
         prop_status, prop_job_description, prop_service_report,
         prop_location, prop_end_date, employee_note, review_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pending')
    """, (
        job_id, now, session["name"], code,
        request.form.get("prop_status", ""),
        request.form.get("prop_job_description", ""),
        request.form.get("prop_service_report", ""),
        request.form.get("prop_location", ""),
        request.form.get("prop_end_date", ""),
        request.form.get("employee_note", ""),
    ))
    cur.execute("UPDATE jobs SET review_status='Pending Employee Edit' WHERE id=%s", (job_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("my_jobs") + "?submitted=1")


# ══════════════════════════════════════════
#  ROUTES — MANAGER: FINALIZE JOB EDIT
# ══════════════════════════════════════════
@app.route("/manager/jobs/<int:req_id>/finalize", methods=["POST"])
def finalize_job_edit(req_id):
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM job_edit_requests WHERE id=%s", (req_id,))
    req = cur.fetchone()
    if not req:
        cur.close(); conn.close()
        return redirect(url_for("assign_job"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    final_status   = request.form.get("final_status")   or req["prop_status"]
    final_desc     = request.form.get("final_desc")     or req["prop_job_description"]
    final_report   = request.form.get("final_report")   or req["prop_service_report"]
    final_location = request.form.get("final_location") or req["prop_location"]
    final_end_date = request.form.get("final_end_date") or req["prop_end_date"]
    manager_note   = request.form.get("manager_note", "")
    cur.execute("""
        UPDATE jobs SET
            status          = COALESCE(NULLIF(%s,''), status),
            job_description = COALESCE(NULLIF(%s,''), job_description),
            service_report  = COALESCE(NULLIF(%s,''), service_report),
            location        = COALESCE(NULLIF(%s,''), location),
            end_date        = COALESCE(NULLIF(%s,''), end_date),
            review_status   = 'Finalized',
            last_edited     = %s
        WHERE id=%s
    """, (final_status, final_desc, final_report, final_location, final_end_date, now, req["job_id"]))
    cur.execute("""
        UPDATE job_edit_requests SET
            review_status='Finalized', reviewed_at=%s, reviewed_by=%s, manager_note=%s
        WHERE id=%s
    """, (now, session.get("name", "Manager"), manager_note, req_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("assign_job") + "?finalized=1")


@app.route("/manager/jobs/<int:req_id>/decline", methods=["POST"])
def decline_job_edit(req_id):
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manager_note = request.form.get("manager_note", "")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT job_id FROM job_edit_requests WHERE id=%s", (req_id,))
    req = cur.fetchone()
    if req:
        cur.execute("""
            UPDATE job_edit_requests SET
                review_status='Declined', reviewed_at=%s, reviewed_by=%s, manager_note=%s
            WHERE id=%s
        """, (now, session.get("name", "Manager"), manager_note, req_id))
        cur.execute("UPDATE jobs SET review_status='Normal' WHERE id=%s", (req["job_id"],))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("assign_job") + "?declined=1")


# ══════════════════════════════════════════
#  ROUTES — ATTENDANCE TAB
# ══════════════════════════════════════════
@app.route("/attendance")
def attendance():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    today    = datetime.now().strftime("%Y-%m-%d")
    from_d   = request.args.get("from_d", today)
    to_d     = request.args.get("to_d",   today)
    emp_f    = request.args.get("emp","")
    status_f = request.args.get("status","")
    company_f= request.args.get("company","")
    view     = request.args.get("view","daily")

    error = None; records = []
    try:
        txns    = fetch_transactions(from_d, to_d)
        records = process_attendance(txns, from_d, to_d)
    except Exception as e:
        error = str(e)

    filtered = records
    if emp_f:     filtered = [r for r in filtered if r["emp_name"] == emp_f]
    if status_f:  filtered = [r for r in filtered if r["status"]   == status_f]
    if company_f: filtered = [r for r in filtered if r["company"].lower() == company_f.lower()]

    today_recs = [r for r in records if r["date"] == today]
    hrs_list   = [r["hours"] for r in today_recs if r["hours"] > 0]
    stats = {
        "total":     len(EMPLOYEES),
        "present":   len([r for r in today_recs if r["status"] in ["Present","Half Day"]]),
        "absent":    len([r for r in today_recs if r["status"] == "Absent"]),
        "half":      len([r for r in today_recs if r["status"] == "Half Day"]),
        "avg_hours": round(sum(hrs_list)/len(hrs_list), 1) if hrs_list else 0,
    }
    emp_list  = [e["name"] for e in EMPLOYEES.values()]
    date_list = get_date_range(from_d, to_d)

    return render_template("attendance.html",
        records=filtered, stats=stats, emp_list=emp_list,
        date_list=date_list, employees=EMPLOYEES,
        filters={"from_d":from_d,"to_d":to_d,"emp":emp_f,"status":status_f,"company":company_f},
        view=view, error=error, record_count=len(filtered)
    )

# ══════════════════════════════════════════
#  ROUTES — EXPORT CSV
# ══════════════════════════════════════════
@app.route("/export/reports")
def export_reports():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM reports ORDER BY timestamp DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Timestamp","Emp Code","Emp Name","Company","Date","Work Type","Client","Location","Summary","Remarks","Status","Supervisor"])
    for r in rows:
        writer.writerow([r["id"],r["timestamp"],r["emp_code"],r["emp_name"],r["company"],
                         r["date"],r["work_type"],r["client_name"],r["location"],r["summary"],r["remarks"],r["status"],r.get("supervisor_name") or ""])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":"attachment;filename=work_reports.csv"})

@app.route("/export/attendance")
def export_attendance():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    from_d = request.args.get("from_d", datetime.now().strftime("%Y-%m-%d"))
    to_d   = request.args.get("to_d",   datetime.now().strftime("%Y-%m-%d"))
    recs   = process_attendance(fetch_transactions(from_d, to_d), from_d, to_d)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Emp Code","Emp Name","Company","Date","Check In","Check Out","Hours","Punches","Status"])
    for r in recs:
        writer.writerow([r["emp_code"],r["emp_name"],r["company"],r["date"],
                         r["check_in"],r["check_out"],r["hours"],r["punch_count"],r["status"]])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":"attachment;filename=attendance.csv"})

# ══════════════════════════════════════════
#  ROUTES — SUPER ADMIN: USER MANAGEMENT
# ══════════════════════════════════════════
@app.route("/manager/users")
def manage_users():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    refresh_employees()

    status_filter = request.args.get("status", "")   # '', 'active', 'inactive'
    search        = request.args.get("search", "")

    conn  = get_db(); cur = conn.cursor()
    query = "SELECT * FROM users WHERE 1=1"
    params = []
    if status_filter == "active":
        query += " AND is_active = TRUE"
    elif status_filter == "inactive":
        query += " AND is_active = FALSE"
    if search:
        query += " AND (name ILIKE %s OR username ILIKE %s OR emp_code ILIKE %s)"
        s = f"%{search}%"; params += [s, s, s]
    query += " ORDER BY is_active DESC, name ASC"
    cur.execute(query, params)
    users = cur.fetchall()

    cur.execute("SELECT COUNT(*) AS c FROM users")
    total_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_active = TRUE")
    active_count = cur.fetchone()["c"]
    cur.close(); conn.close()

    return render_template("manage_users.html",
        users=users, total_count=total_count, active_count=active_count,
        inactive_count=total_count - active_count,
        filters={"status": status_filter, "search": search},
        record_count=len(users),
        flash=request.args.get("flash", ""),
        flash_type=request.args.get("flash_type", "success"),
        temp_password=request.args.get("temp_password", ""),
        temp_password_for=request.args.get("temp_password_for", ""),
    )

@app.route("/manager/users/create", methods=["POST"])
def create_user():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))

    emp_code = request.form.get("emp_code", "").strip()
    name     = request.form.get("name", "").strip()
    username = request.form.get("username", "").strip().lower()
    company  = request.form.get("company", "").strip()
    password = request.form.get("password", "").strip()
    can_work_report = "can_work_report" in request.form
    can_sales_visit = "can_sales_visit" in request.form
    can_my_jobs     = "can_my_jobs" in request.form
    can_ta          = "can_ta" in request.form
    can_support     = "can_support" in request.form
    can_products    = "can_products" in request.form

    if not emp_code or not name or not username or not password:
        return redirect(url_for("manage_users", flash="All fields are required to create a user.", flash_type="error"))
    if len(password) < 6:
        return redirect(url_for("manage_users", flash="Password must be at least 6 characters.", flash_type="error"))

    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM users WHERE emp_code=%s OR username=%s", (emp_code, username))
        if cur.fetchone():
            cur.close(); conn.close()
            return redirect(url_for("manage_users", flash="Employee code or username already exists.", flash_type="error"))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            INSERT INTO users (emp_code, name, username, password_hash, company,
                                is_active, can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support, can_products,
                                created_at, created_by)
            VALUES (%s,%s,%s,%s,%s, TRUE, %s,%s,%s,%s,%s,%s, %s,%s)
        """, (emp_code, name, username, hash_password(password), company,
              can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support, can_products, now, session.get("name", "manager")))
        conn.commit()
        refresh_employees()
        return redirect(url_for("manage_users", flash=f"User '{name}' created successfully.", flash_type="success"))
    except Exception as e:
        conn.rollback()
        return redirect(url_for("manage_users", flash=f"Error creating user: {e}", flash_type="error"))
    finally:
        cur.close(); conn.close()

@app.route("/manager/users/<emp_code>/reset-password", methods=["POST"])
def reset_user_password(emp_code):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))

    custom_password = request.form.get("new_password", "").strip()
    new_password = custom_password if custom_password else generate_temp_password()
    if len(new_password) < 6:
        return redirect(url_for("manage_users", flash="Password must be at least 6 characters.", flash_type="error"))

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name FROM users WHERE emp_code=%s", (emp_code,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return redirect(url_for("manage_users", flash="User not found.", flash_type="error"))

    cur.execute("UPDATE users SET password_hash=%s WHERE emp_code=%s", (hash_password(new_password), emp_code))
    conn.commit(); cur.close(); conn.close()
    refresh_employees()

    return redirect(url_for("manage_users",
        flash=f"Password reset for {row['name']}.", flash_type="success",
        temp_password=new_password, temp_password_for=row["name"]))

@app.route("/manager/users/<emp_code>/update-permissions", methods=["POST"])
def update_user_permissions(emp_code):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))

    can_work_report = "can_work_report" in request.form
    can_sales_visit = "can_sales_visit" in request.form
    can_my_jobs     = "can_my_jobs" in request.form
    can_ta          = "can_ta" in request.form
    can_support     = "can_support" in request.form
    can_products    = "can_products" in request.form
    name            = request.form.get("name", "").strip()
    company         = request.form.get("company", "").strip()

    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE users SET can_work_report=%s, can_sales_visit=%s, can_my_jobs=%s, can_ta=%s, can_support=%s, can_products=%s,
                          name=COALESCE(NULLIF(%s,''), name),
                          company=COALESCE(NULLIF(%s,''), company)
        WHERE emp_code=%s
    """, (can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support, can_products, name, company, emp_code))
    conn.commit(); cur.close(); conn.close()
    refresh_employees()

    return redirect(url_for("manage_users", flash="Permissions updated.", flash_type="success"))

@app.route("/manager/users/<emp_code>/toggle-active", methods=["POST"])
def toggle_user_active(emp_code):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name, is_active FROM users WHERE emp_code=%s", (emp_code,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return redirect(url_for("manage_users", flash="User not found.", flash_type="error"))

    new_status = not row["is_active"]
    cur.execute("UPDATE users SET is_active=%s WHERE emp_code=%s", (new_status, emp_code))
    conn.commit(); cur.close(); conn.close()
    refresh_employees()

    verb = "reactivated" if new_status else "deactivated"
    return redirect(url_for("manage_users", flash=f"{row['name']} has been {verb}.", flash_type="success"))

# ══════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════
with app.app_context():
    try:
        init_db()
        print("✅ Database tables ready")
    except Exception as e:
        print(f"⚠️ DB init error: {e}")

with app.app_context():
    try:
        init_users_db()
        refresh_employees()
        print(f"✅ Users table ready ({len(EMPLOYEES)} active users loaded)")
    except Exception as e:
        print(f"⚠️ Users DB init error: {e}")

if __name__ == "__main__":
    print("\n✅  Work Report System V3 (PostgreSQL) is running!")
    print("📌  Open: http://127.0.0.1:5000")
    print("👤  Manager : manager / manager123")
    print("👤  Employee: subrato / 1013123456\n")
    app.run(debug=True)


# ══════════════════════════════════════════
#  SALES VISIT REPORT — DB INIT
# ══════════════════════════════════════════
def init_sales_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_visits (
            id                  SERIAL PRIMARY KEY,
            timestamp           TEXT,
            visit_date          TEXT,
            start_time          TEXT,
            end_time            TEXT,
            client_name         TEXT,
            contact_number      TEXT,
            address             TEXT,
            type_of_visit       TEXT,
            discussion_summary  TEXT,
            products_interested TEXT,
            visit_outcome       TEXT,
            next_followup_date  TEXT,
            salesperson_code    TEXT,
            salesperson_name    TEXT,
            remark              TEXT
        )
    """)
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_sales_db()
        print("✅ Sales visit table ready")
    except Exception as e:
        print(f"⚠️ Sales DB init error: {e}")


# ══════════════════════════════════════════
#  MINI CRM — COMPANIES (CLIENTS)
# ══════════════════════════════════════════
def init_companies_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id              SERIAL PRIMARY KEY,
            name            TEXT UNIQUE NOT NULL,
            industry        TEXT,
            address         TEXT,
            phone           TEXT,
            primary_contact TEXT,
            notes           TEXT,
            owner_code      TEXT,
            created_at      TEXT
        )
    """)
    conn.commit()

    # link sales_visits to companies (nullable, non-destructive)
    cur.execute("""
        ALTER TABLE sales_visits
        ADD COLUMN IF NOT EXISTS company_id INTEGER
    """)
    conn.commit()

    # Backfill: create a company row for every distinct client_name
    # that doesn't have one yet, and link existing visits to it.
    cur.execute("""
        SELECT DISTINCT client_name, salesperson_code
        FROM sales_visits
        WHERE client_name IS NOT NULL AND client_name <> ''
          AND company_id IS NULL
    """)
    distinct_clients = cur.fetchall()
    for row in distinct_clients:
        cname = row["client_name"].strip()
        if not cname:
            continue
        cur.execute("SELECT id FROM companies WHERE name=%s", (cname,))
        existing = cur.fetchone()
        if existing:
            cid = existing["id"]
        else:
            cur.execute("""
                INSERT INTO companies (name, owner_code, created_at)
                VALUES (%s, %s, %s) RETURNING id
            """, (cname, row["salesperson_code"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            cid = cur.fetchone()["id"]
        cur.execute("""
            UPDATE sales_visits SET company_id=%s
            WHERE client_name=%s AND company_id IS NULL
        """, (cid, cname))
    conn.commit()
    cur.close(); conn.close()

def get_or_create_company(name, owner_code=None):
    """Find a company by name (case-insensitive), or create it. Returns id."""
    name = (name or "").strip()
    if not name:
        return None
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM companies WHERE LOWER(name)=LOWER(%s)", (name,))
    row = cur.fetchone()
    if row:
        cid = row["id"]
    else:
        cur.execute("""
            INSERT INTO companies (name, owner_code, created_at)
            VALUES (%s, %s, %s) RETURNING id
        """, (name, owner_code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        cid = cur.fetchone()["id"]
        conn.commit()
    cur.close(); conn.close()
    return cid

with app.app_context():
    try:
        init_companies_db()
        print("✅ Companies (mini CRM) table ready")
    except Exception as e:
        print(f"⚠️ Companies DB init error: {e}")


@app.route("/api/companies-search")
def api_companies_search():
    if not logged_in():
        return jsonify([])
    q = request.args.get("q", "").strip()
    conn = get_db(); cur = conn.cursor()
    if q:
        cur.execute("SELECT name FROM companies WHERE name ILIKE %s ORDER BY name LIMIT 10", (f"%{q}%",))
    else:
        cur.execute("SELECT name FROM companies ORDER BY name LIMIT 10")
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([r["name"] for r in rows])


# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE: SALES VISIT REPORT
# ══════════════════════════════════════════
@app.route("/sales-visit", methods=["GET", "POST"])
def sales_visit():
    if not logged_in() or is_manager():
        return redirect(url_for("index"))
    if not has_perm("sales_visit"):
        return redirect(url_for("no_access"))

    success = False
    if request.method == "POST":
        sp_code = request.form.get("salesperson_code", "")
        sp_name = EMPLOYEES.get(sp_code, {}).get("name", "")
        client_name = request.form.get("client_name")
        company_id = get_or_create_company(client_name, owner_code=sp_code)
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO sales_visits
            (timestamp, visit_date, start_time, end_time, client_name,
             contact_number, address, type_of_visit, discussion_summary,
             products_interested, visit_outcome, next_followup_date,
             salesperson_code, salesperson_name, remark, company_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            request.form.get("visit_date"),
            request.form.get("start_time"),
            request.form.get("end_time"),
            client_name,
            request.form.get("contact_number"),
            request.form.get("address"),
            request.form.get("type_of_visit"),
            request.form.get("discussion_summary"),
            request.form.get("products_interested"),
            request.form.get("visit_outcome"),
            request.form.get("next_followup_date"),
            sp_code, sp_name,
            request.form.get("remark"),
            company_id,
        ))
        conn.commit(); cur.close(); conn.close()
        success = True

    # filters for "my visit history" (own visits only)
    f_vtype   = request.args.get("vtype", "")
    f_outcome = request.args.get("outcome", "")
    f_from    = request.args.get("from_d", "")
    f_to      = request.args.get("to_d", "")
    f_search  = request.args.get("search", "")

    # Fetch this employee's own history
    code   = get_emp_code()
    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM sales_visits WHERE salesperson_code=%s"
    params = [code]
    if f_vtype:   query += " AND type_of_visit=%s";   params.append(f_vtype)
    if f_outcome: query += " AND visit_outcome=%s";   params.append(f_outcome)
    if f_from:    query += " AND visit_date>=%s";     params.append(f_from)
    if f_to:      query += " AND visit_date<=%s";     params.append(f_to)
    if f_search:
        query += " AND (client_name ILIKE %s OR address ILIKE %s OR products_interested ILIKE %s OR discussion_summary ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s, s]
    query += " ORDER BY timestamp DESC"
    cur.execute(query, params)
    history = cur.fetchall()
    cur.close(); conn.close()

    salesperson_choices = [
        {"code": c, "name": i["name"]}
        for c, i in EMPLOYEES.items()
    ]

    return render_template(
        "sales_visit.html",
        name=session["name"],
        success=success,
        history=history,
        record_count=len(history),
        salesperson_choices=salesperson_choices,
        current_code=code,
        perms=session.get("perms", {}),
        filters={"vtype": f_vtype, "outcome": f_outcome, "from_d": f_from, "to_d": f_to, "search": f_search},
    )


# ══════════════════════════════════════════
#  ROUTES — MANAGER: VIEW SALES VISITS
# ══════════════════════════════════════════
@app.route("/manager/sales-visits")
def manager_sales_visits():
    if not logged_in() or not is_manager():
        return redirect(url_for("index"))

    sp_f      = request.args.get("sp", "")
    outcome_f = request.args.get("outcome", "")
    vtype_f   = request.args.get("vtype", "")
    from_d    = request.args.get("from_d", "")
    to_d      = request.args.get("to_d", "")
    search    = request.args.get("search", "")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM sales_visits WHERE 1=1"
    params = []
    if sp_f:      query += " AND salesperson_name=%s";              params.append(sp_f)
    if outcome_f: query += " AND visit_outcome=%s";                 params.append(outcome_f)
    if vtype_f:   query += " AND type_of_visit=%s";                 params.append(vtype_f)
    if from_d:    query += " AND visit_date>=%s";                   params.append(from_d)
    if to_d:      query += " AND visit_date<=%s";                   params.append(to_d)
    if search:
        query += " AND (client_name ILIKE %s OR address ILIKE %s OR products_interested ILIKE %s OR discussion_summary ILIKE %s)"
        s = f"%{search}%"; params += [s, s, s, s]
    query += " ORDER BY timestamp DESC"
    cur.execute(query, params)
    visits = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM sales_visits"); total = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM sales_visits WHERE visit_outcome='Interested'"); interested = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM sales_visits WHERE visit_outcome='Need Follow-up'"); followup = cur.fetchone()["count"]
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT COUNT(*) FROM sales_visits WHERE visit_date=%s", (today,)); today_ct = cur.fetchone()["count"]
    cur.execute("SELECT DISTINCT salesperson_name FROM sales_visits ORDER BY salesperson_name"); sp_list = [r["salesperson_name"] for r in cur.fetchall()]
    cur.close(); conn.close()

    return render_template(
        "manager_sales.html",
        name=session["name"],
        visits=visits,
        total=total, interested=interested, followup=followup, today_ct=today_ct,
        sp_list=sp_list,
        filters={"sp": sp_f, "outcome": outcome_f, "vtype": vtype_f, "from_d": from_d, "to_d": to_d, "search": search},
        record_count=len(visits),
    )


# ══════════════════════════════════════════
#  ROUTES — EXPORT: SALES VISITS CSV
# ══════════════════════════════════════════
@app.route("/export/sales-visits")
def export_sales_visits():
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM sales_visits ORDER BY timestamp DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID","Timestamp","Visit Date","Start Time","End Time","Client Name",
        "Contact Number","Address","Type of Visit","Discussion Summary",
        "Products Interested","Visit Outcome","Next Follow-up Date",
        "Salesperson","Remark"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["timestamp"], r["visit_date"], r["start_time"], r["end_time"],
            r["client_name"], r["contact_number"], r["address"], r["type_of_visit"],
            r["discussion_summary"], r["products_interested"], r["visit_outcome"],
            r["next_followup_date"], r["salesperson_name"], r["remark"]
        ])
    output.seek(0)
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=sales_visits.csv"}
    )

# ══════════════════════════════════════════
#  TA (TRAVEL EXPENSES) REPORT — DB INIT
# ══════════════════════════════════════════
def init_ta_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ta_reports (
            id              SERIAL PRIMARY KEY,
            timestamp       TEXT,
            emp_code        TEXT,
            emp_name        TEXT,
            travel_date     TEXT,
            from_place      TEXT,
            to_place        TEXT,
            travel_by       TEXT,
            description     TEXT,
            expense_cost    NUMERIC DEFAULT 0,
            payment_status  TEXT DEFAULT 'Due',
            approval_status TEXT DEFAULT 'Not Approved',
            last_edited     TEXT,
            last_edited_by  TEXT
        )
    """)
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_ta_db()
        print("✅ TA (travel expenses) table ready")
    except Exception as e:
        print(f"⚠️ TA DB init error: {e}")

# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE: TA (TRAVEL EXPENSES) REPORT
# ══════════════════════════════════════════
@app.route("/ta-report", methods=["GET", "POST"])
def ta_report():
    if not logged_in() or is_manager():
        return redirect(url_for("index"))
    if not has_perm("ta"):
        return redirect(url_for("no_access"))

    code = get_emp_code()
    success = False
    lock_error = False

    if request.method == "POST":
        travel_date  = request.form.get("travel_date", "").strip()
        from_place   = request.form.get("from_place", "").strip()
        to_place     = request.form.get("to_place", "").strip()
        travel_by    = request.form.get("travel_by", "").strip()
        description  = request.form.get("description", "").strip()
        expense_cost = request.form.get("expense_cost", "0").strip()
        edit_id      = request.form.get("edit_id", "").strip()

        try:
            expense_cost = float(expense_cost) if expense_cost else 0.0
        except ValueError:
            expense_cost = 0.0

        if travel_date and from_place and to_place and travel_by:
            now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_db(); cur = conn.cursor()
            if edit_id:
                # Employees may only edit their own report's trip details (cols 1-5),
                # and only while it hasn't been approved yet. Status columns (6 & 7)
                # are intentionally excluded from this UPDATE regardless.
                cur.execute("""
                    UPDATE ta_reports
                    SET travel_date=%s, from_place=%s, to_place=%s, travel_by=%s,
                        description=%s, expense_cost=%s, last_edited=%s, last_edited_by=%s
                    WHERE id=%s AND emp_code=%s AND approval_status != 'Approved'
                """, (travel_date, from_place, to_place, travel_by, description,
                      expense_cost, now, session["name"], edit_id, code))
                if cur.rowcount == 0:
                    lock_error = True
                else:
                    success = True
            else:
                cur.execute("""
                    INSERT INTO ta_reports
                        (timestamp, emp_code, emp_name, travel_date, from_place, to_place,
                         travel_by, description, expense_cost, payment_status, approval_status,
                         last_edited, last_edited_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Due','Not Approved',%s,%s)
                """, (now, code, session["name"], travel_date, from_place, to_place,
                      travel_by, description, expense_cost, now, session["name"]))
                success = True
            conn.commit(); cur.close(); conn.close()
            success = True

    # filters for own TA history
    f_status   = request.args.get("status", "")
    f_approval = request.args.get("approval", "")
    f_from     = request.args.get("from_d", "")
    f_to       = request.args.get("to_d", "")
    f_search   = request.args.get("search", "")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM ta_reports WHERE emp_code=%s"
    params = [code]
    if f_status:   query += " AND payment_status=%s";   params.append(f_status)
    if f_approval: query += " AND approval_status=%s";  params.append(f_approval)
    if f_from:     query += " AND travel_date>=%s";     params.append(f_from)
    if f_to:       query += " AND travel_date<=%s";     params.append(f_to)
    if f_search:
        query += " AND (from_place ILIKE %s OR to_place ILIKE %s OR description ILIKE %s OR travel_by ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s, s]
    query += " ORDER BY travel_date DESC, id DESC"
    cur.execute(query, params)
    reports = cur.fetchall()

    cur.execute("SELECT COALESCE(SUM(expense_cost),0) AS total FROM ta_reports WHERE emp_code=%s", (code,))
    own_total = float(cur.fetchone()["total"])

    # Total reflecting the currently applied filters (matches what's on screen)
    filtered_total = sum(float(r["expense_cost"] or 0) for r in reports)

    cur.close(); conn.close()

    return render_template("ta_report.html",
        name=session["name"], success=success, lock_error=lock_error, reports=reports,
        record_count=len(reports), own_total=own_total, filtered_total=filtered_total,
        perms=session.get("perms", {}),
        filters={"status": f_status, "approval": f_approval, "from_d": f_from, "to_d": f_to, "search": f_search})

# ══════════════════════════════════════════
#  ROUTES — MANAGER: TA (TRAVEL EXPENSES) REPORT
# ══════════════════════════════════════════
@app.route("/manager/ta-reports", methods=["GET", "POST"])
def manager_ta_reports():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))

    if request.method == "POST":
        # Manager full edit: trip details (1-5) + status columns (6 & 7)
        edit_id      = request.form.get("edit_id", "").strip()
        travel_date  = request.form.get("travel_date", "").strip()
        from_place   = request.form.get("from_place", "").strip()
        to_place     = request.form.get("to_place", "").strip()
        travel_by    = request.form.get("travel_by", "").strip()
        description  = request.form.get("description", "").strip()
        expense_cost = request.form.get("expense_cost", "0").strip()
        payment_status  = request.form.get("payment_status", "Due")
        approval_status = request.form.get("approval_status", "Not Approved")

        try:
            expense_cost = float(expense_cost) if expense_cost else 0.0
        except ValueError:
            expense_cost = 0.0

        if edit_id:
            now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                UPDATE ta_reports
                SET travel_date=%s, from_place=%s, to_place=%s, travel_by=%s, description=%s,
                    expense_cost=%s, payment_status=%s, approval_status=%s,
                    last_edited=%s, last_edited_by=%s
                WHERE id=%s
            """, (travel_date, from_place, to_place, travel_by, description, expense_cost,
                  payment_status, approval_status, now, session["name"], edit_id))
            conn.commit(); cur.close(); conn.close()
        # Preserve whatever filters were active on the page the edit was submitted from
        return redirect(url_for("manager_ta_reports", **{k: v for k, v in request.args.items()}))

    emp    = request.args.get("emp", "")
    status = request.args.get("status", "")
    approval = request.args.get("approval", "")
    from_d = request.args.get("from_d", "")
    to_d   = request.args.get("to_d", "")
    search = request.args.get("search", "")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM ta_reports WHERE 1=1"
    params = []
    if emp:      query += " AND emp_name=%s";          params.append(emp)
    if status:   query += " AND payment_status=%s";    params.append(status)
    if approval: query += " AND approval_status=%s";   params.append(approval)
    if from_d:   query += " AND travel_date>=%s";      params.append(from_d)
    if to_d:     query += " AND travel_date<=%s";      params.append(to_d)
    if search:
        query += " AND (from_place ILIKE %s OR to_place ILIKE %s OR description ILIKE %s OR travel_by ILIKE %s)"
        s = f"%{search}%"; params += [s, s, s, s]
    query += " ORDER BY travel_date DESC, id DESC"
    cur.execute(query, params)
    reports = cur.fetchall()

    cur.execute("SELECT COALESCE(SUM(expense_cost),0) AS total FROM ta_reports")
    grand_total = float(cur.fetchone()["total"])
    cur.execute("SELECT COALESCE(SUM(expense_cost),0) AS total FROM ta_reports WHERE payment_status='Due'")
    due_total = float(cur.fetchone()["total"])
    cur.execute("SELECT COALESCE(SUM(expense_cost),0) AS total FROM ta_reports WHERE approval_status='Not Approved'")
    pending_approval_total = float(cur.fetchone()["total"])
    cur.execute("SELECT DISTINCT emp_name FROM ta_reports ORDER BY emp_name")
    emp_list = [r["emp_name"] for r in cur.fetchall()]
    cur.close(); conn.close()

    filtered_total = sum(float(r["expense_cost"] or 0) for r in reports)

    return render_template("manager_ta.html",
        reports=reports, emp_list=emp_list, record_count=len(reports),
        grand_total=grand_total, due_total=due_total, pending_approval_total=pending_approval_total,
        filtered_total=filtered_total,
        filters={"emp": emp, "status": status, "approval": approval, "from_d": from_d, "to_d": to_d, "search": search})

@app.route("/manager/ta-reports/bulk-update", methods=["POST"])
def manager_ta_bulk_update():
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))

    ids = request.form.getlist("selected_ids")
    ids = [int(i) for i in ids if i.isdigit()]
    action = request.form.get("bulk_action", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if ids and action:
        conn = get_db(); cur = conn.cursor()
        if action == "approve":
            cur.execute("""
                UPDATE ta_reports SET approval_status='Approved', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
        elif action == "unapprove":
            cur.execute("""
                UPDATE ta_reports SET approval_status='Not Approved', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
        elif action == "mark_paid":
            cur.execute("""
                UPDATE ta_reports SET payment_status='Paid', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
        elif action == "mark_due":
            cur.execute("""
                UPDATE ta_reports SET payment_status='Due', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
        conn.commit(); cur.close(); conn.close()

    return redirect(url_for("manager_ta_reports", **{k: v for k, v in request.args.items()}))

@app.route("/export/ta-reports")
def export_ta_reports():
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM ta_reports ORDER BY travel_date DESC, id DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID","Submitted","Employee","Travel Date","From","To","Travel By",
        "Description","Expense Cost","Payment Status","Approval Status","Last Edited"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["timestamp"], r["emp_name"], r["travel_date"], r["from_place"], r["to_place"],
            r["travel_by"], r["description"], r["expense_cost"], r["payment_status"],
            r["approval_status"], r["last_edited"]
        ])
    output.seek(0)
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=ta_reports.csv"}
    )


# ══════════════════════════════════════════
#  MINI CRM — MANAGER: CLIENTS
# ══════════════════════════════════════════
@app.route("/manager/clients")
def manager_clients():
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))

    f_search = request.args.get("search", "")
    f_stale  = request.args.get("stale", "")  # "30" = no visit in 30+ days

    conn = get_db(); cur = conn.cursor()
    query = """
        SELECT c.*,
               COUNT(DISTINCT v.id)              AS visit_count,
               MAX(v.visit_date)                  AS last_visit_date,
               MAX(v.next_followup_date)          AS latest_followup,
               COUNT(DISTINCT s.id) FILTER (WHERE LOWER(COALESCE(s.status,'pending')) <> 'complete') AS open_support_count
        FROM companies c
        LEFT JOIN sales_visits v ON v.company_id = c.id
        LEFT JOIN support_reports s ON s.company_id = c.id
        WHERE 1=1
    """
    params = []
    if f_search:
        query += " AND c.name ILIKE %s"
        params.append(f"%{f_search}%")
    query += " GROUP BY c.id ORDER BY c.name ASC"
    cur.execute(query, params)
    companies = cur.fetchall()
    cur.close(); conn.close()

    today = datetime.now().date()
    enriched = []
    for c in companies:
        days_since = None
        if c["last_visit_date"]:
            try:
                d = datetime.strptime(c["last_visit_date"], "%Y-%m-%d").date()
                days_since = (today - d).days
            except Exception:
                days_since = None
        row = dict(c)
        row["days_since"] = days_since
        if f_stale:
            try:
                threshold = int(f_stale)
                if days_since is None or days_since < threshold:
                    continue
            except ValueError:
                pass
        enriched.append(row)

    return render_template(
        "manager_clients.html",
        companies=enriched,
        record_count=len(enriched),
        total_companies=len(companies),
        filters={"search": f_search, "stale": f_stale},
    )


@app.route("/manager/clients/<int:company_id>")
def manager_client_detail(company_id):
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM companies WHERE id=%s", (company_id,))
    company = cur.fetchone()
    if not company:
        cur.close(); conn.close()
        return redirect(url_for("manager_clients"))

    cur.execute("""
        SELECT * FROM sales_visits
        WHERE company_id=%s
        ORDER BY visit_date DESC, timestamp DESC
    """, (company_id,))
    visits = cur.fetchall()

    cur.execute("""
        SELECT * FROM support_reports
        WHERE company_id=%s
        ORDER BY support_date DESC, timestamp DESC
    """, (company_id,))
    support_tickets = cur.fetchall()
    cur.close(); conn.close()

    outcome_counts = {}
    for v in visits:
        o = v["visit_outcome"] or "Unspecified"
        outcome_counts[o] = outcome_counts.get(o, 0) + 1

    salespeople = sorted({v["salesperson_name"] for v in visits if v["salesperson_name"]})
    support_status_counts = {}
    for t in support_tickets:
        s = t["status"] or "Pending"
        support_status_counts[s] = support_status_counts.get(s, 0) + 1
    open_support_count = sum(c for s, c in support_status_counts.items() if s.lower() != "complete")

    return render_template(
        "manager_client_detail.html",
        company=company,
        visits=visits,
        visit_count=len(visits),
        outcome_counts=outcome_counts,
        salespeople=salespeople,
        support_tickets=support_tickets,
        support_count=len(support_tickets),
        support_status_counts=support_status_counts,
        open_support_count=open_support_count,
    )


@app.route("/manager/clients/<int:company_id>/update", methods=["POST"])
def manager_client_update(company_id):
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE companies SET industry=%s, address=%s, phone=%s,
               primary_contact=%s, notes=%s
        WHERE id=%s
    """, (
        request.form.get("industry"),
        request.form.get("address"),
        request.form.get("phone"),
        request.form.get("primary_contact"),
        request.form.get("notes"),
        company_id,
    ))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_client_detail", company_id=company_id))


# ══════════════════════════════════════════
#  SUPPORT REPORT — DB INIT
# ══════════════════════════════════════════
def init_support_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_reports (
            id                  SERIAL PRIMARY KEY,
            timestamp           TEXT,
            emp_code            TEXT,
            emp_name            TEXT,
            support_date        TEXT,
            company             TEXT,
            contact_person      TEXT,
            address             TEXT,
            contact_number      TEXT,
            client_email        TEXT,
            dealer_type         TEXT,
            dealer_contact_number TEXT,
            dealer_contact_person TEXT,
            issue_description   TEXT,
            solution_description TEXT,
            remarks             TEXT,
            status              TEXT DEFAULT 'Pending'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_devices (
            id              SERIAL PRIMARY KEY,
            report_id       INTEGER NOT NULL REFERENCES support_reports(id) ON DELETE CASCADE,
            device_model    TEXT,
            device_serial   TEXT
        )
    """)
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_support_db()
        print("✅ Support report tables ready")
    except Exception as e:
        print(f"⚠️ Support DB init error: {e}")


# ══════════════════════════════════════════
#  SUPPORT — link to CRM companies
#  Runs after both companies and support_reports tables exist.
# ══════════════════════════════════════════
def link_support_to_companies():
    conn = get_db(); cur = conn.cursor()

    cur.execute("""
        ALTER TABLE support_reports
        ADD COLUMN IF NOT EXISTS company_id INTEGER
    """)
    conn.commit()

    # Backfill: create/find a company row for every distinct support
    # report's company name that doesn't have one yet.
    cur.execute("""
        SELECT DISTINCT company, emp_code
        FROM support_reports
        WHERE company IS NOT NULL AND company <> ''
          AND company_id IS NULL
    """)
    distinct_support_companies = cur.fetchall()
    for row in distinct_support_companies:
        cname = row["company"].strip()
        if not cname:
            continue
        cur.execute("SELECT id FROM companies WHERE LOWER(name)=LOWER(%s)", (cname,))
        existing = cur.fetchone()
        if existing:
            cid = existing["id"]
        else:
            cur.execute("""
                INSERT INTO companies (name, owner_code, created_at)
                VALUES (%s, %s, %s) RETURNING id
            """, (cname, row["emp_code"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            cid = cur.fetchone()["id"]
        cur.execute("""
            UPDATE support_reports SET company_id=%s
            WHERE LOWER(company)=LOWER(%s) AND company_id IS NULL
        """, (cid, cname))
    conn.commit()
    cur.close(); conn.close()

with app.app_context():
    try:
        link_support_to_companies()
        print("✅ Support reports linked to CRM companies")
    except Exception as e:
        print(f"⚠️ Support-CRM link error: {e}")


# ══════════════════════════════════════════
#  SUPPORT — TICKET ID + EDIT + REOPEN/FOLLOW-UPS
#  Adds:
#    - support_ticket_id   : permanent 6-digit ticket number per case
#    - last_edited         : timestamp of last edit by the employee
#    - support_followups   : every new issue/resolution logged against
#                             a ticket (the original submission is
#                             follow-up #1), so reopening a ticket for
#                             a returning client builds a full timeline
#                             instead of overwriting prior history.
# ══════════════════════════════════════════
def migrate_support_upgrade():
    conn = get_db(); cur = conn.cursor()
    cur.execute("ALTER TABLE support_reports ADD COLUMN IF NOT EXISTS support_ticket_id TEXT")
    cur.execute("ALTER TABLE support_reports ADD COLUMN IF NOT EXISTS last_edited TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_followups (
            id                    SERIAL PRIMARY KEY,
            report_id             INTEGER NOT NULL REFERENCES support_reports(id) ON DELETE CASCADE,
            timestamp             TEXT,
            emp_code              TEXT,
            emp_name              TEXT,
            issue_description     TEXT,
            solution_description  TEXT,
            remarks               TEXT,
            status                TEXT DEFAULT 'Pending'
        )
    """)
    conn.commit()

    # Backfill: give every pre-existing report a ticket ID, and seed a
    # follow-up #1 entry from its original issue/solution so old reports
    # show up correctly in the new timeline view.
    cur.execute("SELECT id FROM support_reports WHERE support_ticket_id IS NULL ORDER BY id")
    missing = cur.fetchall()
    existing_ids = set()
    cur.execute("SELECT support_ticket_id FROM support_reports WHERE support_ticket_id IS NOT NULL")
    for row in cur.fetchall():
        existing_ids.add(row["support_ticket_id"])

    for row in missing:
        new_tid = generate_ticket_id(existing_ids)
        existing_ids.add(new_tid)
        cur.execute("UPDATE support_reports SET support_ticket_id=%s WHERE id=%s", (new_tid, row["id"]))

    cur.execute("""
        SELECT r.id, r.timestamp, r.emp_code, r.emp_name, r.issue_description,
               r.solution_description, r.remarks, r.status
        FROM support_reports r
        WHERE NOT EXISTS (SELECT 1 FROM support_followups f WHERE f.report_id = r.id)
    """)
    for r in cur.fetchall():
        cur.execute("""
            INSERT INTO support_followups
            (report_id, timestamp, emp_code, emp_name, issue_description, solution_description, remarks, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (r["id"], r["timestamp"], r["emp_code"], r["emp_name"],
              r["issue_description"], r["solution_description"], r["remarks"], r["status"]))

    conn.commit(); cur.close(); conn.close()


def generate_ticket_id(existing_ids=None):
    """6-digit numeric support ticket ID, unique against existing_ids (or the DB if not supplied)."""
    if existing_ids is None:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT support_ticket_id FROM support_reports WHERE support_ticket_id IS NOT NULL")
        existing_ids = {row["support_ticket_id"] for row in cur.fetchall()}
        cur.close(); conn.close()
    while True:
        candidate = "".join(secrets.choice(string.digits) for _ in range(6))
        if candidate not in existing_ids:
            return candidate

with app.app_context():
    try:
        migrate_support_upgrade()
        print("✅ Support ticket-ID / edit / reopen upgrade ready")
    except Exception as e:
        print(f"⚠️ Support upgrade migration error: {e}")


# ══════════════════════════════════════════
#  SUPPORT — add can_support column to users
# ══════════════════════════════════════════
def migrate_support_permission():
    conn = get_db(); cur = conn.cursor()
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_support BOOLEAN DEFAULT TRUE")
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        migrate_support_permission()
        print("✅ Support permission column ready")
    except Exception as e:
        print(f"⚠️ Support permission migration error: {e}")


# patch refresh_employees to include can_support
_orig_refresh = refresh_employees
def refresh_employees():
    global EMPLOYEES, USERNAME_MAP
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT emp_code, name, username, password_hash, company,
               is_active, can_work_report, can_sales_visit, can_my_jobs, can_ta,
               COALESCE(can_support, TRUE) AS can_support
        FROM users WHERE is_active = TRUE
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    new_employees = {}
    new_username_map = {}
    for r in rows:
        new_employees[r["emp_code"]] = {
            "name": r["name"], "company": r["company"] or "",
            "username": r["username"], "password_hash": r["password_hash"],
            "can_work_report": r["can_work_report"], "can_sales_visit": r["can_sales_visit"],
            "can_my_jobs": r["can_my_jobs"], "can_ta": r["can_ta"],
            "can_support": r["can_support"],
        }
        new_username_map[r["username"]] = r["emp_code"]
    EMPLOYEES = new_employees
    USERNAME_MAP = new_username_map

with app.app_context():
    try:
        refresh_employees()
        print(f"✅ Employees refreshed with support perm ({len(EMPLOYEES)} users)")
    except Exception as e:
        print(f"⚠️ Refresh error: {e}")


# patch login to include can_support in session perms
_orig_index = app.view_functions.get("index")

# Override index to handle support perm redirect
@app.route("/", endpoint="index_override")
def index_override():
    if not logged_in(): return redirect(url_for("login"))
    if is_manager(): return redirect(url_for("dashboard"))
    if has_perm("work_report"): return redirect(url_for("employee_form"))
    if has_perm("sales_visit"): return redirect(url_for("sales_visit"))
    if has_perm("my_jobs"):     return redirect(url_for("my_jobs"))
    if has_perm("ta"):          return redirect(url_for("ta_report"))
    if has_perm("support"):     return redirect(url_for("support_report"))
    return redirect(url_for("no_access"))


# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE: SUPPORT REPORT
# ══════════════════════════════════════════
@app.route("/support-report", methods=["GET", "POST"])
def support_report():
    if not logged_in() or is_manager():
        return redirect(url_for("login"))
    if not has_perm("support"):
        return redirect(url_for("no_access"))

    code      = get_emp_code()
    success   = False
    lock_error  = False
    ticket_error = ""
    active_ticket = ""
    success_ticket_id = ""

    if request.method == "POST":
        action = request.form.get("form_action", "new")  # new | edit | reopen
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # collect device rows (multiple)
        models  = request.form.getlist("device_model[]")
        serials = request.form.getlist("device_serial[]")
        devices = [(m.strip(), s.strip()) for m, s in zip(models, serials) if m.strip() or s.strip()]

        conn = get_db(); cur = conn.cursor()

        if action == "edit":
            # Edit an existing report — only the owning employee can edit,
            # and only their own report (ownership is enforced in the WHERE
            # clause, not just by hiding the button in the UI).
            edit_id = request.form.get("edit_id", "").strip()
            edit_company_id = get_or_create_company(request.form.get("company"), owner_code=code)
            cur.execute("""
                UPDATE support_reports
                SET support_date=%s, company=%s, contact_person=%s, address=%s,
                    contact_number=%s, client_email=%s, dealer_type=%s,
                    dealer_contact_number=%s, dealer_contact_person=%s,
                    issue_description=%s, solution_description=%s, remarks=%s,
                    status=%s, last_edited=%s, company_id=%s
                WHERE id=%s AND emp_code=%s
                RETURNING id
            """, (
                request.form.get("support_date"),
                request.form.get("company"),
                request.form.get("contact_person"),
                request.form.get("address"),
                request.form.get("contact_number"),
                request.form.get("client_email"),
                request.form.get("dealer_type"),
                request.form.get("dealer_contact_number"),
                request.form.get("dealer_contact_person"),
                request.form.get("issue_description"),
                request.form.get("solution_description"),
                request.form.get("remarks"),
                request.form.get("status", "Pending"),
                now, edit_company_id, edit_id, code,
            ))
            row = cur.fetchone()
            if row is None:
                lock_error = True
            else:
                report_id = row["id"]
                # Devices on edit: replace the device list with what's on the form
                cur.execute("DELETE FROM support_devices WHERE report_id=%s", (report_id,))
                for model, serial in devices:
                    cur.execute("""
                        INSERT INTO support_devices (report_id, device_model, device_serial)
                        VALUES (%s, %s, %s)
                    """, (report_id, model, serial))
                # Keep follow-up #1 (the original case entry) in sync with the edit
                cur.execute("""
                    UPDATE support_followups
                    SET issue_description=%s, solution_description=%s, remarks=%s, status=%s
                    WHERE id = (SELECT id FROM support_followups WHERE report_id=%s ORDER BY id ASC LIMIT 1)
                """, (
                    request.form.get("issue_description"),
                    request.form.get("solution_description"),
                    request.form.get("remarks"),
                    request.form.get("status", "Pending"),
                    report_id,
                ))
                cur.execute("SELECT support_ticket_id FROM support_reports WHERE id=%s", (report_id,))
                success_ticket_id = cur.fetchone()["support_ticket_id"]
                success = True

        elif action == "reopen":
            # Reopen a previous ticket by its 6-digit Support ID: log a new
            # follow-up entry (new issue + new resolution) against the same
            # ticket, without touching the original report's history.
            ticket_id = request.form.get("ticket_id", "").strip()
            cur.execute("SELECT id FROM support_reports WHERE support_ticket_id=%s", (ticket_id,))
            target = cur.fetchone()
            if target is None:
                ticket_error = f"No support ticket found with ID {ticket_id}."
                active_ticket = ticket_id
            else:
                report_id = target["id"]
                new_status = request.form.get("status", "Pending")
                cur.execute("""
                    INSERT INTO support_followups
                    (report_id, timestamp, emp_code, emp_name, issue_description, solution_description, remarks, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    report_id, now, code, session["name"],
                    request.form.get("issue_description"),
                    request.form.get("solution_description"),
                    request.form.get("remarks"),
                    new_status,
                ))
                # Reflect the latest status + last-touched time on the parent ticket
                cur.execute("""
                    UPDATE support_reports SET status=%s, last_edited=%s WHERE id=%s
                """, (new_status, now, report_id))
                success = True
                success_ticket_id = ticket_id
                active_ticket = ticket_id

        else:
            # Brand-new support report — gets its own permanent 6-digit ticket ID
            new_ticket_id = generate_ticket_id()
            company_id = get_or_create_company(request.form.get("company"), owner_code=code)
            cur.execute("""
                INSERT INTO support_reports
                (timestamp, emp_code, emp_name, support_date, company, contact_person,
                 address, contact_number, client_email, dealer_type,
                 dealer_contact_number, dealer_contact_person,
                 issue_description, solution_description, remarks, status, support_ticket_id, company_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                now, code, session["name"],
                request.form.get("support_date"),
                request.form.get("company"),
                request.form.get("contact_person"),
                request.form.get("address"),
                request.form.get("contact_number"),
                request.form.get("client_email"),
                request.form.get("dealer_type"),
                request.form.get("dealer_contact_number"),
                request.form.get("dealer_contact_person"),
                request.form.get("issue_description"),
                request.form.get("solution_description"),
                request.form.get("remarks"),
                request.form.get("status", "Pending"),
                new_ticket_id,
                company_id,
            ))
            report_id = cur.fetchone()["id"]
            for model, serial in devices:
                cur.execute("""
                    INSERT INTO support_devices (report_id, device_model, device_serial)
                    VALUES (%s, %s, %s)
                """, (report_id, model, serial))
            cur.execute("""
                INSERT INTO support_followups
                (report_id, timestamp, emp_code, emp_name, issue_description, solution_description, remarks, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                report_id, now, code, session["name"],
                request.form.get("issue_description"),
                request.form.get("solution_description"),
                request.form.get("remarks"),
                request.form.get("status", "Pending"),
            ))
            success = True
            success_ticket_id = new_ticket_id

        conn.commit(); cur.close(); conn.close()

    # --- history filters ---
    f_status = request.args.get("status", "")
    f_from   = request.args.get("from_d", "")
    f_to     = request.args.get("to_d", "")
    f_search = request.args.get("search", "")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM support_reports WHERE emp_code=%s"
    params = [code]
    if f_status: query += " AND LOWER(status)=%s";  params.append(f_status.lower())
    if f_from:   query += " AND support_date>=%s";  params.append(f_from)
    if f_to:     query += " AND support_date<=%s";  params.append(f_to)
    if f_search:
        query += " AND (company ILIKE %s OR contact_person ILIKE %s OR issue_description ILIKE %s OR remarks ILIKE %s OR support_ticket_id ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s, s, s]
    query += " ORDER BY timestamp DESC"
    cur.execute(query, params)
    history = cur.fetchall()

    # fetch devices + follow-up timeline for each report
    history_with_devices = []
    for rep in history:
        cur.execute("SELECT * FROM support_devices WHERE report_id=%s ORDER BY id", (rep["id"],))
        devices = cur.fetchall()
        cur.execute("SELECT * FROM support_followups WHERE report_id=%s ORDER BY id ASC", (rep["id"],))
        followups = cur.fetchall()
        history_with_devices.append({"report": rep, "devices": devices, "followups": followups})

    cur.close(); conn.close()

    return render_template(
        "support_report.html",
        name=session["name"],
        success=success,
        lock_error=lock_error,
        ticket_error=ticket_error,
        active_ticket=active_ticket,
        success_ticket_id=success_ticket_id,
        history=history_with_devices,
        record_count=len(history_with_devices),
        perms=session.get("perms", {}),
        filters={"status": f_status, "from_d": f_from, "to_d": f_to, "search": f_search},
    )


# ══════════════════════════════════════════
#  API — LOOK UP A SUPPORT TICKET BY ITS 6-DIGIT ID
#  Used by the "Reopen Previous Ticket" panel so an employee can pull up
#  a ticket's client details + full timeline before adding a follow-up.
# ══════════════════════════════════════════
@app.route("/support-report/lookup/<ticket_id>")
def support_ticket_lookup(ticket_id):
    if not logged_in() or is_manager():
        return jsonify({"found": False, "error": "Not authorized"}), 403
    if not has_perm("support"):
        return jsonify({"found": False, "error": "Not authorized"}), 403

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM support_reports WHERE support_ticket_id=%s", (ticket_id.strip(),))
    rep = cur.fetchone()
    if rep is None:
        cur.close(); conn.close()
        return jsonify({"found": False})

    cur.execute("SELECT device_model, device_serial FROM support_devices WHERE report_id=%s ORDER BY id", (rep["id"],))
    devices = cur.fetchall()
    cur.execute("SELECT * FROM support_followups WHERE report_id=%s ORDER BY id ASC", (rep["id"],))
    followups = cur.fetchall()
    cur.close(); conn.close()

    return jsonify({
        "found": True,
        "report": dict(rep),
        "devices": [dict(d) for d in devices],
        "followups": [dict(f) for f in followups],
    })


# ══════════════════════════════════════════
#  ROUTES — MANAGER: SUPPORT REPORTS
# ══════════════════════════════════════════
@app.route("/manager/support-reports")
def manager_support_reports():
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))

    f_emp    = request.args.get("emp", "")
    f_status = request.args.get("status", "")
    f_from   = request.args.get("from_d", "")
    f_to     = request.args.get("to_d", "")
    f_search = request.args.get("search", "")
    f_dealer = request.args.get("dealer", "")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM support_reports WHERE 1=1"
    params = []
    if f_emp:    query += " AND emp_name=%s";             params.append(f_emp)
    if f_status: query += " AND LOWER(status)=%s";        params.append(f_status.lower())
    if f_from:   query += " AND support_date>=%s";        params.append(f_from)
    if f_to:     query += " AND support_date<=%s";        params.append(f_to)
    if f_dealer: query += " AND dealer_type=%s";          params.append(f_dealer)
    if f_search:
        query += " AND (company ILIKE %s OR contact_person ILIKE %s OR issue_description ILIKE %s OR remarks ILIKE %s OR support_ticket_id ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s, s, s]
    query += " ORDER BY timestamp DESC"
    cur.execute(query, params)
    reports = cur.fetchall()

    reports_with_devices = []
    for rep in reports:
        cur.execute("SELECT * FROM support_devices WHERE report_id=%s ORDER BY id", (rep["id"],))
        devices = cur.fetchall()
        cur.execute("SELECT * FROM support_followups WHERE report_id=%s ORDER BY id ASC", (rep["id"],))
        followups = cur.fetchall()
        reports_with_devices.append({"report": rep, "devices": devices, "followups": followups})

    cur.execute("SELECT COUNT(*) AS c FROM support_reports"); total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM support_reports WHERE LOWER(status)='complete'"); completed = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM support_reports WHERE LOWER(status)='pending'"); pending = cur.fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT COUNT(*) AS c FROM support_reports WHERE support_date=%s", (today,)); today_ct = cur.fetchone()["c"]
    cur.execute("SELECT DISTINCT emp_name FROM support_reports ORDER BY emp_name"); emp_list = [r["emp_name"] for r in cur.fetchall()]
    cur.close(); conn.close()

    return render_template(
        "manager_support.html",
        name=session["name"],
        reports=reports_with_devices,
        total=total, completed=completed, pending=pending, today_ct=today_ct,
        emp_list=emp_list,
        filters={"emp": f_emp, "status": f_status, "from_d": f_from, "to_d": f_to, "search": f_search, "dealer": f_dealer},
        record_count=len(reports_with_devices),
    )


# ══════════════════════════════════════════
#  EXPORT: SUPPORT REPORTS CSV
# ══════════════════════════════════════════
@app.route("/export/support-reports")
def export_support_reports():
    if not logged_in() or not is_manager():
        return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM support_reports ORDER BY timestamp DESC")
    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID","Support ID","Submitted","Employee","Support Date","Company","Contact Person",
        "Address","Contact Number","Client Email","Dealer Type",
        "Dealer Contact Number","Dealer Contact Person",
        "Issue Description","Solution Description","Remarks","Status","Last Edited"
    ])
    for r in rows:
        cur.execute("SELECT device_model, device_serial FROM support_devices WHERE report_id=%s ORDER BY id", (r["id"],))
        devices = cur.fetchall()
        device_str = "; ".join(f"{d['device_model']} ({d['device_serial']})" for d in devices)
        writer.writerow([
            r["id"], r.get("support_ticket_id", ""), r["timestamp"], r["emp_name"], r["support_date"],
            r["company"], r["contact_person"], r["address"], r["contact_number"],
            r["client_email"], r["dealer_type"], r["dealer_contact_number"],
            r["dealer_contact_person"], r["issue_description"],
            r["solution_description"], r["remarks"], r["status"], r.get("last_edited", "")
        ])
    cur.close(); conn.close()
    output.seek(0)
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=support_reports.csv"}
    )


# ══════════════════════════════════════════
#  PRODUCT CATALOGUE — DB INIT
# ══════════════════════════════════════════
def init_products_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS product_brands (
            id          SERIAL PRIMARY KEY,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            logo_url    TEXT,
            created_at  TEXT,
            created_by  TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id            SERIAL PRIMARY KEY,
            brand_id      INTEGER REFERENCES product_brands(id) ON DELETE CASCADE,
            model_code    TEXT NOT NULL,
            name          TEXT NOT NULL,
            category      TEXT,
            description   TEXT,
            unit          TEXT DEFAULT 'PCS',
            price         NUMERIC(12,2) DEFAULT 0,
            in_stock      INTEGER DEFAULT 0,
            min_stock     INTEGER DEFAULT 0,
            is_active     BOOLEAN DEFAULT TRUE,
            created_at    TEXT,
            created_by    TEXT,
            updated_at    TEXT,
            updated_by    TEXT
        )
    """)
    cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS min_stock INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_products BOOLEAN DEFAULT FALSE")
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_products_db()
        print("✅ Products table ready")
    except Exception as e:
        print(f"⚠️ Products DB init error: {e}")

# ── seed brands from PDF stock data ─────────────────────────────────────────
def seed_brands_from_stock():
    """Auto-seeds CONIXA, DAICHI, UNV, ZKTECO, Tplink if missing."""
    brands = [
        ("CONIXA", "Networking & CCTV accessories"),
        ("DAICHI", "CCTV cameras & storage"),
        ("UNV", "IP cameras & NVR systems"),
        ("ZKTECO", "Access control & biometric devices"),
        ("TPLINK", "Networking routers & switches"),
    ]
    conn = get_db(); cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for bname, bdesc in brands:
        cur.execute("INSERT INTO product_brands (name,description,created_at,created_by) VALUES (%s,%s,%s,'system') ON CONFLICT (name) DO NOTHING",
                    (bname, bdesc, now))
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        seed_brands_from_stock()
        print("✅ Default brands seeded")
    except Exception as e:
        print(f"⚠️ Brand seed error: {e}")


# ══════════════════════════════════════════
#  PRODUCT CATALOGUE — EMPLOYEE VIEW
# ══════════════════════════════════════════
@app.route("/products")
def products():
    if not logged_in(): return redirect(url_for("login"))
    if not has_perm("can_products"): return redirect(url_for("no_access"))

    q      = request.args.get("q", "").strip()
    brand  = request.args.get("brand", "")
    cat    = request.args.get("category", "")
    stock  = request.args.get("stock", "")   # "in","low","out"

    conn = get_db(); cur = conn.cursor()

    # brands for filter
    cur.execute("SELECT id, name FROM product_brands ORDER BY name")
    brands = cur.fetchall()

    # categories for filter
    cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL ORDER BY category")
    categories = [r["category"] for r in cur.fetchall()]

    sql = """
        SELECT p.*, b.name AS brand_name
        FROM products p
        JOIN product_brands b ON b.id = p.brand_id
        WHERE p.is_active = TRUE
    """
    params = []
    if q:
        params.append(f"%{q}%")
        sql += f" AND (p.name ILIKE %s OR p.model_code ILIKE %s OR p.description ILIKE %s)"
        params += [f"%{q}%", f"%{q}%"]
    if brand:
        params.append(int(brand))
        sql += f" AND p.brand_id = %s"
    if cat:
        params.append(cat)
        sql += f" AND p.category = %s"
    if stock == "in":
        sql += " AND p.in_stock > p.min_stock"
    elif stock == "low":
        sql += " AND p.in_stock > 0 AND p.in_stock <= p.min_stock"
    elif stock == "out":
        sql += " AND p.in_stock = 0"

    sql += " ORDER BY b.name, p.category, p.name"
    cur.execute(sql, params)
    all_products = cur.fetchall()
    cur.close(); conn.close()

    # group by brand
    from collections import defaultdict
    grouped = defaultdict(list)
    for p in all_products:
        grouped[p["brand_name"]].append(p)

    return render_template("products.html",
        name=session["name"], role=session.get("role","employee"),
        grouped=grouped, brands=brands, categories=categories,
        filters={"q": q, "brand": brand, "category": cat, "stock": stock},
        total=len(all_products), perms=session.get("perms", {}))


# ══════════════════════════════════════════
#  PRODUCT CATALOGUE — MANAGER (full CRUD)
# ══════════════════════════════════════════
@app.route("/manager/products")
def manager_products():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))

    q     = request.args.get("q", "").strip()
    brand = request.args.get("brand", "")
    cat   = request.args.get("category", "")
    stock = request.args.get("stock", "")

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, name, description FROM product_brands ORDER BY name")
    brands = cur.fetchall()

    cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL ORDER BY category")
    categories = [r["category"] for r in cur.fetchall()]

    sql = """
        SELECT p.*, b.name AS brand_name
        FROM products p
        JOIN product_brands b ON b.id = p.brand_id
        WHERE 1=1
    """
    params = []
    if q:
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
        sql += " AND (p.name ILIKE %s OR p.model_code ILIKE %s OR p.description ILIKE %s)"
    if brand:
        params.append(int(brand))
        sql += " AND p.brand_id = %s"
    if cat:
        params.append(cat)
        sql += " AND p.category = %s"
    if stock == "in":
        sql += " AND p.in_stock > p.min_stock"
    elif stock == "low":
        sql += " AND p.in_stock > 0 AND p.in_stock <= p.min_stock"
    elif stock == "out":
        sql += " AND p.in_stock = 0"

    sql += " ORDER BY b.name, p.category, p.name"
    cur.execute(sql, params)
    products_list = cur.fetchall()

    cur.execute("SELECT COUNT(*) AS c FROM products WHERE is_active=TRUE")
    active_ct = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM products WHERE in_stock=0 AND is_active=TRUE")
    out_ct = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM products WHERE in_stock>0 AND in_stock<=min_stock AND is_active=TRUE")
    low_ct = cur.fetchone()["c"]

    cur.close(); conn.close()

    from collections import defaultdict
    grouped = defaultdict(list)
    for p in products_list:
        grouped[p["brand_name"]].append(p)

    flash_msg  = request.args.get("flash", "")
    flash_type = request.args.get("flash_type", "success")

    return render_template("manager_products.html",
        name=session["name"],
        grouped=grouped, brands=brands, categories=categories,
        filters={"q": q, "brand": brand, "category": cat, "stock": stock},
        total=len(products_list), active_ct=active_ct, out_ct=out_ct, low_ct=low_ct,
        flash_msg=flash_msg, flash_type=flash_type)


@app.route("/manager/products/brand/add", methods=["POST"])
def add_brand():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    name = request.form.get("name","").strip().upper()
    desc = request.form.get("description","").strip()
    if not name:
        return redirect(url_for("manager_products", flash="Brand name required.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO product_brands (name,description,created_at,created_by) VALUES (%s,%s,%s,%s)",
                    (name, desc, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session["name"]))
        conn.commit()
        msg = f"Brand '{name}' added."
    except Exception:
        conn.rollback(); msg = "Brand already exists."
    cur.close(); conn.close()
    return redirect(url_for("manager_products", flash=msg, flash_type="success"))


@app.route("/manager/products/brand/<int:brand_id>/edit", methods=["POST"])
def edit_brand(brand_id):
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    name = request.form.get("name","").strip().upper()
    desc = request.form.get("description","").strip()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE product_brands SET name=%s, description=%s WHERE id=%s", (name, desc, brand_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash="Brand updated.", flash_type="success"))


@app.route("/manager/products/brand/<int:brand_id>/delete", methods=["POST"])
def delete_brand(brand_id):
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM product_brands WHERE id=%s", (brand_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash="Brand deleted.", flash_type="success"))


@app.route("/manager/products/add", methods=["POST"])
def add_product():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    f = request.form
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO products (brand_id, model_code, name, category, description, unit, price, in_stock, min_stock, is_active, created_at, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)
    """, (f["brand_id"], f["model_code"].strip(), f["name"].strip(),
          f.get("category","").strip(), f.get("description","").strip(),
          f.get("unit","PCS"), float(f.get("price",0) or 0),
          int(f.get("in_stock",0) or 0), int(f.get("min_stock",0) or 0),
          now, session["name"]))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash="Product added.", flash_type="success"))


@app.route("/manager/products/<int:product_id>/edit", methods=["POST"])
def edit_product(product_id):
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    f = request.form
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE products SET brand_id=%s, model_code=%s, name=%s, category=%s,
            description=%s, unit=%s, price=%s, in_stock=%s, min_stock=%s,
            updated_at=%s, updated_by=%s
        WHERE id=%s
    """, (f["brand_id"], f["model_code"].strip(), f["name"].strip(),
          f.get("category","").strip(), f.get("description","").strip(),
          f.get("unit","PCS"), float(f.get("price",0) or 0),
          int(f.get("in_stock",0) or 0), int(f.get("min_stock",0) or 0),
          now, session["name"], product_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash="Product updated.", flash_type="success"))


@app.route("/manager/products/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id):
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE id=%s", (product_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash="Product deleted.", flash_type="success"))


@app.route("/manager/products/<int:product_id>/toggle", methods=["POST"])
def toggle_product(product_id):
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE products SET is_active = NOT is_active WHERE id=%s", (product_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash="Product status toggled.", flash_type="success"))


@app.route("/manager/products/<int:product_id>/update-price", methods=["POST"])
def update_product_price(product_id):
    """Quick inline price edit straight from the product table row — only touches price,
    leaves every other field (name, stock, category, etc.) exactly as-is."""
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    raw_price = request.form.get("price", "").strip()
    try:
        price = float(raw_price) if raw_price else 0
    except ValueError:
        price = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE products SET price=%s, updated_at=%s, updated_by=%s WHERE id=%s",
                (price, now, session["name"], product_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash="Price updated.", flash_type="success"))


@app.route("/export/products")
def export_products():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT b.name AS brand, p.model_code, p.name, p.category, p.unit, p.price, p.in_stock, p.min_stock, p.is_active, p.created_at
        FROM products p JOIN product_brands b ON b.id=p.brand_id ORDER BY b.name, p.name
    """)
    rows = cur.fetchall(); cur.close(); conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Brand","Model Code","Product Name","Category","Unit","Price (₹)","In Stock","Min Stock","Active","Created At"])
    for r in rows:
        writer.writerow([r["brand"], r["model_code"], r["name"], r["category"], r["unit"],
                         r["price"], r["in_stock"], r["min_stock"], r["is_active"], r["created_at"]])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=products.csv"})


# ══════════════════════════════════════════
#  STOCK UPLOAD — DB INIT
# ══════════════════════════════════════════
def init_stock_upload_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_uploads (
            id           SERIAL PRIMARY KEY,
            filename     TEXT,
            report_date  TEXT,
            uploaded_at  TEXT,
            uploaded_by  TEXT,
            total_parsed INTEGER DEFAULT 0,
            total_matched INTEGER DEFAULT 0,
            total_new    INTEGER DEFAULT 0,
            notes        TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_upload_log (
            id          SERIAL PRIMARY KEY,
            upload_id   INTEGER REFERENCES stock_uploads(id) ON DELETE CASCADE,
            product_name TEXT,
            model_code   TEXT,
            brand        TEXT,
            qty_closing  INTEGER,
            unit         TEXT,
            action       TEXT,
            product_id   INTEGER
        )
    """)
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_stock_upload_db()
        print("✅ Stock upload tables ready")
    except Exception as e:
        print(f"⚠️ Stock upload DB init error: {e}")


# ══════════════════════════════════════════
#  PDF PARSER — extract closing balance
# ══════════════════════════════════════════
def parse_stock_pdf(file_bytes):
    """
    Parses Tally Godown Summary PDF.
    Returns list of dicts: {name, qty, unit, brand}
    Logic: closing balance is the LAST number+unit on a product line.
    Brand is detected when a line matches a known brand heading (no numbers).
    """
    import pdfplumber, re, io

    SKIP_LINES = {
        "particulars", "quantity", "opening", "inwards", "outwards", "closing",
        "balance", "carried over", "brought forward", "continued", "grand total",
        "page", "godown summary", "conneqtor technology", "patuli", "baishnabghata",
        "kolkata", "chandni chowk", "c-b1",
    }
    KNOWN_BRANDS = {"CONIXA", "DAICHI", "UNV", "ZKTECO", "TPLINK", "HIKVISION",
                    "CP PLUS", "ESSL", "REALTIME", "MATRIX"}

    # Pattern: number (with optional comma) followed by unit
    NUM_UNIT = re.compile(r'(\d[\d,]*)\s*(PCS|NOS|DRUMS|WIRES|SET|BOX|MTR|RLS|PKT)', re.IGNORECASE)

    results = []
    current_brand = "UNKNOWN"

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or len(line) < 3:
                    continue

                line_lower = line.lower()
                # Skip header/footer lines
                if any(skip in line_lower for skip in SKIP_LINES):
                    continue

                # Detect brand heading (line with no numbers, short, ALL CAPS or known)
                nums = NUM_UNIT.findall(line)
                upper_line = line.upper().split()[0] if line.split() else ""

                if not nums and line.isupper() and len(line.split()) <= 3:
                    current_brand = line.strip()
                    continue
                if not nums and upper_line in KNOWN_BRANDS:
                    current_brand = upper_line
                    continue

                # Lines with numbers — extract product name + closing balance
                if nums:
                    # Closing balance = last number+unit pair on the line
                    closing_qty_str, closing_unit = nums[-1]
                    closing_qty = int(closing_qty_str.replace(",", ""))

                    # Product name = everything before the first number
                    first_match = NUM_UNIT.search(line)
                    name = line[:first_match.start()].strip() if first_match else line

                    # Clean up name — remove trailing punctuation
                    name = re.sub(r'\s+', ' ', name).strip(' ,.-')

                    if name and closing_qty >= 0:
                        results.append({
                            "name": name,
                            "qty": closing_qty,
                            "unit": closing_unit.upper(),
                            "brand": current_brand,
                        })

    return results


def match_products_to_db(parsed_items):
    """
    Try to match each parsed item to products table.
    Match strategy:
    1. Exact model_code match (case-insensitive)
    2. Product name contains parsed name (fuzzy)
    Returns list with match info added.
    """
    import re
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.model_code, p.name, p.in_stock, p.price, b.name AS brand_name
        FROM products p JOIN product_brands b ON b.id = p.brand_id
    """)
    db_products = cur.fetchall()
    cur.close(); conn.close()

    def normalize(s):
        return re.sub(r'[\s\(\)\-/,\.]+', '', str(s)).upper()

    results = []
    for item in parsed_items:
        matched_product = None
        match_type = None
        norm_name = normalize(item["name"])

        for dbp in db_products:
            # Strategy 1: model code exact match
            if normalize(dbp["model_code"]) == norm_name:
                matched_product = dbp
                match_type = "model_code"
                break
            # Strategy 2: name contains or is contained
            norm_db = normalize(dbp["name"])
            if norm_name in norm_db or norm_db in norm_name:
                matched_product = dbp
                match_type = "name"
                break

        results.append({
            **item,
            "matched": matched_product is not None,
            "match_type": match_type,
            "product_id": matched_product["id"] if matched_product else None,
            "db_name": matched_product["name"] if matched_product else None,
            "current_stock": matched_product["in_stock"] if matched_product else None,
            "price": matched_product["price"] if matched_product else None,
        })

    # stable index so the preview/confirm steps can reference each row,
    # even the unmatched ("new product") ones that have no product_id yet
    for idx, r in enumerate(results):
        r["idx"] = idx

    return results


def get_or_create_brand(cur, brand_name, now):
    """Find a brand by name (case-insensitive); auto-create it if it doesn't exist yet.
    This is what lets a brand-new brand heading in the PDF (not just a new product)
    show up correctly without manager setup first."""
    name = (brand_name or "UNKNOWN").strip().upper() or "UNKNOWN"
    cur.execute("SELECT id FROM product_brands WHERE UPPER(name)=%s", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("""
        INSERT INTO product_brands (name, description, created_at, created_by)
        VALUES (%s, %s, %s, 'system')
        ON CONFLICT (name) DO NOTHING
        RETURNING id
    """, (name, "Auto-created from stock upload", now))
    row = cur.fetchone()
    if row:
        return row["id"]
    # another row won the race on ON CONFLICT — just re-fetch it
    cur.execute("SELECT id FROM product_brands WHERE UPPER(name)=%s", (name,))
    return cur.fetchone()["id"]


# ══════════════════════════════════════════
#  STOCK UPLOAD ROUTES
# ══════════════════════════════════════════
@app.route("/manager/stock-upload", methods=["GET"])
def stock_upload_page():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM stock_uploads ORDER BY uploaded_at DESC LIMIT 20")
    history = cur.fetchall()
    cur.close(); conn.close()

    flash_msg  = request.args.get("flash", "")
    flash_type = request.args.get("flash_type", "success")

    return render_template("stock_upload.html",
        name=session["name"], history=history,
        flash_msg=flash_msg, flash_type=flash_type)


@app.route("/manager/stock-upload/parse", methods=["POST"])
def stock_upload_parse():
    """Parse PDF and return preview — don't update DB yet."""
    if not logged_in() or not is_manager(): return redirect(url_for("login"))

    f = request.files.get("pdf_file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return redirect(url_for("stock_upload_page", flash="Please upload a valid PDF file.", flash_type="error"))

    file_bytes = f.read()
    filename   = f.filename

    try:
        parsed    = parse_stock_pdf(file_bytes)
        matched   = match_products_to_db(parsed)
    except Exception as e:
        return redirect(url_for("stock_upload_page", flash=f"PDF parse error: {e}", flash_type="error"))

    # Store in session for confirm step
    import json
    session["pending_stock"] = json.dumps(matched)
    session["pending_filename"] = filename

    matched_ct = sum(1 for m in matched if m["matched"])
    unmatched_ct = len(matched) - matched_ct

    return render_template("stock_upload_preview.html",
        name=session["name"],
        items=matched,
        filename=filename,
        total=len(matched),
        matched_ct=matched_ct,
        unmatched_ct=unmatched_ct,
    )


@app.route("/manager/stock-upload/confirm", methods=["POST"])
def stock_upload_confirm():
    """Apply the stock update to the database — updates matched products
    AND creates brand-new product rows for items that weren't found in the DB."""
    if not logged_in() or not is_manager(): return redirect(url_for("login"))

    import json, re
    raw = session.get("pending_stock")
    if not raw:
        return redirect(url_for("stock_upload_page", flash="Session expired. Please upload again.", flash_type="error"))

    items    = json.loads(raw)
    filename = session.get("pending_filename", "unknown.pdf")
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Only apply items that are checked (form checkboxes)
    apply_ids     = set(request.form.getlist("apply_ids"))      # matched products to update
    new_apply_idx = set(request.form.getlist("new_apply_idx"))  # unmatched rows to create as new products

    conn = get_db(); cur = conn.cursor()

    matched_items = [i for i in items if i["matched"] and str(i["product_id"]) in apply_ids]
    new_items     = [i for i in items if not i["matched"] and str(i["idx"]) in new_apply_idx]

    # Create upload record
    cur.execute("""
        INSERT INTO stock_uploads (filename, uploaded_at, uploaded_by, total_parsed, total_matched, total_new)
        VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
    """, (filename, now, session["name"], len(items), len(matched_items), len(new_items)))
    upload_id = cur.fetchone()["id"]

    # Apply stock updates to existing products
    for item in matched_items:
        cur.execute("UPDATE products SET in_stock=%s, updated_at=%s, updated_by=%s WHERE id=%s",
                    (item["qty"], now, f"PDF Upload: {filename}", item["product_id"]))
        cur.execute("""
            INSERT INTO stock_upload_log (upload_id, product_name, model_code, brand, qty_closing, unit, action, product_id)
            VALUES (%s,%s,%s,%s,%s,%s,'updated',%s)
        """, (upload_id, item["name"], item.get("model_code",""), item["brand"], item["qty"], item["unit"], item["product_id"]))

    # Auto-create brand-new products discovered in the PDF (no price/category yet —
    # manager fills those in later from the Products screen)
    for item in new_items:
        brand_id = get_or_create_brand(cur, item["brand"], now)

        slug = re.sub(r'[^A-Z0-9]+', '-', item["name"].upper()).strip('-')[:40] or "ITEM"
        model_code = f"{slug}-{upload_id}-{item['idx']}"

        cur.execute("""
            INSERT INTO products (brand_id, model_code, name, category, description, unit, price,
                                   in_stock, min_stock, is_active, created_at, created_by)
            VALUES (%s,%s,%s,NULL,%s,%s,0,%s,0,TRUE,%s,%s)
            RETURNING id
        """, (brand_id, model_code, item["name"].strip(),
              f"Auto-added from stock upload '{filename}'. Price & category not set yet — please review.",
              item["unit"], item["qty"], now, f"PDF Upload: {filename}"))
        new_id = cur.fetchone()["id"]

        cur.execute("""
            INSERT INTO stock_upload_log (upload_id, product_name, model_code, brand, qty_closing, unit, action, product_id)
            VALUES (%s,%s,%s,%s,%s,%s,'created',%s)
        """, (upload_id, item["name"], model_code, item["brand"], item["qty"], item["unit"], new_id))

    conn.commit(); cur.close(); conn.close()

    # Clear session
    session.pop("pending_stock", None)
    session.pop("pending_filename", None)

    msg = f"✅ {len(matched_items)} product(s) updated"
    if new_items:
        msg += f", {len(new_items)} new product(s) added"
    msg += f" from '{filename}'."

    return redirect(url_for("stock_upload_page", flash=msg, flash_type="success"))


@app.route("/manager/stock-upload/<int:upload_id>/log")
def stock_upload_log(upload_id):
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM stock_uploads WHERE id=%s", (upload_id,))
    upload = cur.fetchone()
    cur.execute("SELECT * FROM stock_upload_log WHERE upload_id=%s ORDER BY id", (upload_id,))
    logs = cur.fetchall()
    cur.close(); conn.close()
    return render_template("stock_upload_log.html", name=session["name"], upload=upload, logs=logs)
