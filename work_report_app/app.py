from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from datetime import datetime, timedelta
import os, csv, io, json
import requests as req

# ══════════════════════════════════════════
#  Google Sheets via gspread
# ══════════════════════════════════════════
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
app.secret_key = "workreport_v4_gsheets_2026"

# ══════════════════════════════════════════
#  GOOGLE SHEETS CONFIG
# ══════════════════════════════════════════
SPREADSHEET_ID  = os.environ.get("SPREADSHEET_ID", "YOUR_GOOGLE_SHEET_ID_HERE")
SHEET_NAME      = os.environ.get("SHEET_NAME", "reports")
JOBS_SHEET_NAME = os.environ.get("JOBS_SHEET_NAME", "assigned_jobs")
CREDS_JSON_ENV  = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
CREDS_FILE      = os.path.join(os.path.dirname(__file__), "credentials.json")

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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
    "1002": {"name": "Sayed Asif Ismail",   "company": "imaxsol",             "username": "asif",    "password": "1002123456", "supervisor": "Manager"},
    "1003": {"name": "Kartick Mondal",       "company": "imaxsol",             "username": "kartick", "password": "1003123456", "supervisor": "Sayed Asif Ismail"},
    "1004": {"name": "Sukumar Mondal",       "company": "imaxsol",             "username": "sukumar", "password": "1004123456", "supervisor": "Sayed Asif Ismail"},
    "1005": {"name": "Ashim Kayal",          "company": "imaxsol",             "username": "ashim",   "password": "1005123456", "supervisor": "Sayed Asif Ismail"},
    "1012": {"name": "Sujata Pahari",        "company": "imaxsol",             "username": "sujata",  "password": "1012123456", "supervisor": "Manager"},
    "2001": {"name": "Gourab Kumar Das",     "company": "imaxsol",             "username": "gourab",  "password": "2001123456", "supervisor": "Sayed Asif Ismail"},
    "1013": {"name": "Subrato Halder",       "company": "imaxsol",             "username": "subrato", "password": "1013123456", "supervisor": "Manager"},
    "2002": {"name": "Pritam Pal",           "company": "CONNEQTORTECHNOLOGY", "username": "pritam",  "password": "2002123456", "supervisor": "Manager"},
}
USERNAME_MAP = {v["username"]: k for k, v in EMPLOYEES.items()}
MANAGERS     = {"manager": {"password": "manager123", "name": "Manager"}}

# All possible supervisors (managers + senior employees)
SUPERVISORS = ["Manager"] + [e["name"] for e in EMPLOYEES.values()]

# Column headers
SHEET_HEADERS = ["id", "timestamp", "emp_code", "emp_name", "company",
                 "date", "work_type", "client_name", "location", "summary", "remarks", "status", "supervisor"]

JOBS_HEADERS = ["job_id", "created_at", "assigned_to_code", "assigned_to_name", "company",
                "supervisor", "job_title", "job_description", "location", "start_date", "end_date", "status"]

# ══════════════════════════════════════════
#  GOOGLE SHEETS HELPERS
# ══════════════════════════════════════════
def get_gspread_client():
    if CREDS_JSON_ENV:
        creds_info = json.loads(CREDS_JSON_ENV)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    elif os.path.exists(CREDS_FILE):
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    else:
        raise RuntimeError(
            "Google credentials not found. "
            "Set GOOGLE_CREDENTIALS_JSON env var or place credentials.json next to app.py"
        )
    return gspread.authorize(creds)

def get_worksheet():
    client = get_gspread_client()
    sh     = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=20)
    first = ws.row_values(1)
    if not first or first[0] != "id":
        ws.insert_row(SHEET_HEADERS, 1)
    return ws

def get_jobs_worksheet():
    client = get_gspread_client()
    sh     = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(JOBS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=JOBS_SHEET_NAME, rows=1000, cols=20)
    first = ws.row_values(1)
    if not first or first[0] != "job_id":
        ws.insert_row(JOBS_HEADERS, 1)
    return ws

def rows_to_dicts(ws):
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return []
    headers = all_values[0]
    return [dict(zip(headers, row)) for row in all_values[1:]]

def next_id(ws):
    records = rows_to_dicts(ws)
    if not records:
        return 1
    ids = []
    for r in records:
        try:
            ids.append(int(r.get("id", 0) or r.get("job_id", 0)))
        except ValueError:
            pass
    return (max(ids) + 1) if ids else 1

def insert_report(data: dict):
    ws  = get_worksheet()
    nid = next_id(ws)
    row = [
        nid,
        data["timestamp"],
        data["emp_code"],
        data["emp_name"],
        data["company"],
        data["date"],
        data["work_type"],
        data["client_name"],
        data["location"],
        data["summary"],
        data["remarks"],
        data["status"],
        data.get("supervisor", ""),
    ]
    ws.append_row(row, value_input_option="RAW")
    return nid

