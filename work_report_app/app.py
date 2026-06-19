from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from datetime import datetime, timedelta
import os, csv, io
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
#  EMPLOYEE LIST
# ══════════════════════════════════════════
EMPLOYEES = {
    "1002": {"name": "Sayed Asif Ismail",   "company": "imaxsol",             "username": "asif",    "password": "1002123456"},
    "1003": {"name": "Kartick Mondal",       "company": "imaxsol",             "username": "kartick", "password": "1003123456"},
    "1004": {"name": "Sukumar Mondal",       "company": "imaxsol",             "username": "sukumar", "password": "1004123456"},
    "1005": {"name": "Ashim Kayal",          "company": "imaxsol",             "username": "ashim",   "password": "1005123456"},
    "1012": {"name": "Sujata Pahari",        "company": "imaxsol",             "username": "sujata",  "password": "1012123456"},
    "2001": {"name": "Gourab Kumar Das",     "company": "imaxsol",             "username": "gourab",  "password": "2001123456"},
    "1013": {"name": "Subrato Halder",       "company": "imaxsol",             "username": "subrato", "password": "1013123456"},
    "2002": {"name": "Pritam Pal",           "company": "CONNEQTORTECHNOLOGY", "username": "pritam",  "password": "2002123456"},
}
USERNAME_MAP = {v["username"]: k for k, v in EMPLOYEES.items()}
MANAGERS     = {"manager": {"password": "manager123", "name": "Manager"}}

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
    return redirect(url_for("manager_view") if is_manager() else url_for("employee_form"))

@app.route("/login", methods=["GET","POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username","").strip().lower()
        password = request.form.get("password","").strip()
        if username in MANAGERS and MANAGERS[username]["password"] == password:
            session.update({"username":username,"name":MANAGERS[username]["name"],"role":"manager"})
            return redirect(url_for("index"))
        emp_code = USERNAME_MAP.get(username)
        if emp_code:
            emp = EMPLOYEES[emp_code]
            if emp["password"] == password:
                session.update({"username":username,"name":emp["name"],"role":"employee","emp_code":emp_code,"company":emp["company"]})
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
    success = False
    if request.method == "POST":
        sup_code = request.form.get("supervisor_code", "")
        sup_name = EMPLOYEES.get(sup_code, {}).get("name", "")
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO reports
            (timestamp,emp_code,emp_name,company,date,work_type,client_name,location,summary,remarks,status,supervisor_code,supervisor_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            get_emp_code(), session["name"], session.get("company",""),
            request.form.get("date"), request.form.get("work_type"),
            request.form.get("client_name"), request.form.get("location"),
            request.form.get("summary"), request.form.get("remarks"),
            request.form.get("status"), sup_code, sup_name,
        ))
        conn.commit(); cur.close(); conn.close()
        success = True

    conn   = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM reports WHERE emp_code=%s ORDER BY timestamp DESC LIMIT 7", (get_emp_code(),))
    recent = cur.fetchall(); cur.close(); conn.close()

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

    return render_template("form.html", name=session["name"], success=success, recent=recent,
                            att_today=att_today, supervisor_choices=supervisor_choices)

# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE: VIEW ASSIGNED JOBS
# ══════════════════════════════════════════
@app.route("/my-jobs")
def my_jobs():
    if not logged_in() or is_manager(): return redirect(url_for("index"))
    code = get_emp_code()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT * FROM jobs
        WHERE (',' || emp_codes || ',') LIKE %s
           OR (',' || supervisor_codes || ',') LIKE %s
        ORDER BY created_at DESC
    """, (f"%,{code},%", f"%,{code},%"))
    jobs = cur.fetchall(); cur.close(); conn.close()

    # tag whether the viewer is on this job as employee, supervisor, or both
    enriched = []
    for j in jobs:
        is_emp = code in parse_codes(j.get("emp_codes"))
        is_sup = code in parse_codes(j.get("supervisor_codes"))
        role = "Both" if (is_emp and is_sup) else ("Supervisor" if is_sup else "Employee")
        enriched.append({**j, "viewer_role": role})

    return render_template("my_jobs.html", name=session["name"], jobs=enriched, record_count=len(enriched))

# ══════════════════════════════════════════
#  ROUTES — MANAGER WORK REPORTS
# ══════════════════════════════════════════
@app.route("/manager")
def manager_view():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    emp    = request.args.get("emp","")
    wtype  = request.args.get("wtype","")
    status = request.args.get("status","")
    from_d = request.args.get("from_d","")
    to_d   = request.args.get("to_d","")
    search = request.args.get("search","")

    conn   = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM reports WHERE 1=1"
    params = []
    if emp:    query += " AND emp_name=%s";                              params.append(emp)
    if wtype:  query += " AND work_type=%s";                             params.append(wtype)
    if status: query += " AND LOWER(status)=%s";                         params.append(status.lower())
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
    cur.execute("SELECT DISTINCT emp_name FROM reports ORDER BY emp_name");                  emp_list  = [r["emp_name"] for r in cur.fetchall()]
    cur.close(); conn.close()

    return render_template("manager.html",
        reports=reports, emp_list=emp_list,
        total=total, completed=completed, pending=pending, partial=partial, today_ct=today_ct,
        filters={"emp":emp,"wtype":wtype,"status":status,"from_d":from_d,"to_d":to_d,"search":search},
        record_count=len(reports)
    )

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

    conn  = get_db(); cur = conn.cursor()
    query = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if f_emp:    query += " AND (emp_names ILIKE %s OR supervisor_names ILIKE %s)"; params += [f"%{f_emp}%", f"%{f_emp}%"]
    if f_status: query += " AND status=%s";     params.append(f_status)
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    jobs = cur.fetchall()
    cur.close(); conn.close()

    employee_choices = [{"code": c, "name": i["name"], "company": i["company"]} for c, i in EMPLOYEES.items()]

    edited = request.args.get("edited") == "1"
    return render_template("assign_job.html",
        success=success, error=error, edited=edited,
        employee_choices=employee_choices,
        jobs=jobs, record_count=len(jobs),
        filters={"emp": f_emp, "status": f_status}
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
#  STARTUP
# ══════════════════════════════════════════
with app.app_context():
    try:
        init_db()
        print("✅ Database tables ready")
    except Exception as e:
        print(f"⚠️ DB init error: {e}")

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
#  ROUTES — EMPLOYEE: SALES VISIT REPORT
# ══════════════════════════════════════════
@app.route("/sales-visit", methods=["GET", "POST"])
def sales_visit():
    if not logged_in() or is_manager():
        return redirect(url_for("index"))

    success = False
    if request.method == "POST":
        sp_code = request.form.get("salesperson_code", "")
        sp_name = EMPLOYEES.get(sp_code, {}).get("name", "")
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO sales_visits
            (timestamp, visit_date, start_time, end_time, client_name,
             contact_number, address, type_of_visit, discussion_summary,
             products_interested, visit_outcome, next_followup_date,
             salesperson_code, salesperson_name, remark)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            request.form.get("visit_date"),
            request.form.get("start_time"),
            request.form.get("end_time"),
            request.form.get("client_name"),
            request.form.get("contact_number"),
            request.form.get("address"),
            request.form.get("type_of_visit"),
            request.form.get("discussion_summary"),
            request.form.get("products_interested"),
            request.form.get("visit_outcome"),
            request.form.get("next_followup_date"),
            sp_code, sp_name,
            request.form.get("remark"),
        ))
        conn.commit(); cur.close(); conn.close()
        success = True

    # Fetch this employee's own history
    code = get_emp_code()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT * FROM sales_visits
        WHERE salesperson_code=%s
        ORDER BY timestamp DESC
    """, (code,))
    history = cur.fetchall(); cur.close(); conn.close()

    salesperson_choices = [
        {"code": c, "name": i["name"]}
        for c, i in EMPLOYEES.items()
    ]

    return render_template(
        "sales_visit.html",
        name=session["name"],
        success=success,
        history=history,
        salesperson_choices=salesperson_choices,
        current_code=code,
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