def insert_job(data: dict):
    ws  = get_jobs_worksheet()
    records = rows_to_dicts(ws)
    ids = []
    for r in records:
        try:
            ids.append(int(r.get("job_id", 0)))
        except ValueError:
            pass
    nid = (max(ids) + 1) if ids else 1
    row = [
        nid,
        data["created_at"],
        data["assigned_to_code"],
        data["assigned_to_name"],
        data["company"],
        data["supervisor"],
        data["job_title"],
        data["job_description"],
        data["location"],
        data["start_date"],
        data["end_date"],
        data["status"],
    ]
    ws.append_row(row, value_input_option="RAW")
    return nid

def get_all_reports(filters=None):
    ws      = get_worksheet()
    records = rows_to_dicts(ws)
    records = list(reversed(records))
    if not filters:
        return records
    result = []
    for r in records:
        if filters.get("emp")    and r.get("emp_name", "")  != filters["emp"]:    continue
        if filters.get("wtype")  and r.get("work_type", "") != filters["wtype"]:  continue
        if filters.get("status") and r.get("status", "").lower() != filters["status"].lower(): continue
        if filters.get("from_d") and r.get("date", "") < filters["from_d"]:       continue
        if filters.get("to_d")   and r.get("date", "") > filters["to_d"]:         continue
        if filters.get("search"):
            s = filters["search"].lower()
            haystack = " ".join([
                r.get("client_name",""), r.get("location",""),
                r.get("summary",""),    r.get("remarks","")
            ]).lower()
            if s not in haystack:
                continue
        result.append(r)
    return result

def get_all_jobs(filters=None):
    ws      = get_jobs_worksheet()
    records = rows_to_dicts(ws)
    records = list(reversed(records))
    if not filters:
        return records
    result = []
    for r in records:
        if filters.get("emp")    and r.get("assigned_to_name", "") != filters["emp"]:  continue
        if filters.get("status") and r.get("status", "").lower() != filters["status"].lower(): continue
        if filters.get("from_d") and r.get("start_date", "") < filters["from_d"]:      continue
        if filters.get("to_d")   and r.get("end_date", "")   > filters["to_d"]:        continue
        result.append(r)
    return result

def get_employee_reports(emp_code, limit=7):
    ws      = get_worksheet()
    records = rows_to_dicts(ws)
    filtered = [r for r in reversed(records) if r.get("emp_code") == emp_code]
    return filtered[:limit]

def get_employee_jobs(emp_code):
    ws      = get_jobs_worksheet()
    records = rows_to_dicts(ws)
    filtered = [r for r in reversed(records) if r.get("assigned_to_code") == emp_code]
    return filtered

def count_by_status(records, status_values):
    return sum(1 for r in records if r.get("status","").lower() in [s.lower() for s in status_values])

def update_job_status(job_id, new_status):
    ws = get_jobs_worksheet()
    all_values = ws.get_all_values()
    for i, row in enumerate(all_values):
        if i == 0:
            continue
        if str(row[0]) == str(job_id):
            # status is column index 11 (0-based) → sheet col 12
            ws.update_cell(i + 1, 12, new_status)
            return True
    return False

# ══════════════════════════════════════════
#  BIOTIME API
# ══════════════════════════════════════════
_biotime_token = None
_token_expiry  = None

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
        except (ValueError, IndexError):
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
    dates, cur = [], datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates

# ══════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════
def logged_in():    return "username" in session
def is_manager():   return session.get("role") == "manager"
def get_emp_code(): return session.get("emp_code")

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
                session.update({"username":username,"name":emp["name"],"role":"employee",
                                "emp_code":emp_code,"company":emp["company"],
                                "supervisor":emp.get("supervisor","")})
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
    error   = None
    emp_code = get_emp_code()
    emp_info = EMPLOYEES.get(emp_code, {})
    # Build supervisor list: their own supervisor + Manager always available
    emp_supervisor = emp_info.get("supervisor", "Manager")

    if request.method == "POST":
        try:
            insert_report({
                "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "emp_code":    emp_code,
                "emp_name":    session["name"],
                "company":     session.get("company",""),
                "date":        request.form.get("date"),
                "work_type":   request.form.get("work_type"),
                "client_name": request.form.get("client_name",""),
                "location":    request.form.get("location",""),
                "summary":     request.form.get("summary",""),
                "remarks":     request.form.get("remarks",""),
                "status":      request.form.get("status",""),
                "supervisor":  request.form.get("supervisor",""),
            })
            success = True
        except Exception as e:
            error = str(e)

    recent    = get_employee_reports(emp_code)
    assigned_jobs = []
    try:
        assigned_jobs = get_employee_jobs(emp_code)
    except:
        pass

    today     = datetime.now().strftime("%Y-%m-%d")
    att_today = []
    try:
        txns      = fetch_transactions(today, today)
        att_today = [a for a in process_attendance(txns, today, today) if a["emp_code"] == emp_code]
    except:
        pass

    return render_template("form.html",
        name=session["name"], success=success,
        recent=recent, att_today=att_today, error=error,
        assigned_jobs=assigned_jobs,
        emp_supervisor=emp_supervisor,
        supervisors=SUPERVISORS,
    )

# ══════════════════════════════════════════
#  ROUTES — MANAGER WORK REPORTS
# ══════════════════════════════════════════
@app.route("/manager")
def manager_view():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    filters = {
        "emp":    request.args.get("emp",""),
        "wtype":  request.args.get("wtype",""),
        "status": request.args.get("status",""),
        "from_d": request.args.get("from_d",""),
        "to_d":   request.args.get("to_d",""),
        "search": request.args.get("search",""),
    }
    error = None
    reports = []
    try:
        reports = get_all_reports(filters)
        all_rep = get_all_reports()
        today   = datetime.now().strftime("%Y-%m-%d")
        total     = len(all_rep)
        completed = count_by_status(all_rep, ["done","completed"])
        pending   = count_by_status(all_rep, ["pending"])
        partial   = count_by_status(all_rep, ["partial"])
        today_ct  = len({r["emp_code"] for r in all_rep if r.get("date") == today})
        emp_list  = sorted({r["emp_name"] for r in all_rep if r.get("emp_name")})
    except Exception as e:
        error     = str(e)
        total = completed = pending = partial = today_ct = 0
        emp_list = []

    return render_template("manager.html",
        reports=reports, emp_list=emp_list,
        total=total, completed=completed, pending=pending, partial=partial, today_ct=today_ct,
        filters=filters, record_count=len(reports), error=error,
        active_tab="reports",
    )

# ══════════════════════════════════════════
#  ROUTES — ASSIGN JOB (manager)
# ══════════════════════════════════════════
@app.route("/assign-job", methods=["GET","POST"])
def assign_job():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    success = False
    error   = None
    job_filters = {
        "emp":    request.args.get("emp",""),
        "status": request.args.get("status",""),
        "from_d": request.args.get("from_d",""),
        "to_d":   request.args.get("to_d",""),
    }

    if request.method == "POST":
        action = request.form.get("action","")
        if action == "assign":
            try:
                emp_code = request.form.get("assigned_to_code","")
                emp_info = EMPLOYEES.get(emp_code, {})
                insert_job({
                    "created_at":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "assigned_to_code":  emp_code,
                    "assigned_to_name":  emp_info.get("name",""),
                    "company":           emp_info.get("company",""),
                    "supervisor":        request.form.get("supervisor",""),
                    "job_title":         request.form.get("job_title",""),
                    "job_description":   request.form.get("job_description",""),
                    "location":          request.form.get("location",""),
                    "start_date":        request.form.get("start_date",""),
                    "end_date":          request.form.get("end_date",""),
                    "status":            request.form.get("job_status","assigned"),
                })
                success = True
            except Exception as e:
                error = str(e)
        elif action == "update_status":
            try:
                update_job_status(request.form.get("job_id"), request.form.get("new_status"))
            except Exception as e:
                error = str(e)
            return redirect(url_for("assign_job"))

    jobs = []
    try:
        jobs = get_all_jobs(job_filters)
    except Exception as e:
        if not error:
            error = str(e)

    emp_list_all = sorted(EMPLOYEES.items(), key=lambda x: x[1]["name"])
    job_emp_list = sorted({j["assigned_to_name"] for j in get_all_jobs() if j.get("assigned_to_name")} if not error else [])

    return render_template("assign_job.html",
        success=success, error=error,
        employees=EMPLOYEES, emp_list_all=emp_list_all,
        supervisors=SUPERVISORS,
        jobs=jobs, job_filters=job_filters,
        job_emp_list=job_emp_list,
        record_count=len(jobs),
        active_tab="assign",
    )

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
        view=view, error=error, record_count=len(filtered),
        active_tab="attendance",
    )

# ══════════════════════════════════════════
#  ROUTES — EXPORT CSV
# ══════════════════════════════════════════
@app.route("/export/reports")
def export_reports():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    rows   = get_all_reports()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Timestamp","Emp Code","Emp Name","Company","Date",
                     "Work Type","Client","Location","Summary","Remarks","Status","Supervisor"])
    for r in rows:
        writer.writerow([r.get("id"),r.get("timestamp"),r.get("emp_code"),r.get("emp_name"),
                         r.get("company"),r.get("date"),r.get("work_type"),r.get("client_name"),
                         r.get("location"),r.get("summary"),r.get("remarks"),r.get("status"),r.get("supervisor","")])
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

@app.route("/export/jobs")
def export_jobs():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    rows   = get_all_jobs()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Job ID","Created At","Assigned To","Company","Supervisor",
                     "Job Title","Description","Location","Start Date","End Date","Status"])
    for r in rows:
        writer.writerow([r.get("job_id"),r.get("created_at"),r.get("assigned_to_name"),
                         r.get("company"),r.get("supervisor"),r.get("job_title"),
                         r.get("job_description"),r.get("location"),
                         r.get("start_date"),r.get("end_date"),r.get("status")])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":"attachment;filename=assigned_jobs.csv"})

# ══════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════
if __name__ == "__main__":
    print("\n✅  Work Report System V4 (Google Sheets) is running!")
    print("📌  Open: http://127.0.0.1:5000")
    print("👤  Manager : manager / manager123")
    print("👤  Employee: subrato / 1013123456\n")
    app.run(debug=True)
