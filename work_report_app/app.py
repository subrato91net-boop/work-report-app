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
#  BIOTIME CLOUD 2.0 — MULTI-COMPANY CONFIG
# ══════════════════════════════════════════
PUNCH_START_HOUR = 0   # IST punches at 6 AM = 00:30 UTC — must start from 0
PUNCH_END_HOUR   = 23

# Legacy single-var kept for any old references
BIOTIME_URL = os.environ.get("BIOTIME_URL_IMAXSOL", "https://imaxsol.itimedev.minervaiot.com")

BIOTIME_COMPANIES = {
    "imaxsol": {
        "url":      os.environ.get("BIOTIME_URL_IMAXSOL",     "https://imaxsol.itimedev.minervaiot.com"),
        "email":    os.environ.get("BIOTIME_EMAIL_IMAXSOL",   "presales@conneqtortech.com"),
        "password": os.environ.get("BIOTIME_PASS_IMAXSOL",    "Y@jh_ro@562"),
        # biotime_company = slug sent to BioTime's own login API for this tenant
        "biotime_company": os.environ.get("BIOTIME_COMPANY_IMAXSOL", "imaxsol"),
        # company = YOUR internal label, must match EMPLOYEES[...]["company"]
        "company":  "imaxsol",
    },
    "conneqtortech": {
        "url":      os.environ.get("BIOTIME_URL_CONNEQTOR",     "https://conneqtortech.itimedev.minervaiot.com"),
        "email":    os.environ.get("BIOTIME_EMAIL_CONNEQTOR",   "presales@conneqtortech.com"),
        "password": os.environ.get("BIOTIME_PASS_CONNEQTOR",    "Y@jh_ro@562"),
        # biotime_company = slug sent to BioTime's own login API for this tenant
        "biotime_company": os.environ.get("BIOTIME_COMPANY_CONNEQTOR", "conneqtortech"),
        # company = YOUR internal label, must match EMPLOYEES[...]["company"]
        "company":  "conneqtortech",
    },

}

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
    "2002": {"name": "Pritam Pal",           "company": "conneqtortech",       "username": "pritam",  "password": "2002123456"},
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
            user_role       TEXT DEFAULT 'employee',
            can_work_report BOOLEAN DEFAULT TRUE,
            can_sales_visit BOOLEAN DEFAULT TRUE,
            can_my_jobs     BOOLEAN DEFAULT TRUE,
            can_ta          BOOLEAN DEFAULT TRUE,
            created_at      TEXT,
            created_by      TEXT
        )
    """)
    # Add user_role column if upgrading from older schema
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS user_role TEXT DEFAULT 'employee'")

    # Supervisor permission sets (what a supervisor is allowed to see/do)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS supervisor_permissions (
            emp_code            TEXT PRIMARY KEY REFERENCES users(emp_code) ON DELETE CASCADE,
            can_view_reports    BOOLEAN DEFAULT TRUE,
            can_approve_reports BOOLEAN DEFAULT FALSE,
            can_view_jobs       BOOLEAN DEFAULT TRUE,
            can_assign_jobs     BOOLEAN DEFAULT FALSE,
            can_view_ta         BOOLEAN DEFAULT TRUE,
            can_approve_ta      BOOLEAN DEFAULT FALSE,
            can_view_users      BOOLEAN DEFAULT FALSE,
            can_view_sales      BOOLEAN DEFAULT TRUE,
            can_view_support    BOOLEAN DEFAULT FALSE,
            can_view_clients    BOOLEAN DEFAULT FALSE,
            updated_at          TEXT,
            updated_by          TEXT
        )
    """)
    # In case upgrading from an even older partial schema
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_work_report BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_sales_visit BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_my_jobs BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_ta BOOLEAN DEFAULT TRUE")

    # Log of users that were auto-created from BioTime attendance data,
    # so the manager has somewhere to see the generated temp passwords
    # (instead of digging through hosting logs).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS biotime_sync_log (
            id            SERIAL PRIMARY KEY,
            emp_code      TEXT NOT NULL,
            name          TEXT,
            username      TEXT,
            temp_password TEXT,
            company       TEXT,
            created_at    TEXT
        )
    """)
    conn.commit()

    # Migration: earlier versions used "CONNEQTORTECHNOLOGY" as the company
    # label. Renamed to lowercase "conneqtortech" for consistency with the
    # BioTime URL/slug — update any existing rows so nothing falls out of sync.
    cur.execute("UPDATE users SET company = 'conneqtortech' WHERE company = 'CONNEQTORTECHNOLOGY'")
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

# refresh_employees() is defined once, further down in this file (it needs
# columns like can_support/can_products/can_challan/user_role). Do not add
# a second definition here — Python silently keeps only the last one.

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
    # Add dismissed_at column to job_edit_requests for notification dismissal
    cur.execute("ALTER TABLE job_edit_requests ADD COLUMN IF NOT EXISTS dismissed_at TEXT")
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
#  BIOTIME CLOUD 2.0 — JWT TOKEN CACHE
# ══════════════════════════════════════════
import threading as _threading
_bt_tokens   = {}
_bt_expiries = {}
_bt_lock     = _threading.Lock()

def get_biotime_token(company_key="imaxsol"):
    """Return a valid JWT for the given BioTime company, refreshing if needed."""
    with _bt_lock:
        if (_bt_tokens.get(company_key) and _bt_expiries.get(company_key)
                and datetime.now() < _bt_expiries[company_key]):
            return _bt_tokens[company_key]
    cfg = BIOTIME_COMPANIES.get(company_key)
    if not cfg:
        print(f"BioTime: unknown company key '{company_key}'")
        return None

    base_url = cfg["url"].rstrip("/")

    # ── Try both auth endpoints (Cloud 2.0 and older versions) ──
    auth_paths = ["/jwt-api-token-auth/", "/api-token-auth/"]
    # ── Try both payload formats (email or username) ──
    payloads = [
        {"company": cfg["biotime_company"], "email":    cfg["email"],    "password": cfg["password"]},
        {"company": cfg["biotime_company"], "username": cfg["email"],    "password": cfg["password"]},
    ]

    for path in auth_paths:
        for payload in payloads:
            try:
                res = req.post(f"{base_url}{path}", json=payload, timeout=15, verify=True)
                print(f"BioTime auth [{company_key}] {path} → HTTP {res.status_code}")
                if res.status_code == 200:
                    data  = res.json()
                    token = data.get("token") or data.get("access") or data.get("jwt")
                    if token:
                        with _bt_lock:
                            _bt_tokens[company_key]   = token
                            _bt_expiries[company_key] = datetime.now() + timedelta(hours=23)
                        print(f"BioTime auth [{company_key}] ✅ Token received")
                        return token
                    else:
                        print(f"BioTime auth [{company_key}] 200 OK but no token. Keys: {list(data.keys())}")
                elif res.status_code in (400, 401):
                    print(f"BioTime auth [{company_key}] credentials rejected: {res.text[:200]}")
            except Exception as e:
                print(f"BioTime auth [{company_key}] error on {path}: {e}")

    print(f"BioTime auth [{company_key}] ❌ All attempts failed")
    return None

# ══════════════════════════════════════════
#  BIOTIME CLOUD 2.0 — SESSION/COOKIE LOGIN
#  Workaround for a server-side bug on the vendor's
#  BioTime Cloud install: their JWT auth path crashes
#  with "module 'jwt' has no attribute 'ExpiredSignature'"
#  (PyJWT 2.x removed that old attribute name).
#  Logging in the same way the browser's "Log in" link
#  on the Django REST Framework page does (Django session
#  cookie) goes through a different code path and works.
# ══════════════════════════════════════════
import re as _re

_bt_web_sessions = {}
_bt_web_expiries = {}
_bt_web_lock     = _threading.Lock()

def _biotime_login_session(company_key):
    """Create a fresh authenticated requests.Session by logging in
    the same way the browser does (Django session auth), not JWT."""
    cfg = BIOTIME_COMPANIES.get(company_key)
    if not cfg:
        return None
    base_url = cfg["url"].rstrip("/")
    api_url  = f"{base_url}/iclock/api/transactions/"

    s = req.Session()
    try:
        # Step 1: hit the API page to discover the "Log in" link + get a csrftoken cookie
        r0 = s.get(api_url, timeout=15)
        login_url = f"{base_url}/api-auth/login/"   # standard DRF default convention
        m = _re.search(r'href="([^"]*\blogin[^"]*)"', r0.text, _re.IGNORECASE)
        if m:
            href = m.group(1)
            login_url = href if href.startswith("http") else f"{base_url}{href}"

        # Step 2: load the login form itself (this is what sets the real csrf cookie
        # and gives us the hidden csrfmiddlewaretoken value to echo back)
        r1  = s.get(login_url, params={"next": "/iclock/api/transactions/"}, timeout=15)
        csrf = s.cookies.get("csrftoken")
        m2 = _re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']', r1.text)
        if m2:
            csrf = m2.group(1)
        if not csrf:
            print(f"BioTime web-login [{company_key}] ❌ could not find CSRF token at {login_url}")
            return None

        # Step 3: submit the login form, exactly like the browser did in your screenshot
        payload = {
            "username": cfg["email"],
            "password": cfg["password"],
            "csrfmiddlewaretoken": csrf,
            "next": "/iclock/api/transactions/",
        }
        headers = {"Referer": r1.url}
        s.post(login_url, data=payload, headers=headers, timeout=15, allow_redirects=True)

        # Step 4: confirm it actually worked by calling the real endpoint
        check = s.get(api_url, params={"page_size": 1}, timeout=15)
        if check.status_code == 200:
            print(f"BioTime web-login [{company_key}] ✅ session login OK")
            return s
        print(f"BioTime web-login [{company_key}] ❌ HTTP {check.status_code}: {check.text[:200]}")
        return None
    except Exception as e:
        print(f"BioTime web-login [{company_key}] error: {e}")
        return None


def get_biotime_web_session(company_key="imaxsol", force_new=False):
    """Return a cached, logged-in requests.Session for this company, refreshing if needed."""
    with _bt_web_lock:
        cached = _bt_web_sessions.get(company_key)
        if (not force_new and cached and _bt_web_expiries.get(company_key)
                and datetime.now() < _bt_web_expiries[company_key]):
            return cached

    s = _biotime_login_session(company_key)
    if s:
        with _bt_web_lock:
            _bt_web_sessions[company_key]  = s
            _bt_web_expiries[company_key]  = datetime.now() + timedelta(hours=6)
    return s

# ══════════════════════════════════════════
#  BIOTIME — EMPLOYEE / PERSONNEL DIRECTORY
#  Pulls real names (and department, if present) from BioTime
#  using the working session auth. Used to fill in proper names
#  when a new employee shows up in attendance.
# ══════════════════════════════════════════
def get_biotime_employee_directory(company_key="imaxsol"):
    """Return {emp_code: {"name": ..., "department": ...}} from BioTime."""
    sess = get_biotime_web_session(company_key)
    if not sess:
        return {}
    cfg       = BIOTIME_COMPANIES[company_key]
    base_url  = cfg["url"].rstrip("/")
    directory = {}

    for emp_path in ["/personnel/api/employees/", "/hr/api/employees/",
                      "/iclock/api/employees/", "/att/api/employees/"]:
        url       = f"{base_url}{emp_path}"
        params    = {"page_size": 500}
        found_any = False
        try:
            while url:
                res = sess.get(url, params=params, timeout=20)
                if res.status_code != 200:
                    break
                data = res.json()
                rows = data.get("data", [])
                for e in rows:
                    code = str(e.get("emp_code") or e.get("emp_no") or e.get("employee_code") or "").strip()
                    if not code:
                        continue
                    first = (e.get("first_name") or "").strip()
                    last  = (e.get("last_name") or "").strip()
                    name  = (f"{first} {last}".strip() or e.get("nickname")
                              or e.get("name") or e.get("full_name") or "").strip()
                    dept  = e.get("department")
                    dept_name = dept.get("dept_name") if isinstance(dept, dict) else dept
                    directory[code] = {"name": name, "department": dept_name}
                    found_any = True
                url    = data.get("next")
                params = {}
        except Exception as ex:
            print(f"BioTime directory [{company_key}] {emp_path} error: {ex}")
            continue
        if found_any:
            print(f"BioTime directory [{company_key}] ✅ {len(directory)} employees via {emp_path}")
            break  # this endpoint exists and works on this server — stop probing the rest
    return directory

# ══════════════════════════════════════════
#  BIOTIME — FETCH TRANSACTIONS (one company, paginated)
# ══════════════════════════════════════════
def _fetch_from_company(company_key, date_from, date_to):
    cfg      = BIOTIME_COMPANIES[company_key]
    base_url = cfg["url"].rstrip("/")
    api_url  = f"{base_url}/iclock/api/transactions/"

    # ── BioTime Cloud 2.0 — CoreAPI schema confirmed ───────────────────────────
    # Schema: /iclock/api/transactions/ params: start_time, end_time, ordering
    # start_time / end_time filter on punch_time which is stored & filtered in IST.
    # No UTC conversion needed — just use the full IST day window.
    _start = f"{date_from} 00:00:00"
    _end   = f"{date_to} 23:59:59"

    # ── METHOD 1 (primary): session/cookie login — works around the server's
    #    broken JWT auth. Retries once with a forced fresh login if the
    #    cached session has expired server-side. ──
    for attempt in range(2):
        sess = get_biotime_web_session(company_key, force_new=(attempt == 1))
        if not sess:
            break
        all_data = []
        url      = api_url
        params   = {"start_time": _start, "end_time": _end, "ordering": "punch_time", "page_size": 500}
        retry_needed = False
        try:
            while url:
                res = sess.get(url, params=params, timeout=20)
                print(f"BioTime txn [{company_key}] web-session → HTTP {res.status_code}")
                if res.status_code == 200:
                    data   = res.json()
                    rows   = data.get("data", [])
                    all_data.extend(rows)
                    url    = data.get("next")
                    params = {}
                    if not url:
                        print(f"BioTime txn [{company_key}] ✅ {len(all_data)} records fetched (session)")
                        return all_data
                elif res.status_code in (401, 403):
                    print(f"BioTime txn [{company_key}] session expired/rejected, retrying with fresh login…")
                    retry_needed = True
                    break
                else:
                    print(f"BioTime txn [{company_key}] HTTP {res.status_code}: {res.text[:200]}")
                    return []
        except Exception as e:
            print(f"BioTime txn [{company_key}] session error: {e}")
            break
        if not retry_needed:
            break

    # ── METHOD 2 (fallback): old JWT bearer-token method. Currently broken on
    #    the vendor's server, but kept here in case they fix it later. ──
    token = get_biotime_token(company_key)
    if not token:
        return []
    for auth_prefix in ("JWT", "Token"):
        headers  = {"Authorization": f"{auth_prefix} {token}"}
        all_data = []
        url      = api_url
        params   = {
            "start_time": _start,
            "end_time":   _end,
            "ordering":   "punch_time",
            "page_size":  500,
        }
        try:
            while url:
                res = req.get(url, headers=headers, params=params, timeout=20)
                print(f"BioTime txn [{company_key}] {auth_prefix} → HTTP {res.status_code}")
                if res.status_code == 200:
                    data     = res.json()
                    rows     = data.get("data", [])
                    all_data.extend(rows)
                    url      = data.get("next")
                    params   = {}
                    if not url:
                        print(f"BioTime txn [{company_key}] ✅ {len(all_data)} records fetched (jwt)")
                        return all_data
                elif res.status_code == 401:
                    print(f"BioTime txn [{company_key}] 401 with {auth_prefix}, trying next prefix…")
                    break
                else:
                    print(f"BioTime txn [{company_key}] HTTP {res.status_code}: {res.text[:200]}")
                    return []
        except Exception as e:
            print(f"BioTime txn [{company_key}] error: {e}")
            return []
    return []

# ══════════════════════════════════════════
#  BIOTIME — FETCH ALL COMPANIES
# ══════════════════════════════════════════
# ══════════════════════════════════════════
#  BIOTIME — AUTO-CREATE USERS FOUND IN ATTENDANCE
#  Any emp_code that shows up in BioTime punches but doesn't
#  exist yet in your `users` table gets created automatically,
#  using the real name from BioTime's personnel API when available.
# ══════════════════════════════════════════
def auto_provision_employees_from_transactions(transactions):
    unknown = {}   # emp_code -> source company (internal label)
    for t in transactions:
        code = str(t.get("emp_code", "")).strip()
        if code and code not in EMPLOYEES:
            unknown.setdefault(code, t.get("_source_company") or "")
    if not unknown:
        return []

    # One directory fetch per company involved, not one per employee
    directories = {}
    for company_key, cfg in BIOTIME_COMPANIES.items():
        if cfg["company"] in unknown.values() and cfg["company"] not in directories:
            directories[cfg["company"]] = get_biotime_employee_directory(company_key)

    conn = get_db(); cur = conn.cursor()
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created = []
    try:
        for code, source_company in unknown.items():
            cur.execute("SELECT 1 FROM users WHERE emp_code=%s", (code,))
            if cur.fetchone():
                continue  # created a moment ago, e.g. by another request

            info = directories.get(source_company, {}).get(code, {})
            real_name = (info.get("name") or "").strip()
            name      = real_name or f"Employee {code}"

            base_username = "".join(ch for ch in (real_name.lower().replace(" ", "") if real_name else f"emp{code}") if ch.isalnum()) or f"emp{code}"
            username, suffix = base_username, 1
            while True:
                cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
                if not cur.fetchone():
                    break
                suffix  += 1
                username = f"{base_username}{suffix}"

            temp_password = generate_temp_password()
            cur.execute("""
                INSERT INTO users (emp_code, name, username, password_hash, company,
                                    is_active, can_work_report, can_sales_visit, can_my_jobs, can_ta,
                                    created_at, created_by)
                VALUES (%s,%s,%s,%s,%s, TRUE, TRUE, TRUE, TRUE, TRUE, %s, 'biotime-auto-sync')
                ON CONFLICT (emp_code) DO NOTHING
            """, (code, name, username, hash_password(temp_password), source_company, now))
            cur.execute("""
                INSERT INTO biotime_sync_log (emp_code, name, username, temp_password, company, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (code, name, username, temp_password, source_company, now))
            conn.commit()
            created.append(code)
            print(f"BioTime auto-sync: created user emp_code={code} name='{name}' username={username} company={source_company}")
    except Exception as e:
        conn.rollback()
        print(f"BioTime auto-sync error: {e}")
    finally:
        cur.close(); conn.close()

    if created:
        refresh_employees()
    return created

# ══════════════════════════════════════════
#  BIOTIME — FETCH ALL COMPANIES
# ══════════════════════════════════════════
def fetch_transactions(date_from, date_to):
    """Fetch transactions from ALL configured BioTime companies and merge."""
    all_txns = []
    for company_key, cfg in BIOTIME_COMPANIES.items():
        rows = _fetch_from_company(company_key, date_from, date_to)
        for r in rows:
            # Use the employee's registered company from EMPLOYEES dict.
            # Both BioTime tenants may return overlapping records; using the
            # emp's actual registered company ensures correct filtering/display.
            code = str(r.get("emp_code", "")).strip()
            if code in EMPLOYEES:
                r["_source_company"] = EMPLOYEES[code]["company"]
            else:
                r["_source_company"] = cfg["company"]
        all_txns.extend(rows)

    # Safety net: if two configured companies happen to return the same
    # underlying punch (e.g. a reseller/dealer login that surfaces the same
    # data as the client tenant), de-duplicate by BioTime's own record "id"
    # so nobody's attendance/hours get counted twice.
    seen_ids = set()
    deduped  = []
    for r in all_txns:
        rid = r.get("id")
        if rid is not None:
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
        deduped.append(r)
    all_txns = deduped

    try:
        auto_provision_employees_from_transactions(all_txns)
    except Exception as e:
        print(f"BioTime auto-sync hook error: {e}")

    return all_txns

# ══════════════════════════════════════════
#  BIOTIME — DEBUG ROUTE  /debug-biotime
#  Accessible by manager only — shows live
#  API test results so you can diagnose issues
#  without needing a terminal.
# ══════════════════════════════════════════
@app.route("/debug-biotime")
def debug_biotime():
    if not logged_in() or not is_manager():
        return redirect(url_for("index"))
    lines = ["<pre style='font-family:monospace;font-size:13px;padding:20px'>"]
    lines.append("<b>BioTime Cloud 2.0 — Live Diagnostic v2</b>\n")
    today = datetime.now().strftime("%Y-%m-%d")
    now_ist = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    lines.append(f"Server time : {now_ist}")

    # Only run for the first company key (both are same URL/creds)
    seen_urls = set()

    for company_key, cfg in BIOTIME_COMPANIES.items():
        base_url = cfg["url"].rstrip("/")
        if base_url in seen_urls:
            lines.append(f"\n{'='*50}")
            lines.append(f"Company key : {company_key}  [skipped — same URL as above]")
            continue
        seen_urls.add(base_url)

        lines.append(f"\n{'='*50}")
        lines.append(f"Company key : {company_key}")
        lines.append(f"URL         : {cfg['url']}")
        lines.append(f"Company slug: {cfg['biotime_company']}  (internal label: {cfg['company']})")

        # ── 0. Session/cookie login test (the new, working method) ───────────
        lines.append("\n--- 0. Session/cookie login test (workaround for server JWT bug) ---")
        try:
            web_sess = get_biotime_web_session(company_key, force_new=True)
            if web_sess:
                chk = web_sess.get(f"{base_url}/iclock/api/transactions/",
                                    params={"page_size": 3, "ordering": "-punch_time"}, timeout=15)
                lines.append(f"   ✅ Session login OK — HTTP {chk.status_code}")
                if chk.status_code == 200:
                    d = chk.json()
                    lines.append(f"   Total records visible: {d.get('count', '?')}")
                    for t in d.get("data", [])[:3]:
                        lines.append(f"   emp_code={t.get('emp_code')}  punch_time={t.get('punch_time')}")
            else:
                lines.append("   ❌ Session login FAILED — see server logs for details")
        except Exception as e:
            lines.append(f"   ❌ Exception: {e}")

        # ── 1. Auth (JWT — currently broken on vendor's server, kept for reference) ──
        lines.append("\n--- 1. Auth test (legacy JWT path) ---")
        token = get_biotime_token(company_key)
        if not token:
            lines.append("❌ Auth FAILED — check URL/email/password")
            continue
        lines.append(f"✅ Token: {token[:40]}...")

        headers_jwt   = {"Authorization": f"JWT {token}"}
        headers_token = {"Authorization": f"Token {token}"}

        # ── 2. Raw no-filter fetch (last 5 records) ───────────────────────────
        lines.append("\n--- 2. Raw fetch — last 5 records (no date filter) ---")
        try:
            r = req.get(f"{base_url}/iclock/api/transactions/",
                        headers=headers_jwt,
                        params={"page_size": 5, "ordering": "-punch_time"},
                        timeout=20)
            lines.append(f"   HTTP {r.status_code}  (JWT prefix)")
            if r.status_code == 200:
                data = r.json()
                lines.append(f"   Total records on server: {data.get('count', '?')}")
                for t in data.get("data", []):
                    lines.append(f"   emp_code={t.get('emp_code')}  punch_time={t.get('punch_time')}  punch_state={t.get('punch_state')}")
            elif r.status_code == 401:
                # Try Token prefix
                r2 = req.get(f"{base_url}/iclock/api/transactions/",
                             headers=headers_token,
                             params={"page_size": 5, "ordering": "-punch_time"},
                             timeout=20)
                lines.append(f"   HTTP {r2.status_code}  (Token prefix)")
                if r2.status_code == 200:
                    data = r2.json()
                    lines.append(f"   Total records on server: {data.get('count', '?')}")
                    for t in data.get("data", []):
                        lines.append(f"   emp_code={t.get('emp_code')}  punch_time={t.get('punch_time')}  punch_state={t.get('punch_state')}")
                else:
                    lines.append(f"   ❌ Both JWT and Token prefix failed. Body: {r2.text[:300]}")
            else:
                lines.append(f"   ❌ Unexpected HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:
            lines.append(f"   ❌ Exception: {e}")

        # ── 3. Today filter test ──────────────────────────────────────────────
        lines.append(f"\n--- 3. Today filter: {today} 00:00:00 → {today} 23:59:59 ---")
        try:
            r = req.get(f"{base_url}/iclock/api/transactions/",
                        headers=headers_jwt,
                        params={"start_time": f"{today} 00:00:00",
                                "end_time":   f"{today} 23:59:59",
                                "ordering":   "punch_time",
                                "page_size":  100},
                        timeout=20)
            lines.append(f"   HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                count = data.get("count", 0)
                rows  = data.get("data", [])
                lines.append(f"   count={count}  rows_this_page={len(rows)}")
                for t in rows[:10]:
                    lines.append(f"   emp_code={t.get('emp_code')}  punch_time={t.get('punch_time')}  upload_time={t.get('upload_time')}")
                if count == 0:
                    lines.append("   ⚠️  No punches recorded for today yet — employees haven't punched in/out today")
            else:
                lines.append(f"   ❌ HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:
            lines.append(f"   ❌ Exception: {e}")

        # ── 4. Yesterday filter test ──────────────────────────────────────────
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        lines.append(f"\n--- 4. Yesterday filter: {yesterday} 00:00:00 → {yesterday} 23:59:59 ---")
        try:
            r = req.get(f"{base_url}/iclock/api/transactions/",
                        headers=headers_jwt,
                        params={"start_time": f"{yesterday} 00:00:00",
                                "end_time":   f"{yesterday} 23:59:59",
                                "ordering":   "punch_time",
                                "page_size":  100},
                        timeout=20)
            lines.append(f"   HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                count = data.get("count", 0)
                rows  = data.get("data", [])
                lines.append(f"   count={count}  rows_this_page={len(rows)}")
                for t in rows[:10]:
                    lines.append(f"   emp_code={t.get('emp_code')}  punch_time={t.get('punch_time')}  upload_time={t.get('upload_time')}")
            else:
                lines.append(f"   ❌ HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:
            lines.append(f"   ❌ Exception: {e}")

        # ── 5. emp_code matching ──────────────────────────────────────────────
        lines.append("\n--- 5. EMPLOYEES dict emp_codes ---")
        app_codes = [k for k, v in EMPLOYEES.items() if v.get("company") in (company_key, cfg["company"])]
        lines.append(f"   Your app emp_codes: {app_codes}")

        # ── 6. Employee endpoints (via session — the working auth method) ─────
        lines.append("\n--- 6. Employee endpoint probe (session auth) ---")
        web_sess_probe = get_biotime_web_session(company_key)
        if not web_sess_probe:
            lines.append("   ❌ No working session — see Step 0 above")
        else:
            for emp_path in ["/personnel/api/employees/", "/hr/api/employees/",
                             "/iclock/api/employees/", "/att/api/employees/"]:
                try:
                    r = web_sess_probe.get(f"{base_url}{emp_path}",
                                            params={"page_size": 5}, timeout=10)
                    lines.append(f"   {emp_path}  → HTTP {r.status_code}")
                    if r.status_code == 200:
                        d = r.json()
                        lines.append(f"      count={d.get('count','?')}  keys={list(d.keys())}")
                        for e in d.get("data", [])[:3]:
                            lines.append(f"      record fields: {list(e.keys())}")
                            ec = e.get("emp_code") or e.get("emp_no") or e.get("employee_code", "?")
                            lines.append(f"      emp_code={ec}  sample={ {k: e[k] for k in list(e.keys())[:8]} }")
                except Exception as ex:
                    lines.append(f"   {emp_path}  → error: {ex}")

    lines.append('\n<a href="/biotime-sync-log">→ View auto-created users &amp; temp passwords</a>')
    lines.append("\n</pre>")
    return "\n".join(lines)

# ══════════════════════════════════════════
#  VIEW: USERS AUTO-CREATED FROM BIOTIME
#  Shows the generated temp password ONCE per
#  user so the manager can hand it over — no
#  need to dig through hosting logs.
# ══════════════════════════════════════════
@app.route("/biotime-sync-log")
def biotime_sync_log_view():
    if not logged_in() or not is_manager():
        return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM biotime_sync_log ORDER BY id DESC LIMIT 200")
    rows = cur.fetchall()
    cur.close(); conn.close()

    html = ["<div style='font-family:sans-serif;padding:20px'>",
            "<h2>Users auto-created from BioTime attendance</h2>",
            "<p>These were created automatically because their emp_code appeared in BioTime "
            "punches but had no account yet. Share the temp password with the employee, "
            "then ask them to log in and you can change it from Manage Users.</p>",
            "<table border='1' cellpadding='6' style='border-collapse:collapse'>",
            "<tr><th>Created</th><th>Emp Code</th><th>Name</th><th>Username</th>"
            "<th>Temp Password</th><th>Company</th></tr>"]
    for r in rows:
        html.append(
            f"<tr><td>{r['created_at']}</td><td>{r['emp_code']}</td><td>{r['name']}</td>"
            f"<td>{r['username']}</td><td>{r['temp_password']}</td><td>{r['company']}</td></tr>"
        )
    html.append("</table></div>")
    return "\n".join(html)

# ══════════════════════════════════════════
#  PROCESS TRANSACTIONS → ATTENDANCE
# ══════════════════════════════════════════
def process_attendance(transactions, date_from, date_to):
    from collections import defaultdict
    punch_map  = defaultdict(list)
    valid_dates = set(get_date_range(date_from, date_to))
    for t in transactions:
        emp_code   = str(t.get("emp_code", ""))
        punch_time = t.get("punch_time", "")
        if not emp_code or not punch_time or emp_code not in EMPLOYEES:
            continue
        try:
            # punch_time is already IST — no timezone conversion needed.
            # We now fetch a wider UTC window so we just filter by IST date here.
            dt       = datetime.strptime(punch_time[:19], "%Y-%m-%d %H:%M:%S")
            date_str = dt.strftime("%Y-%m-%d")
            if date_str in valid_dates:
                punch_map[(emp_code, date_str)].append(dt)
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
def logged_in():     return "username" in session
def is_manager():    return session.get("role") == "manager"   # super admin
def is_supervisor(): return session.get("role") == "supervisor"
def get_emp_code():  return session.get("emp_code")

def has_perm(perm_key):
    """Super admin always passes. Supervisors & employees are gated by their
    session-cached permission flags, set at login time."""
    if is_manager():
        return True
    return session.get("perms", {}).get(perm_key, True)

def has_sup_perm(perm_key):
    """Check supervisor-level permissions (stored in session['sup_perms'])."""
    if is_manager():
        return True
    if is_supervisor():
        return session.get("sup_perms", {}).get(perm_key, False)
    return False

def get_supervisor_perms(emp_code):
    """Load supervisor permissions from DB for given emp_code."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM supervisor_permissions WHERE emp_code=%s", (emp_code,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return {
            "can_view_reports": True,  "can_approve_reports": False,
            "can_view_jobs": True,     "can_assign_jobs": False,
            "can_view_ta": True,       "can_approve_ta": False,
            "can_view_users": False,   "can_view_sales": True,
            "can_view_support": False, "can_view_clients": False,
            "can_add_employees": False,
        }
    return dict(row)

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
    if is_manager():    return redirect(url_for("dashboard"))
    if is_supervisor(): return redirect(url_for("supervisor_dashboard"))
    # Land the employee on the first feature they actually have access to
    if has_perm("work_report"): return redirect(url_for("employee_form"))
    if has_perm("sales_visit"): return redirect(url_for("sales_visit"))
    if has_perm("my_jobs"):     return redirect(url_for("my_jobs"))
    if has_perm("ta"):          return redirect(url_for("ta_report"))
    if has_perm("support"):     return redirect(url_for("support_report"))
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
        # ── Check DB admin accounts first ──
        admin_row = get_admin_by_username(username)
        if admin_row and admin_row["is_active"] and verify_password(password, admin_row["password_hash"]):
            session.update({"username": username, "name": admin_row["name"], "role": "manager",
                            "admin_id": admin_row["id"], "is_super": admin_row.get("is_super", False)})
            conn2 = get_db(); cur2 = conn2.cursor()
            cur2.execute("UPDATE admin_accounts SET last_login=%s WHERE id=%s",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), admin_row["id"]))
            conn2.commit(); cur2.close(); conn2.close()
            return redirect(url_for("index"))
        # ── Legacy fallback (MANAGERS dict) ──
        if username in MANAGERS and MANAGERS[username]["password"] == password:
            session.update({"username":username,"name":MANAGERS[username]["name"],"role":"manager"})
            return redirect(url_for("index"))
        refresh_employees()
        emp_code = USERNAME_MAP.get(username)
        if emp_code:
            emp = EMPLOYEES[emp_code]
            if verify_password(password, emp["password_hash"]):
                user_role = emp.get("user_role", "employee")
                base_session = {
                    "username": username, "name": emp["name"], "role": user_role,
                    "emp_code": emp_code, "company": emp["company"],
                    "perms": {
                        "work_report": emp["can_work_report"],
                        "sales_visit": emp["can_sales_visit"],
                        "my_jobs":     emp["can_my_jobs"],
                        "ta":          emp["can_ta"],
                        "support":     emp.get("can_support", True),
                        "can_products": emp.get("can_products", False),
                        "can_challan":  emp.get("can_challan", False),
                    },
                }
                if user_role == "supervisor":
                    base_session["sup_perms"] = get_supervisor_perms(emp_code)
                session.update(base_session)
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
        if sup_code == "NA":
            sup_code = ""
        sup_name = EMPLOYEES.get(sup_code, {}).get("name", "")
        new_status = request.form.get("status")
        # A Completed submission is itself the review request: it locks immediately.
        # Pending/Partial stay editable drafts. Resubmitting a Rejected report as
        # Completed sends it back into the review queue.
        new_review_status = "Awaiting Review" if (new_status or "").lower() in ("done", "completed") else "Draft"
        edit_id = request.form.get("edit_id", "").strip()

        conn = get_db(); cur = conn.cursor()
        if edit_id:
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
                conn.commit()
                report_custom_fields, _ = get_form_config("report")
                save_custom_field_values("report", int(edit_id), report_custom_fields, request.form)
        else:
            cur.execute("""
                INSERT INTO reports
                (timestamp,emp_code,emp_name,company,date,work_type,client_name,location,summary,remarks,status,supervisor_code,supervisor_name,review_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                get_emp_code(), session["name"], session.get("company",""),
                request.form.get("date"), request.form.get("work_type"),
                request.form.get("client_name"), request.form.get("location"),
                request.form.get("summary"), request.form.get("remarks"),
                new_status, sup_code, sup_name, new_review_status,
            ))
            new_id = cur.fetchone()["id"]
            success = True
            conn.commit()
            report_custom_fields, _ = get_form_config("report")
            save_custom_field_values("report", new_id, report_custom_fields, request.form)
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

    report_custom_fields, report_visibility = get_form_config("report")
    report_ids = [r["id"] for r in recent]
    report_custom_values = load_custom_field_values("report", report_ids)

    return render_template("form.html", name=session["name"], success=success, lock_error=lock_error, recent=recent,
                            att_today=att_today, supervisor_choices=supervisor_choices,
                            record_count=len(recent), perms=session.get("perms", {}),
                            role=session.get("role", "employee"), sup_perms=session.get("sup_perms", {}),
                            custom_fields=report_custom_fields,
                            visibility=report_visibility,
                            custom_values=report_custom_values,
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

    # recent reviewed results to show notification banners (only undismissed)
    cur.execute("""
        SELECT jer.*, j.job_title FROM job_edit_requests jer
        JOIN jobs j ON j.id=jer.job_id
        WHERE jer.submitted_code=%s AND jer.review_status IN ('Finalized','Declined')
          AND jer.dismissed_at IS NULL
        ORDER BY jer.reviewed_at DESC LIMIT 10
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
        user_role=session.get("role", "employee"), sup_perms=session.get("sup_perms", {}),
        filters={"status": f_status, "search": f_search, "from_d": f_from, "to_d": f_to},
    )

# ── Dismiss a job-edit-request notification ──────────────────────────────────
@app.route("/my-jobs/dismiss-notification/<int:req_id>", methods=["POST"])
def dismiss_job_notification(req_id):
    if not logged_in(): return ("", 403)
    code = session.get("code")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE job_edit_requests
        SET dismissed_at = NOW()::TEXT
        WHERE id=%s AND submitted_code=%s AND review_status IN ('Finalized','Declined')
    """, (req_id, code))
    conn.commit(); cur.close(); conn.close()
    return ("", 204)

# ── Dismiss ALL job-edit-request notifications at once ───────────────────────
@app.route("/my-jobs/dismiss-all-notifications", methods=["POST"])
def dismiss_all_job_notifications():
    if not logged_in(): return ("", 403)
    code = session.get("code")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE job_edit_requests
        SET dismissed_at = NOW()::TEXT
        WHERE submitted_code=%s AND review_status IN ('Finalized','Declined')
          AND dismissed_at IS NULL
    """, (code,))
    conn.commit(); cur.close(); conn.close()
    return ("", 204)


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

    report_custom_fields, report_visibility = get_form_config("report")
    report_ids = [r["id"] for r in reports]
    report_custom_values = load_custom_field_values("report", report_ids)

    return render_template("manager.html",
        reports=reports, emp_list=emp_list,
        total=total, completed=completed, pending=pending, partial=partial, today_ct=today_ct,
        awaiting_review=awaiting_review,
        filters={"emp":emp,"wtype":wtype,"status":status,"review":review,"from_d":from_d,"to_d":to_d,"search":search},
        record_count=len(reports),
        custom_fields=report_custom_fields,
        custom_values=report_custom_values,
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
    conn.commit()
    cur.execute("SELECT emp_code, work_type FROM reports WHERE id=%s", (report_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        send_push(row["emp_code"], "Work report approved", row["work_type"] or "Your report was approved", url="/form")
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
    conn.commit()
    cur.execute("SELECT emp_code, work_type FROM reports WHERE id=%s", (report_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        send_push(row["emp_code"], "Work report rejected", row["work_type"] or "Your report was rejected", url="/form")
    return redirect(request.referrer or url_for("manager_view"))

# ══════════════════════════════════════════
#  FEATURE: REPORT DETAIL VIEW
# ══════════════════════════════════════════
@app.route("/report/<int:report_id>")
def report_detail(report_id):
    if not logged_in():
        return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM reports WHERE id=%s", (report_id,))
    report = cur.fetchone()
    cur.close(); conn.close()
    if not report:
        return redirect(url_for("employee_form"))
    role = session.get("role", "employee")
    if role == "employee" and report["emp_code"] != get_emp_code():
        return redirect(url_for("no_access"))
    custom_values = []
    try:
        report_custom_fields, _ = get_form_config("report")
        if report_custom_fields:
            raw = load_custom_field_values("report", [report_id])
            values_for_report = raw.get(report_id, {})
            for cf in report_custom_fields:
                val = values_for_report.get(str(cf["id"]), "")
                if val:
                    custom_values.append({"label": cf["label"], "value": val})
    except Exception:
        pass
    if role == "manager":
        back_url = url_for("manager_view")
    elif role == "supervisor":
        back_url = url_for("supervisor_reports")
    else:
        back_url = url_for("employee_form")
    return render_template(
        "report_detail.html",
        report=report,
        custom_values=custom_values,
        role=role,
        viewer_name=session.get("name", ""),
        perms=session.get("perms", {}),
        sup_perms=session.get("sup_perms", {}),
        back_url=back_url,
    )

# ══════════════════════════════════════════
#  ROUTES — MANAGER: ASSIGN JOBS
# ══════════════════════════════════════════
@app.route("/assign-job", methods=["GET", "POST"])
def assign_job():
    if not logged_in(): return redirect(url_for("index"))
    if not is_manager() and not (is_supervisor() and has_sup_perm("can_assign_jobs")):
        return redirect(url_for("index"))
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
                RETURNING id
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
            new_job_id = cur.fetchone()["id"]
            conn.commit()
            job_custom_fields, _ = get_form_config("job")
            save_custom_field_values("job", new_job_id, job_custom_fields, request.form)
            cur.close(); conn.close()
            success = True
            job_title_for_push = request.form.get("job_title") or "New job"
            for code in set(emp_codes + sup_codes):
                send_push(code, "New job assigned", job_title_for_push, url="/my-jobs")

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
    job_custom_fields, job_visibility = get_form_config("job")
    return render_template("assign_job.html",
        success=success, error=error, edited=edited,
        finalized=finalized, declined=declined,
        employee_choices=employee_choices,
        jobs=jobs, record_count=len(jobs),
        pending_edits_ct=pending_edits_ct,
        pending_requests=pending_requests,
        name=session.get("name", ""),
        role=session.get("role", "manager"), perms=session.get("perms", {}),
        sup_perms=session.get("sup_perms", {}),
        custom_fields=job_custom_fields, visibility=job_visibility,
        filters={"emp": f_emp, "status": f_status, "search": f_search,
                 "from_d": f_from, "to_d": f_to, "review": f_review}
    )


# ══════════════════════════════════════════
#  ROUTES — MANAGER: EDIT JOB (modal POST)
# ══════════════════════════════════════════
@app.route("/edit-job/<int:job_id>", methods=["POST"])
def edit_job(job_id):
    if not logged_in() or not (is_manager() or (is_supervisor() and has_sup_perm("can_assign_jobs"))):
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
    if not logged_in() or not (is_manager() or (is_supervisor() and has_sup_perm("can_assign_jobs"))):
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
    if not logged_in() or not (is_manager() or (is_supervisor() and has_sup_perm("can_assign_jobs"))):
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
    if req["submitted_code"]:
        send_push(req["submitted_code"], "Job edit request reviewed", "Your edit request was finalized.", url="/my-jobs")
    return redirect(url_for("assign_job") + "?finalized=1")


@app.route("/manager/jobs/<int:req_id>/decline", methods=["POST"])
def decline_job_edit(req_id):
    if not logged_in() or not (is_manager() or (is_supervisor() and has_sup_perm("can_assign_jobs"))):
        return redirect(url_for("login"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manager_note = request.form.get("manager_note", "")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT job_id, submitted_code FROM job_edit_requests WHERE id=%s", (req_id,))
    req = cur.fetchone()
    if req:
        cur.execute("""
            UPDATE job_edit_requests SET
                review_status='Declined', reviewed_at=%s, reviewed_by=%s, manager_note=%s
            WHERE id=%s
        """, (now, session.get("name", "Manager"), manager_note, req_id))
        cur.execute("UPDATE jobs SET review_status='Normal' WHERE id=%s", (req["job_id"],))
    conn.commit(); cur.close(); conn.close()
    if req and req["submitted_code"]:
        send_push(req["submitted_code"], "Job edit request reviewed", "Your edit request was declined.", url="/my-jobs")
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

    error = None; records = []; txns = []
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

    # Prepare raw transactions for the Raw Punches tab (cap at 2000 for page size)
    # Convert any non-serialisable fields to plain types
    safe_txns = []
    for t in txns[:2000]:
        safe_txns.append({
            "emp_code":     str(t.get("emp_code","") or ""),
            "punch_time":   str(t.get("punch_time","") or ""),
            "punch_state":  t.get("punch_state", 0),
            "terminal_sn":  str(t.get("terminal_sn","") or ""),
            "terminal_alias": str(t.get("terminal_alias","") or ""),
            "gps_location": str(t.get("gps_location","") or ""),
            "latitude":     t.get("latitude"),
            "longitude":    t.get("longitude"),
            "upload_time":  str(t.get("upload_time","") or ""),
            "source":       t.get("source"),
        })

    last_sync = datetime.now().strftime("%H:%M:%S") if not error else None
    return render_template("attendance.html",
        records=filtered, stats=stats, emp_list=emp_list,
        date_list=date_list, employees=EMPLOYEES,
        filters={"from_d":from_d,"to_d":to_d,"emp":emp_f,"status":status_f,"company":company_f},
        view=view, error=error, record_count=len(filtered),
        last_sync=last_sync,
        raw_transactions=safe_txns,
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
    report_custom_fields, _ = get_form_config("report")
    report_ids = [r["id"] for r in rows]
    custom_values = load_custom_field_values("report", report_ids)
    output = io.StringIO()
    writer = csv.writer(output)
    base_headers = ["ID","Timestamp","Emp Code","Emp Name","Company","Date","Work Type","Client","Location","Summary","Remarks","Status","Supervisor"]
    custom_headers = [f["label"] for f in report_custom_fields]
    writer.writerow(base_headers + custom_headers)
    for r in rows:
        base_row = [r["id"],r["timestamp"],r["emp_code"],r["emp_name"],r["company"],
                    r["date"],r["work_type"],r["client_name"],r["location"],r["summary"],r["remarks"],r["status"],r.get("supervisor_name") or ""]
        cv = custom_values.get(r["id"], {})
        custom_row = [cv.get(f["id"], "") for f in report_custom_fields]
        writer.writerow(base_row + custom_row)
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
#  ROUTES — SUPERVISOR PORTAL
# ══════════════════════════════════════════
@app.route("/supervisor/dashboard")
def supervisor_dashboard():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    sp = session.get("sup_perms", {})

    stats = {}
    if sp.get("can_view_reports"):
        cur.execute("SELECT COUNT(*) AS c FROM reports WHERE date=%s", (today,))
        stats["reports_today"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM reports WHERE review_status='Awaiting Review'")
        stats["reports_pending_review"] = cur.fetchone()["c"]

    if sp.get("can_view_jobs"):
        cur.execute("SELECT COUNT(*) AS c FROM jobs WHERE LOWER(COALESCE(status,'open'))='open'")
        stats["open_jobs"] = cur.fetchone()["c"]

    if sp.get("can_view_ta"):
        cur.execute("SELECT COUNT(*) AS c FROM ta_reports WHERE approval_status='Not Approved'")
        stats["pending_ta"] = cur.fetchone()["c"]

    cur.close(); conn.close()
    return render_template("supervisor_dashboard.html",
        name=session.get("name", "Supervisor"),
        sp=sp, stats=stats, today=today,
        perms=session.get("perms", {}), sup_perms=sp)


@app.route("/supervisor/reports")
def supervisor_reports():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not session.get("sup_perms", {}).get("can_view_reports"): return redirect(url_for("no_access"))
    conn = get_db(); cur = conn.cursor()
    date_filter = request.args.get("date", "")
    query  = "SELECT * FROM reports WHERE 1=1"
    params = []
    if date_filter:
        query += " AND date=%s"; params.append(date_filter)
    query += " ORDER BY timestamp DESC LIMIT 100"
    cur.execute(query, params)
    reports = cur.fetchall()
    cur.close(); conn.close()
    can_approve = session.get("sup_perms", {}).get("can_approve_reports", False)
    return render_template("supervisor_reports.html",
        reports=reports, date_filter=date_filter, can_approve=can_approve,
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))


@app.route("/supervisor/reports/<int:report_id>/approve", methods=["POST"])
def supervisor_approve_report(report_id):
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not session.get("sup_perms", {}).get("can_approve_reports"): return redirect(url_for("no_access"))
    conn = get_db(); cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        UPDATE reports SET review_status='Approved', reviewed_at=%s, reviewed_by=%s
        WHERE id=%s
    """, (now, session.get("name","Supervisor"), report_id))
    conn.commit()
    cur.execute("SELECT emp_code, work_type FROM reports WHERE id=%s", (report_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        send_push(row["emp_code"], "Work report approved", row["work_type"] or "Your report was approved", url="/form")
    return redirect(request.referrer or url_for("supervisor_reports"))


@app.route("/supervisor/reports/<int:report_id>/reject", methods=["POST"])
def supervisor_reject_report(report_id):
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not session.get("sup_perms", {}).get("can_approve_reports"): return redirect(url_for("no_access"))
    conn = get_db(); cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        UPDATE reports SET review_status='Rejected', reviewed_at=%s, reviewed_by=%s
        WHERE id=%s
    """, (now, session.get("name","Supervisor"), report_id))
    conn.commit()
    cur.execute("SELECT emp_code, work_type FROM reports WHERE id=%s", (report_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        send_push(row["emp_code"], "Work report rejected", row["work_type"] or "Your report was rejected", url="/form")
    return redirect(request.referrer or url_for("supervisor_reports"))


@app.route("/supervisor/users")
def supervisor_users():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not session.get("sup_perms", {}).get("can_view_users"): return redirect(url_for("no_access"))
    refresh_employees()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE is_active=TRUE ORDER BY name")
    users = cur.fetchall()
    cur.close(); conn.close()
    return render_template("supervisor_users.html", users=users,
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))



# ══════════════════════════════════════════
#  ROUTES — SUPER ADMIN: USER MANAGEMENT
# ══════════════════════════════════════════
@app.route("/manager/users")
def manage_users():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    refresh_employees()

    status_filter = request.args.get("status", "")
    role_filter   = request.args.get("role", "")
    search        = request.args.get("search", "")

    conn  = get_db(); cur = conn.cursor()
    query = """SELECT u.*,
        sp.can_view_reports, sp.can_approve_reports,
        sp.can_view_jobs, sp.can_assign_jobs,
        sp.can_view_ta, sp.can_approve_ta,
        sp.can_view_users, sp.can_view_sales,
        sp.can_view_support, sp.can_view_clients,
        sp.can_add_employees
    FROM users u
    LEFT JOIN supervisor_permissions sp ON sp.emp_code = u.emp_code
    WHERE 1=1"""
    params = []
    if status_filter == "active":
        query += " AND is_active = TRUE"
    elif status_filter == "inactive":
        query += " AND is_active = FALSE"
    if role_filter in ("employee", "supervisor"):
        query += " AND COALESCE(user_role,'employee') = %s"
        params.append(role_filter)
    if search:
        query += " AND (name ILIKE %s OR username ILIKE %s OR emp_code ILIKE %s)"
        s = f"%{search}%"; params += [s, s, s]
    query += " ORDER BY COALESCE(user_role,'employee') DESC, is_active DESC, name ASC"
    cur.execute(query, params)
    users = cur.fetchall()

    cur.execute("SELECT COUNT(*) AS c FROM users")
    total_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_active = TRUE")
    active_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(user_role,'employee')='supervisor'")
    supervisor_count = cur.fetchone()["c"]
    cur.execute("SELECT id, name FROM departments WHERE is_active=TRUE ORDER BY name")
    department_choices = cur.fetchall()
    cur.close(); conn.close()

    return render_template("manage_users.html",
        name=session.get("name",""),
        users=users, total_count=total_count, active_count=active_count,
        inactive_count=total_count - active_count,
        supervisor_count=supervisor_count,
        department_choices=department_choices,
        filters={"status": status_filter, "search": search, "role": role_filter},
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
    department_id = request.form.get("department_id", "").strip() or None
    position = request.form.get("position", "").strip()
    user_role_new = request.form.get("user_role", "employee").strip()
    if user_role_new not in ("employee", "supervisor"): user_role_new = "employee"
    can_work_report = "can_work_report" in request.form
    can_sales_visit = "can_sales_visit" in request.form
    can_my_jobs     = "can_my_jobs" in request.form
    can_ta          = "can_ta" in request.form
    can_support     = "can_support" in request.form
    can_products    = "can_products" in request.form
    can_challan     = "can_challan" in request.form

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
            INSERT INTO users (emp_code, name, username, password_hash, company, department_id,
                                is_active, user_role, can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support, can_products, can_challan,
                                created_at, created_by)
            VALUES (%s,%s,%s,%s,%s,%s, TRUE, %s, %s,%s,%s,%s,%s,%s,%s, %s,%s)
        """, (emp_code, name, username, hash_password(password), company, department_id,
              user_role_new,
              can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support, can_products, can_challan, now, session.get("name", "manager")))
        conn.commit()
        # create supervisor_permissions row if role is supervisor
        if user_role_new == "supervisor":
            cur.execute("""
                INSERT INTO supervisor_permissions (emp_code, updated_at, updated_by)
                VALUES (%s, %s, %s) ON CONFLICT (emp_code) DO NOTHING
            """, (emp_code, now, session.get("name","manager")))
            conn.commit()
        if position:
            cur.execute("""
                INSERT INTO employee_profiles (emp_code, position, joining_date, updated_at, updated_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (emp_code) DO UPDATE SET position=%s, updated_at=%s, updated_by=%s
            """, (emp_code, position, now[:10], now, session.get("name","manager"),
                  position, now, session.get("name","manager")))
            conn.commit()
        refresh_employees()
        return redirect(url_for("manage_users", flash=f"User '{name}' created as {user_role_new}.", flash_type="success"))
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
    can_challan     = "can_challan" in request.form
    name            = request.form.get("name", "").strip()
    company         = request.form.get("company", "").strip()

    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE users SET can_work_report=%s, can_sales_visit=%s, can_my_jobs=%s, can_ta=%s, can_support=%s, can_products=%s, can_challan=%s,
                          name=COALESCE(NULLIF(%s,''), name),
                          company=COALESCE(NULLIF(%s,''), company)
        WHERE emp_code=%s
    """, (can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support, can_products, can_challan, name, company, emp_code))
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
#  ROUTES — SUPER ADMIN: SUPERVISOR PERMISSIONS
# ══════════════════════════════════════════
@app.route("/manager/users/<emp_code>/set-role", methods=["POST"])
def set_user_role(emp_code):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    new_role = request.form.get("user_role", "employee")
    if new_role not in ("employee", "supervisor"):
        return redirect(url_for("manage_users", flash="Invalid role.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET user_role=%s WHERE emp_code=%s", (new_role, emp_code))
    # If becoming supervisor and no perms row yet, create default one
    if new_role == "supervisor":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            INSERT INTO supervisor_permissions (emp_code, updated_at, updated_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (emp_code) DO NOTHING
        """, (emp_code, now, session.get("name","Super Admin")))
    conn.commit(); cur.close(); conn.close()
    refresh_employees()
    return redirect(url_for("manage_users", flash=f"Role updated to {new_role}.", flash_type="success"))


@app.route("/manager/supervisors")
def manage_supervisors():
    """Super Admin: view all supervisors and edit their permissions."""
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT u.emp_code, u.name, u.username, u.company, u.is_active,
               sp.can_view_reports, sp.can_approve_reports,
               sp.can_view_jobs, sp.can_assign_jobs,
               sp.can_view_ta, sp.can_approve_ta,
               sp.can_view_users, sp.can_view_sales,
               sp.can_view_support, sp.can_view_clients,
               sp.can_add_employees,
               sp.updated_at, sp.updated_by
        FROM users u
        LEFT JOIN supervisor_permissions sp ON sp.emp_code = u.emp_code
        WHERE COALESCE(u.user_role, 'employee') = 'supervisor'
        ORDER BY u.name
    """)
    supervisors = cur.fetchall()
    cur.close(); conn.close()
    return render_template("manage_supervisors.html",
        supervisors=supervisors,
        flash=request.args.get("flash",""),
        flash_type=request.args.get("flash_type","success"))


@app.route("/manager/supervisors/<emp_code>/permissions", methods=["POST"])
def update_supervisor_permissions(emp_code):
    """Super Admin: save supervisor permission set."""
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    perms = {
        "can_view_reports":    "can_view_reports"    in request.form,
        "can_approve_reports": "can_approve_reports" in request.form,
        "can_view_jobs":       "can_view_jobs"       in request.form,
        "can_assign_jobs":     "can_assign_jobs"     in request.form,
        "can_view_ta":         "can_view_ta"         in request.form,
        "can_approve_ta":      "can_approve_ta"      in request.form,
        "can_view_users":      "can_view_users"      in request.form,
        "can_view_sales":      "can_view_sales"      in request.form,
        "can_view_support":    "can_view_support"    in request.form,
        "can_view_clients":    "can_view_clients"    in request.form,
        "can_add_employees":   "can_add_employees"   in request.form,
    }
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO supervisor_permissions
            (emp_code, can_view_reports, can_approve_reports, can_view_jobs, can_assign_jobs,
             can_view_ta, can_approve_ta, can_view_users, can_view_sales, can_view_support,
             can_view_clients, can_add_employees, updated_at, updated_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (emp_code) DO UPDATE SET
            can_view_reports=%s, can_approve_reports=%s, can_view_jobs=%s, can_assign_jobs=%s,
            can_view_ta=%s, can_approve_ta=%s, can_view_users=%s, can_view_sales=%s,
            can_view_support=%s, can_view_clients=%s, can_add_employees=%s, updated_at=%s, updated_by=%s
    """, (
        emp_code,
        perms["can_view_reports"], perms["can_approve_reports"],
        perms["can_view_jobs"], perms["can_assign_jobs"],
        perms["can_view_ta"], perms["can_approve_ta"],
        perms["can_view_users"], perms["can_view_sales"],
        perms["can_view_support"], perms["can_view_clients"], perms["can_add_employees"],
        now, session.get("name","Super Admin"),
        # ON CONFLICT UPDATE values:
        perms["can_view_reports"], perms["can_approve_reports"],
        perms["can_view_jobs"], perms["can_assign_jobs"],
        perms["can_view_ta"], perms["can_approve_ta"],
        perms["can_view_users"], perms["can_view_sales"],
        perms["can_view_support"], perms["can_view_clients"], perms["can_add_employees"],
        now, session.get("name","Super Admin"),
    ))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manage_supervisors",
        flash=f"Permissions saved for supervisor.", flash_type="success"))


@app.route("/manager/employee-profiles")
def employee_profiles():
    """Super Admin: full employee profile list with detail view."""
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    search = request.args.get("search","")
    role_filter = request.args.get("role","")
    conn = get_db(); cur = conn.cursor()
    query  = "SELECT * FROM users WHERE 1=1"
    params = []
    if search:
        query += " AND (name ILIKE %s OR username ILIKE %s OR emp_code ILIKE %s)"
        s = f"%{search}%"; params += [s,s,s]
    if role_filter:
        query += " AND COALESCE(user_role,'employee')=%s"; params.append(role_filter)
    query += " ORDER BY COALESCE(user_role,'employee') ASC, name ASC"
    cur.execute(query, params)
    users = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS c FROM users"); total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(user_role,'employee')='supervisor'"); sup_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(user_role,'employee')='employee'"); emp_count = cur.fetchone()["c"]
    cur.close(); conn.close()
    return render_template("employee_profiles.html",
        users=users, total=total, sup_count=sup_count, emp_count=emp_count,
        filters={"search":search, "role":role_filter},
        flash=request.args.get("flash",""),
        flash_type=request.args.get("flash_type","success"))


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
        cur.execute("SELECT id, name, address, phone FROM companies WHERE name ILIKE %s ORDER BY name LIMIT 10", (f"%{q}%",))
    else:
        cur.execute("SELECT id, name, address, phone FROM companies ORDER BY name LIMIT 10")
    rows = cur.fetchall(); cur.close(); conn.close()
    return jsonify([{"id": r["id"], "name": r["name"], "address": r["address"] or "", "phone": r["phone"] or ""} for r in rows])


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
        role=session.get("role", "employee"), sup_perms=session.get("sup_perms", {}),
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
        role=session.get("role", "employee"), sup_perms=session.get("sup_perms", {}),
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
            cur.execute("SELECT DISTINCT emp_code FROM ta_reports WHERE id = ANY(%s) AND emp_code IS NOT NULL", (ids,))
            affected_codes = [r["emp_code"] for r in cur.fetchall()]
            cur.execute("""
                UPDATE ta_reports SET approval_status='Approved', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
            conn.commit()
            for code in affected_codes:
                send_push(code, "Travel expense approved", "One or more of your TA reports were approved.", url="/ta-report")
        elif action == "unapprove":
            cur.execute("""
                UPDATE ta_reports SET approval_status='Not Approved', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
            conn.commit()
        elif action == "mark_paid":
            cur.execute("""
                UPDATE ta_reports SET payment_status='Paid', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
            conn.commit()
        elif action == "mark_due":
            cur.execute("""
                UPDATE ta_reports SET payment_status='Due', last_edited=%s, last_edited_by=%s
                WHERE id = ANY(%s)
            """, (now, session["name"], ids))
            conn.commit()
        cur.close(); conn.close()

    return redirect(url_for("manager_ta_reports", **{k: v for k, v in request.args.items()}))

# ══════════════════════════════════════════
#  FEATURE: TA EXPENSE CLAIM VOUCHER — PDF GENERATOR
# ══════════════════════════════════════════
def _build_ta_voucher_pdf(employee_info, ta_rows, company_key):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.utils import simpleSplit, ImageReader
    import os, io as _io

    buf = _io.BytesIO()
    W, H = A4
    cv = pdfcanvas.Canvas(buf, pagesize=A4)

    margin  = 14 * mm
    left    = margin
    right   = W - margin
    box_w   = right - left
    top     = H - margin

    FONT   = "Helvetica"
    FONT_B = "Helvetica-Bold"
    FONT_I = "Helvetica-Oblique"

    if 'conneqtor' in company_key.lower() or 'conn' in company_key.lower():
        company_name = "CONNEQTOR TECHNOLOGY PVT. LTD."
        logo_path    = os.path.join("static", "logos", "conneqtor_logo.png")
    else:
        company_name = "IMAX SOLUTION"
        logo_path    = os.path.join("static", "logos", "imax_logo.png")

    grand_total = sum(float(r["expense_cost"] or 0) for r in ta_rows)

    def num_to_words(n):
        n = int(round(n))
        if n == 0: return "Zero"
        ones = ["","One","Two","Three","Four","Five","Six","Seven","Eight","Nine",
                "Ten","Eleven","Twelve","Thirteen","Fourteen","Fifteen","Sixteen",
                "Seventeen","Eighteen","Nineteen"]
        tens = ["","","Twenty","Thirty","Forty","Fifty","Sixty","Seventy","Eighty","Ninety"]
        def say(x):
            if x == 0: return ""
            elif x < 20: return ones[x] + " "
            elif x < 100: return tens[x//10] + (" " + ones[x%10] if x%10 else "") + " "
            elif x < 1000: return ones[x//100] + " Hundred " + say(x%100)
            elif x < 100000: return say(x//1000) + "Thousand " + say(x%1000)
            elif x < 10000000: return say(x//100000) + "Lakh " + say(x%100000)
            else: return say(x//10000000) + "Crore " + say(x%10000000)
        return say(n).strip() + " Only"

    def txt(x, y, s, font=FONT, size=9, color=colors.black):
        cv.setFont(font, size); cv.setFillColor(color)
        cv.drawString(x, y, str(s)); cv.setFillColor(colors.black)
    def txt_r(x, y, s, font=FONT, size=9):
        cv.setFont(font, size); cv.drawRightString(x, y, str(s))
    def txt_c(x, y, s, font=FONT, size=9):
        cv.setFont(font, size); cv.drawCentredString(x, y, str(s))
    def hline(x1, y, x2, w=0.5):
        cv.setLineWidth(w); cv.line(x1, y, x2, y)
    def vline(x, y1, y2, w=0.5):
        cv.setLineWidth(w); cv.line(x, y1, x, y2)
    def rect(x, y, w, h, lw=0.7):
        cv.setLineWidth(lw); cv.rect(x, y, w, h)

    y = top

    # Title row
    title_h = 9 * mm
    rect(left, y - title_h, box_w, title_h, lw=1)
    cv.setFont(FONT_B, 13)
    cv.drawString(left + 4*mm, y - title_h + 2.8*mm, "Expense Claim Voucher")
    logo_x = right - 42*mm
    logo_y = y - title_h + 1*mm
    try:
        logo_img = ImageReader(logo_path)
        cv.drawImage(logo_img, logo_x, logo_y, width=38*mm, height=7*mm,
                     preserveAspectRatio=True, anchor="c", mask="auto")
    except Exception:
        txt(logo_x, logo_y + 2*mm, company_name, font=FONT_B, size=7)
    y -= title_h

    # Claimant row
    row1_h = 7.5 * mm
    rect(left, y - row1_h, box_w, row1_h, lw=0.7)
    txt(left + 2*mm, y - row1_h + 2.2*mm, "Claimant:", font=FONT_B, size=9)
    txt(left + 26*mm, y - row1_h + 2.2*mm, employee_info.get("name", ""), size=9.5)
    y -= row1_h

    # Designation row
    row2_h = 7.5 * mm
    rect(left, y - row2_h, box_w, row2_h, lw=0.7)
    txt(left + 2*mm, y - row2_h + 2.2*mm, "Designation:", font=FONT_B, size=9)
    txt(left + 26*mm, y - row2_h + 2.2*mm, employee_info.get("designation", ""), size=9.5)
    y -= row2_h

    # Emp ID / Mobile / Date row
    row3_h = 7.5 * mm
    rect(left, y - row3_h, box_w, row3_h, lw=0.7)
    col1_x = left; col2_x = left + box_w * 0.38; col3_x = left + box_w * 0.68
    vline(col2_x, y - row3_h, y); vline(col3_x, y - row3_h, y)
    txt(col1_x + 2*mm, y - row3_h + 2.2*mm, "Employee ID No.:", font=FONT_B, size=8.5)
    txt(col1_x + 32*mm, y - row3_h + 2.2*mm, employee_info.get("emp_code", ""), size=9)
    txt(col2_x + 2*mm, y - row3_h + 2.2*mm, "Mobile No.:", font=FONT_B, size=8.5)
    txt(col2_x + 22*mm, y - row3_h + 2.2*mm, employee_info.get("mobile", ""), size=9)
    txt(col3_x + 2*mm, y - row3_h + 2.2*mm, "Date:", font=FONT_B, size=8.5)
    from datetime import date as _date
    txt(col3_x + 13*mm, y - row3_h + 2.2*mm, _date.today().strftime("%d %b %Y"), size=9)
    y -= row3_h

    # Table header
    tbl_hdr_h = 8 * mm
    col_date_w = 26*mm; col_amt_w = 22*mm; col_rmk_w = 44*mm
    col_part_w = box_w - col_date_w - col_amt_w - col_rmk_w
    col_date_x = left; col_part_x = col_date_x + col_date_w
    col_amt_x  = col_part_x + col_part_w; col_rmk_x = col_amt_x + col_amt_w

    rect(left, y - tbl_hdr_h, box_w, tbl_hdr_h, lw=0.8)
    vline(col_part_x, y - tbl_hdr_h, y); vline(col_amt_x, y - tbl_hdr_h, y); vline(col_rmk_x, y - tbl_hdr_h, y)
    txt_c(col_date_x + col_date_w/2, y - tbl_hdr_h + 2.5*mm, "Date",        font=FONT_B, size=9)
    txt_c(col_part_x + col_part_w/2, y - tbl_hdr_h + 2.5*mm, "Particulars", font=FONT_B, size=9)
    txt_c(col_amt_x  + col_amt_w/2,  y - tbl_hdr_h + 2.5*mm, "Amount",      font=FONT_B, size=9)
    txt_c(col_rmk_x  + col_rmk_w/2,  y - tbl_hdr_h + 2.5*mm, "REMARK",      font=FONT_B, size=9)
    y -= tbl_hdr_h

    base_row_h = 6.5*mm; part_text_w = col_part_w - 3*mm; remark_text_w = col_rmk_w - 3*mm
    footer_reserve = 58*mm; table_top = y

    def draw_row(r, row_y):
        from_p = r.get("from_place","") or ""; to_p = r.get("to_place","") or ""; by = r.get("travel_by","") or ""
        particulars = f"{from_p} to {to_p} via {by}" if by else f"{from_p} to {to_p}"
        remark = r.get("description","") or ""; date_str = r.get("travel_date","") or ""
        try:
            from datetime import datetime as _dt
            date_str = _dt.strptime(date_str, "%Y-%m-%d").strftime("%-d %b %Y")
        except Exception: pass
        part_lines = simpleSplit(particulars, FONT, 8.5, part_text_w)
        rmk_lines  = simpleSplit(remark, FONT, 8.5, remark_text_w)
        max_lines  = max(len(part_lines), len(rmk_lines), 1)
        rh = max(base_row_h, max_lines * 4*mm + 2*mm)
        cv.setLineWidth(0.4); cv.setStrokeColor(colors.Color(0.75,0.75,0.75))
        cv.line(left, row_y - rh, right, row_y - rh); cv.setStrokeColor(colors.black)
        vline(col_part_x, row_y-rh, row_y, w=0.4); vline(col_amt_x, row_y-rh, row_y, w=0.4); vline(col_rmk_x, row_y-rh, row_y, w=0.4)
        cell_y = row_y - 4.2*mm
        txt_c(col_date_x + col_date_w/2, cell_y, date_str, size=8.2)
        for i, ln in enumerate(part_lines): txt(col_part_x + 1.5*mm, cell_y - i*4*mm, ln, size=8.5)
        amt = float(r.get("expense_cost",0) or 0)
        amt_str = f"{amt:,.0f}" if amt == int(amt) else f"{amt:,.2f}"
        txt_r(col_amt_x + col_amt_w - 2*mm, cell_y, amt_str, size=8.5)
        for i, ln in enumerate(rmk_lines): txt(col_rmk_x + 1.5*mm, cell_y - i*4*mm, ln, size=8.2)
        return rh

    for r in ta_rows:
        from_p = r.get("from_place","") or ""; to_p = r.get("to_place","") or ""; by = r.get("travel_by","") or ""
        particulars = f"{from_p} to {to_p} via {by}" if by else f"{from_p} to {to_p}"
        remark = r.get("description","") or ""
        pl = simpleSplit(particulars, FONT, 8.5, part_text_w); rl = simpleSplit(remark, FONT, 8.5, remark_text_w)
        est_h = max(base_row_h, max(len(pl),len(rl),1)*4*mm+2*mm)
        if y - est_h < margin + footer_reserve:
            cv.setLineWidth(0.8); cv.rect(left, y, box_w, table_top - y)
            cv.showPage(); y2 = H - margin
            rect(left, y2-7*mm, box_w, 7*mm, lw=0.8)
            txt(left+2*mm, y2-5*mm, f"{company_name} — Expense Claim Voucher (continued)", font=FONT_B, size=9)
            y2 -= 7*mm
            rect(left, y2-tbl_hdr_h, box_w, tbl_hdr_h, lw=0.8)
            vline(col_part_x,y2-tbl_hdr_h,y2); vline(col_amt_x,y2-tbl_hdr_h,y2); vline(col_rmk_x,y2-tbl_hdr_h,y2)
            txt_c(col_date_x+col_date_w/2, y2-tbl_hdr_h+2.5*mm,"Date",font=FONT_B,size=9)
            txt_c(col_part_x+col_part_w/2, y2-tbl_hdr_h+2.5*mm,"Particulars",font=FONT_B,size=9)
            txt_c(col_amt_x+col_amt_w/2,   y2-tbl_hdr_h+2.5*mm,"Amount",font=FONT_B,size=9)
            txt_c(col_rmk_x+col_rmk_w/2,   y2-tbl_hdr_h+2.5*mm,"REMARK",font=FONT_B,size=9)
            y2 -= tbl_hdr_h; table_top = y2; y = y2
        rh = draw_row(r, y); y -= rh

    for _ in range(2):
        if y - base_row_h >= margin + footer_reserve:
            cv.setLineWidth(0.3); cv.setStrokeColor(colors.Color(0.82,0.82,0.82))
            cv.line(left, y-base_row_h, right, y-base_row_h); cv.setStrokeColor(colors.black)
            vline(col_part_x,y-base_row_h,y,w=0.3); vline(col_amt_x,y-base_row_h,y,w=0.3); vline(col_rmk_x,y-base_row_h,y,w=0.3)
            y -= base_row_h

    cv.setLineWidth(0.8); cv.rect(left, y, box_w, table_top - y)

    # TOTAL row
    total_h = 7.5*mm
    rect(left, y-total_h, box_w, total_h, lw=0.8)
    vline(col_part_x,y-total_h,y); vline(col_amt_x,y-total_h,y); vline(col_rmk_x,y-total_h,y)
    txt(left+2*mm, y-total_h+2.3*mm, "TOTAL", font=FONT_B, size=9.5)
    gt_str = f"{grand_total:,.0f}" if grand_total==int(grand_total) else f"{grand_total:,.2f}"
    txt_r(col_amt_x+col_amt_w-2*mm, y-total_h+2.3*mm, gt_str, font=FONT_B, size=9.5)
    y -= total_h

    # Grand total in words
    words_h = 9*mm
    rect(left, y-words_h, box_w, words_h, lw=0.7)
    txt(left+2*mm, y-words_h+5*mm, "Grand Total (in words):", font=FONT_B, size=8.5)
    txt(left+2*mm, y-words_h+1.5*mm, f"Rs. {num_to_words(grand_total)}", font=FONT_I, size=8.5)
    y -= words_h

    # Note row
    note_h = 6*mm
    rect(left, y-note_h, box_w, note_h, lw=0.6)
    txt(left+2*mm, y-note_h+1.8*mm, "Note: All Expenses must be supported with proper bills (if any)", size=8)
    y -= note_h

    # Signature block
    sig_h = 32*mm
    rect(left, y-sig_h, box_w, sig_h, lw=0.8)
    mid_x = left + box_w/2
    vline(mid_x, y-sig_h, y, w=0.8)
    txt_c(left+box_w/4, y-5*mm, "Received", font=FONT_B, size=10)
    hline(left+5*mm, y-6*mm, mid_x-5*mm, w=0.8)
    txt(left+3*mm, y-13*mm, "Advance:",    font=FONT_B, size=8.5)
    txt(left+3*mm, y-19*mm, "Due/Return:", font=FONT_B, size=8.5)
    txt(left+3*mm, y-25*mm, "Date:",       font=FONT_B, size=8.5)
    hline(left+5*mm, y-sig_h+8*mm, mid_x-5*mm, w=0.8)
    txt_c(left+box_w/4, y-sig_h+4*mm, "Signature", font=FONT_B, size=9)
    txt_c(left+box_w*3/4, y-5*mm, "Approved By", font=FONT_B, size=10)
    hline(mid_x+5*mm, y-6*mm, right-5*mm, w=0.8)
    txt(mid_x+3*mm, y-13*mm, "Name:",            font=FONT_B, size=8.5)
    txt(mid_x+3*mm, y-19*mm, "Designation:",     font=FONT_B, size=8.5)
    txt(mid_x+3*mm, y-25*mm, "Sign. with date:", font=FONT_B, size=8.5)
    hline(mid_x+5*mm, y-sig_h+8*mm, right-5*mm, w=0.8)
    txt_c(left+box_w*3/4, y-sig_h+4*mm, "Company Date & Seal", font=FONT_B, size=9)
    y -= sig_h

    # APPROVED watermark
    cv.saveState()
    cv.setFillColor(colors.Color(0, 0.55, 0.2, alpha=0.10))
    cv.setFont(FONT_B, 72)
    cv.translate(W/2, H/2); cv.rotate(40)
    cv.drawCentredString(0, 0, "APPROVED")
    cv.restoreState()

    cv.save(); buf.seek(0)
    return buf


@app.route("/ta-report/<int:ta_id>/pdf")
def ta_voucher_pdf_single(ta_id):
    if not logged_in(): return redirect(url_for("login"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM ta_reports WHERE id=%s", (ta_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return "TA record not found.", 404
    if not is_manager():
        if row["emp_code"] != get_emp_code():
            cur.close(); conn.close()
            return redirect(url_for("no_access"))
        if row["approval_status"] != "Approved":
            cur.close(); conn.close()
            return "PDF is only available after manager approval.", 403
    cur.execute("""
        SELECT u.name, u.emp_code, u.company,
               ep.position AS designation, ep.phone AS mobile
        FROM users u
        LEFT JOIN employee_profiles ep ON ep.emp_code = u.emp_code
        WHERE u.emp_code=%s
    """, (row["emp_code"],))
    profile = cur.fetchone(); cur.close(); conn.close()
    employee_info = {
        "name":        row["emp_name"] or "",
        "emp_code":    row["emp_code"] or "",
        "designation": (profile["designation"] if profile and profile["designation"] else ""),
        "mobile":      (profile["mobile"]      if profile and profile["mobile"]      else ""),
        "company":     (profile["company"]     if profile and profile["company"]     else ""),
    }
    pdf_buf = _build_ta_voucher_pdf(employee_info, [row], employee_info["company"])
    fname = f"TA_Voucher_{row['emp_name'].replace(' ','_')}_{row['travel_date']}.pdf"
    from flask import send_file
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=False, download_name=fname)


@app.route("/manager/ta-reports/bulk-pdf", methods=["POST"])
def ta_voucher_pdf_bulk():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    ids = [int(i) for i in request.form.getlist("selected_ids") if i.isdigit()]
    if not ids: return redirect(url_for("manager_ta_reports"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT * FROM ta_reports WHERE id = ANY(%s) AND approval_status='Approved'
        ORDER BY travel_date ASC, id ASC
    """, (ids,))
    rows = cur.fetchall()
    if not rows:
        cur.close(); conn.close()
        return "No approved TA records found in selection.", 400
    emp_code = rows[0]["emp_code"]
    cur.execute("""
        SELECT u.name, u.emp_code, u.company,
               ep.position AS designation, ep.phone AS mobile
        FROM users u
        LEFT JOIN employee_profiles ep ON ep.emp_code = u.emp_code
        WHERE u.emp_code=%s
    """, (emp_code,))
    profile = cur.fetchone(); cur.close(); conn.close()
    employee_info = {
        "name":        rows[0]["emp_name"] or "",
        "emp_code":    emp_code or "",
        "designation": (profile["designation"] if profile and profile["designation"] else ""),
        "mobile":      (profile["mobile"]      if profile and profile["mobile"]      else ""),
        "company":     (profile["company"]     if profile and profile["company"]     else ""),
    }
    pdf_buf = _build_ta_voucher_pdf(employee_info, rows, employee_info["company"])
    fname = f"TA_Voucher_{employee_info['name'].replace(' ','_')}_bulk.pdf"
    from flask import send_file
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=False, download_name=fname)


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
               COUNT(DISTINCT s.id) FILTER (WHERE LOWER(COALESCE(s.status,'pending')) <> 'complete') AS open_support_count,
               COUNT(DISTINCT ch.id)              AS challan_count
        FROM companies c
        LEFT JOIN sales_visits v ON v.company_id = c.id
        LEFT JOIN support_reports s ON s.company_id = c.id
        LEFT JOIN challans ch ON ch.company_id = c.id
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

    cur.execute("""
        SELECT * FROM challans
        WHERE company_id=%s
        ORDER BY id DESC
    """, (company_id,))
    challans = cur.fetchall()
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
        challans=challans,
        challan_count=len(challans),
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


# refresh_employees() is redefined below to also include can_support.
def refresh_employees():
    global EMPLOYEES, USERNAME_MAP
    conn = get_db(); cur = conn.cursor()
    has_products_col = _column_exists(cur, "users", "can_products")
    has_challan_col  = _column_exists(cur, "users", "can_challan")
    products_select = "COALESCE(can_products, FALSE) AS can_products" if has_products_col else "FALSE AS can_products"
    challan_select  = "COALESCE(can_challan, FALSE) AS can_challan"   if has_challan_col  else "FALSE AS can_challan"
    cur.execute(f"""
        SELECT emp_code, name, username, password_hash, company,
               is_active, can_work_report, can_sales_visit, can_my_jobs, can_ta,
               COALESCE(can_support, TRUE) AS can_support,
               COALESCE(user_role, 'employee') AS user_role,
               {products_select},
               {challan_select}
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
            "can_products": r["can_products"],
            "can_challan": r["can_challan"],
            "user_role": r["user_role"],
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

# NOTE: an old duplicate "/" route + support-perm patch used to live here.
# It was dead code (Flask serves whichever route for a given path was
# registered first, which is the real index() far above, the one with the
# is_supervisor() branch) and also out of date, so it has been removed.
# can_support is already handled by the index() function near the top of
# this file and by has_perm("support") elsewhere — no patch needed.


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
        role=session.get("role", "employee"), sup_perms=session.get("sup_perms", {}),
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
        total=len(all_products), perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))


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


@app.route("/manager/products/bulk-delete", methods=["POST"])
def bulk_delete_products():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    ids_raw = request.form.get("ids", "").strip()
    if not ids_raw:
        return redirect(url_for("manager_products", flash="No products selected.", flash_type="error"))
    try:
        ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
    except Exception:
        return redirect(url_for("manager_products", flash="Invalid selection.", flash_type="error"))
    if not ids:
        return redirect(url_for("manager_products", flash="No products selected.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE id = ANY(%s)", (ids,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash=f"{len(ids)} product(s) deleted.", flash_type="success"))


@app.route("/manager/products/bulk-move", methods=["POST"])
def bulk_move_products():
    if not logged_in() or not is_manager(): return redirect(url_for("login"))
    ids_raw   = request.form.get("ids", "").strip()
    brand_id  = request.form.get("brand_id", "").strip()
    if not ids_raw or not brand_id:
        return redirect(url_for("manager_products", flash="Missing selection or target brand.", flash_type="error"))
    try:
        ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
        brand_id = int(brand_id)
    except Exception:
        return redirect(url_for("manager_products", flash="Invalid data.", flash_type="error"))
    if not ids:
        return redirect(url_for("manager_products", flash="No products selected.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE products SET brand_id=%s WHERE id = ANY(%s)", (brand_id, ids))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manager_products", flash=f"{len(ids)} product(s) moved to new brand.", flash_type="success"))


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

# ══════════════════════════════════════════
#  CHALLAN / INVOICE GENERATOR
# ══════════════════════════════════════════
def init_challan_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS challans (
            id              SERIAL PRIMARY KEY,
            challan_no      TEXT,
            challan_date    TEXT,
            seller_company_key TEXT,
            seller_name     TEXT,
            seller_address  TEXT,
            seller_gstin    TEXT,
            seller_state    TEXT,
            seller_state_code TEXT,
            seller_contact  TEXT,
            buyer_name      TEXT,
            buyer_address   TEXT,
            buyer_gstin     TEXT,
            buyer_state     TEXT,
            buyer_state_code TEXT,
            company_id      INTEGER,
            place_of_supply TEXT,
            delivery_note   TEXT,
            mode_of_payment TEXT,
            reference_no    TEXT,
            other_references TEXT,
            buyers_order_no TEXT,
            buyers_order_date TEXT,
            dispatch_doc_no TEXT,
            delivery_note_date TEXT,
            dispatched_through TEXT,
            destination     TEXT,
            terms_of_delivery TEXT,
            declaration     TEXT,
            jurisdiction    TEXT,
            items           TEXT,
            created_at      TEXT,
            created_by      TEXT,
            updated_at      TEXT,
            updated_by      TEXT
        )
    """)
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_challan BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE challans ADD COLUMN IF NOT EXISTS seller_company_key TEXT")
    cur.execute("ALTER TABLE challans ADD COLUMN IF NOT EXISTS company_id INTEGER")
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_challan_db()
        print("✅ Challan table ready")
    except Exception as e:
        print(f"⚠️ Challan DB init error: {e}")

CHALLAN_COMPANIES = {
    "conneqtor": {
        "label": "Conneqtor Technology Pvt Ltd",
        "seller_name": "CONNEQTOR TECHNOLOGY PVT.LTD. (KOLKATA)",
        "seller_address": "C-B1, 1/30 PATULI TOWNSHIP BAISHNABGHATA\nKOLKATA-700094",
        "seller_gstin": "19AAICC3755D1ZN",
        "seller_state": "West Bengal",
        "seller_state_code": "19",
        "seller_contact": "9830895433",
    },
    "imax": {
        "label": "Imax Solutions",
        "seller_name": "IMAX SOLUTIONS",
        "seller_address": "C-B1, 1/30 PATULI TOWNSHIP BAISHNABGHATA\nKOLKATA-700094",
        "seller_gstin": "",
        "seller_state": "",
        "seller_state_code": "",
        "seller_contact": "",
    },
}
CHALLAN_DEFAULT_COMPANY_KEY = "conneqtor"

def _next_challan_no():
    """Look at the highest existing CHALAN/### number and bump it by one."""
    import re
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT challan_no FROM challans WHERE challan_no LIKE 'CHALAN/%'")
    rows = cur.fetchall(); cur.close(); conn.close()
    best = 0
    for r in rows:
        m = re.search(r"(\d+)\s*$", r["challan_no"] or "")
        if m:
            best = max(best, int(m.group(1)))
    return f"CHALAN/{best + 1}"

@app.route("/challan")
def challan_list():
    if not logged_in(): return redirect(url_for("login"))
    if not has_perm("can_challan"): return redirect(url_for("no_access"))
    q = request.args.get("q", "").strip()
    conn = get_db(); cur = conn.cursor()
    if q:
        cur.execute("""
            SELECT * FROM challans
            WHERE challan_no ILIKE %s OR buyer_name ILIKE %s
            ORDER BY id DESC LIMIT 200
        """, (f"%{q}%", f"%{q}%"))
    else:
        cur.execute("SELECT * FROM challans ORDER BY id DESC LIMIT 200")
    rows = cur.fetchall(); cur.close(); conn.close()
    return render_template("challan_list.html", name=session.get("name"), is_manager=is_manager(),
                            challans=rows, q=q)

@app.route("/challan/new", methods=["GET"])
def challan_new():
    if not logged_in(): return redirect(url_for("login"))
    if not has_perm("can_challan"): return redirect(url_for("no_access"))
    company_key = request.args.get("company", "")
    if company_key not in CHALLAN_COMPANIES:
        company_key = ""
    draft = dict(CHALLAN_COMPANIES[company_key]) if company_key else {}
    draft.pop("label", None)
    draft.update({
        "id": None,
        "seller_company_key": company_key,
        "seller_name": draft.get("seller_name", ""),
        "seller_address": draft.get("seller_address", ""),
        "seller_gstin": draft.get("seller_gstin", ""),
        "seller_state": draft.get("seller_state", ""),
        "seller_state_code": draft.get("seller_state_code", ""),
        "seller_contact": draft.get("seller_contact", ""),
        "challan_no": "",
        "challan_date": "",
        "buyer_name": "", "buyer_address": "", "buyer_gstin": "",
        "buyer_state": "", "buyer_state_code": "", "place_of_supply": "",
        "delivery_note": "", "mode_of_payment": "", "reference_no": "",
        "other_references": "", "buyers_order_no": "", "buyers_order_date": "",
        "dispatch_doc_no": "", "delivery_note_date": "", "dispatched_through": "",
        "destination": "", "terms_of_delivery": "",
        "declaration": "We declare that this invoice shows the actual price of the goods described and that all particulars are true and correct.",
        "jurisdiction": "SUBJECT TO KOLKATA JURISDICTION",
        "items": [{"description": "", "hsn": "", "qty": "", "unit": "", "disc": "", "amount": ""}],
    })
    return render_template("challan_form.html", name=session.get("name"), is_manager=is_manager(),
                            c=draft, mode="new", companies=CHALLAN_COMPANIES)


@app.route("/challan/<int:challan_id>/edit", methods=["GET"])
def challan_edit(challan_id):
    if not logged_in(): return redirect(url_for("login"))
    if not has_perm("can_challan"): return redirect(url_for("no_access"))
    import json
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM challans WHERE id=%s", (challan_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        return redirect(url_for("challan_list"))
    c = dict(row)
    try:
        c["items"] = json.loads(c["items"]) if c["items"] else []
    except Exception:
        c["items"] = []
    if not c["items"]:
        c["items"] = [{"description": "", "hsn": "", "qty": "", "unit": "", "disc": "", "amount": ""}]
    return render_template("challan_form.html", name=session.get("name"), is_manager=is_manager(),
                            c=c, mode="edit", companies=CHALLAN_COMPANIES)

@app.route("/challan/save", methods=["POST"])
def challan_save():
    if not logged_in(): return redirect(url_for("login"))
    if not has_perm("can_challan"): return redirect(url_for("no_access"))
    import json
    f = request.form
    challan_id = f.get("id", "").strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    who = session.get("name", "")

    descs  = request.form.getlist("item_description")
    hsns   = request.form.getlist("item_hsn")
    qtys   = request.form.getlist("item_qty")
    units  = request.form.getlist("item_unit")
    discs  = request.form.getlist("item_disc")
    amts   = request.form.getlist("item_amount")
    items = []
    for i in range(len(descs)):
        if not (descs[i] or "").strip():
            continue
        items.append({
            "description": descs[i].strip(),
            "hsn": hsns[i].strip() if i < len(hsns) else "",
            "qty": qtys[i].strip() if i < len(qtys) else "",
            "unit": units[i].strip() if i < len(units) else "",
            "disc": discs[i].strip() if i < len(discs) else "",
            "amount": amts[i].strip() if i < len(amts) else "",
        })
    items_json = json.dumps(items)

    buyer_name_val = f.get("buyer_name", "").strip()
    owner_code = session.get("emp_code") if not is_manager() else None
    company_id = get_or_create_company(buyer_name_val, owner_code=owner_code) if buyer_name_val else None

    fields = (
        f.get("challan_no","").strip(), f.get("challan_date","").strip(),
        f.get("seller_company_key","").strip(),
        f.get("seller_name","").strip(), f.get("seller_address","").strip(),
        f.get("seller_gstin","").strip(), f.get("seller_state","").strip(),
        f.get("seller_state_code","").strip(), f.get("seller_contact","").strip(),
        buyer_name_val, f.get("buyer_address","").strip(),
        f.get("buyer_gstin","").strip(), f.get("buyer_state","").strip(),
        f.get("buyer_state_code","").strip(), company_id, f.get("place_of_supply","").strip(),
        f.get("delivery_note","").strip(), f.get("mode_of_payment","").strip(),
        f.get("reference_no","").strip(), f.get("other_references","").strip(),
        f.get("buyers_order_no","").strip(), f.get("buyers_order_date","").strip(),
        f.get("dispatch_doc_no","").strip(), f.get("delivery_note_date","").strip(),
        f.get("dispatched_through","").strip(), f.get("destination","").strip(),
        f.get("terms_of_delivery","").strip(), f.get("declaration","").strip(),
        f.get("jurisdiction","").strip(), items_json,
    )

    conn = get_db(); cur = conn.cursor()
    if challan_id:
        cur.execute("""
            UPDATE challans SET
                challan_no=%s, challan_date=%s, seller_company_key=%s, seller_name=%s, seller_address=%s,
                seller_gstin=%s, seller_state=%s, seller_state_code=%s, seller_contact=%s,
                buyer_name=%s, buyer_address=%s, buyer_gstin=%s, buyer_state=%s,
                buyer_state_code=%s, company_id=%s, place_of_supply=%s, delivery_note=%s, mode_of_payment=%s,
                reference_no=%s, other_references=%s, buyers_order_no=%s, buyers_order_date=%s,
                dispatch_doc_no=%s, delivery_note_date=%s, dispatched_through=%s, destination=%s,
                terms_of_delivery=%s, declaration=%s, jurisdiction=%s, items=%s,
                updated_at=%s, updated_by=%s
            WHERE id=%s
        """, fields + (now, who, challan_id))
        cur.execute("SELECT id FROM challans WHERE id=%s", (challan_id,))
    else:
        cur.execute("""
            INSERT INTO challans (
                challan_no, challan_date, seller_company_key, seller_name, seller_address,
                seller_gstin, seller_state, seller_state_code, seller_contact,
                buyer_name, buyer_address, buyer_gstin, buyer_state,
                buyer_state_code, company_id, place_of_supply, delivery_note, mode_of_payment,
                reference_no, other_references, buyers_order_no, buyers_order_date,
                dispatch_doc_no, delivery_note_date, dispatched_through, destination,
                terms_of_delivery, declaration, jurisdiction, items,
                created_at, created_by
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, fields + (now, who))
    new_id = cur.fetchone()["id"]
    conn.commit(); cur.close(); conn.close()

    action = request.form.get("action", "save")
    if action == "save_pdf":
        return redirect(url_for("challan_pdf", challan_id=new_id))
    return redirect(url_for("challan_list", flash=f"✅ Challan {fields[0]} saved", flash_type="success"))

@app.route("/challan/<int:challan_id>/delete", methods=["POST"])
def challan_delete(challan_id):
    if not logged_in(): return redirect(url_for("login"))
    if not has_perm("can_challan"): return redirect(url_for("no_access"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM challans WHERE id=%s", (challan_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("challan_list"))

def _build_challan_pdf(c, items):
    """Generates the Challan PDF bytes in a grid layout matching the company's Excel format.
    Supports multi-page output when the header content or item list is long."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.utils import simpleSplit

    buf = io.BytesIO()
    W, H = A4
    cv = pdfcanvas.Canvas(buf, pagesize=A4)

    margin = 12 * mm
    left = margin
    right = W - margin
    top = H - margin
    bottom_limit = margin
    box_w = right - left

    FONT = "Helvetica"
    FONT_B = "Helvetica-Bold"

    def text(x, y, s, font=FONT, size=8.5, leading=None, max_w=None):
        cv.setFont(font, size)
        if max_w:
            lines = simpleSplit(s, font, size, max_w)
            ld = leading if leading is not None else (size * 0.46) * mm
            for i, ln in enumerate(lines):
                cv.drawString(x, y - i * ld, ln)
            return len(lines) * ld
        else:
            cv.drawString(x, y, s)
            return (size * 0.46) * mm

    def measure_lines(s, font, size, max_w):
        return max(len(simpleSplit(s or "", font, size, max_w)), 1)

    def center(x, y, s, font=FONT_B, size=14):
        cv.setFont(font, size)
        cv.drawCentredString(x, y, s)

    # ---- Layout constants shared across pages ----
    left_w = box_w * 0.565
    right_w = box_w - left_w
    rx = left + left_w
    mid = rx + right_w / 2
    title_h = 9 * mm

    col_w = {"sl": 9*mm, "desc": 0, "hsn": 22*mm, "qty": 20*mm, "disc": 16*mm, "amount": 28*mm}
    fixed_cols = col_w["sl"] + col_w["hsn"] + col_w["qty"] + col_w["disc"] + col_w["amount"]
    col_w["desc"] = box_w - fixed_cols
    cols = ["sl", "desc", "hsn", "qty", "disc", "amount"]
    headers = {"sl": "Sl\nNo", "desc": "Description of Goods", "hsn": "HSN/SAC",
               "qty": "Quantity", "disc": "Disc. %", "amount": "Amount"}
    tbl_hdr_h = 9*mm
    base_row_h = 7*mm
    line_h = 3.6*mm
    desc_w = col_w["desc"] - 3*mm

    # ---- Pre-measure header block height (seller + buyer column), so the box
    #      grows to fit content instead of clipping it ----
    seller_name_lines = measure_lines(c.get("seller_name",""), FONT_B, 10.5, left_w - 4*mm)
    seller_addr_lines = [ln.strip() for ln in (c.get("seller_address") or "").split("\n") if ln.strip()]
    seller_addr_wrapped_lines = sum(measure_lines(ln, FONT, 8.3, left_w - 4*mm) for ln in seller_addr_lines)
    seller_block_h = (seller_name_lines * 4.2*mm + 1.2*mm
                       + seller_addr_wrapped_lines * 4*mm
                       + (4*mm if c.get("seller_gstin") else 0)
                       + (4*mm if c.get("seller_state") else 0)
                       + 4*mm)  # top padding

    buyer_name_lines = measure_lines(c.get("buyer_name",""), FONT_B, 9.5, left_w - 4*mm)
    buyer_addr_lines = [ln.strip() for ln in (c.get("buyer_address") or "").split("\n") if ln.strip()]
    buyer_addr_wrapped_lines = sum(measure_lines(ln, FONT, 8.3, left_w - 4*mm) for ln in buyer_addr_lines)
    buyer_block_h = (4.3*mm  # "Details of Receiver" label
                      + buyer_name_lines * 4*mm + 0.8*mm
                      + buyer_addr_wrapped_lines * 3.8*mm
                      + (4*mm if c.get("buyer_gstin") else 0)
                      + (4*mm if c.get("buyer_state") else 0)
                      + (4*mm if c.get("place_of_supply") else 0)
                      + 4.2*mm)  # top padding before label

    left_col_h = seller_block_h + buyer_block_h
    right_col_h = 6 * 9.2*mm + 8.5*mm + 4.5*mm  # 6 meta rows + terms-of-delivery row
    header_h = max(left_col_h, right_col_h, 55*mm)

    def draw_page_border_placeholder():
        # Outer page content area border is drawn implicitly via component rects.
        pass

    def draw_title(y):
        cv.setLineWidth(1)
        cv.rect(left, y - title_h, box_w, title_h)
        center(W / 2, y - title_h + 2.8 * mm, "CHALLAN", size=16)
        return y - title_h

    def draw_header_block(y):
        """Draws the seller/buyer/meta box. Returns new y (bottom of this block)."""
        cv.setLineWidth(1)
        cv.rect(left, y - header_h, box_w, header_h)
        cv.line(rx, y - header_h, rx, y)

        # Seller block
        sy = y - 4 * mm
        name_h = text(left + 2*mm, sy, c.get("seller_name","") or "", font=FONT_B, size=10.5,
                       max_w=left_w - 4*mm, leading=4.2*mm)
        sy -= max(name_h, 4.2*mm) + 1.2*mm
        for ln in seller_addr_lines:
            consumed = text(left + 2*mm, sy, ln, size=8.3, max_w=left_w - 4*mm, leading=4*mm)
            sy -= max(consumed, 4*mm)
        if c.get("seller_gstin"):
            text(left + 2*mm, sy, f"GSTIN/UIN: {c['seller_gstin']}", size=8.3); sy -= 4*mm
        if c.get("seller_state"):
            text(left + 2*mm, sy, f"State Name: {c['seller_state']}, Code: {c.get('seller_state_code','')}"
                 + (f"  Contact: {c['seller_contact']}" if c.get('seller_contact') else ""), size=8.3); sy -= 4*mm

        div_y = y - seller_block_h
        cv.line(left, div_y, rx, div_y)

        by = div_y - 4.2*mm
        text(left + 2*mm, by, "Details of Receiver (Ship to)", size=8.3); by -= 4.3*mm
        buyer_name_h = text(left + 2*mm, by, c.get("buyer_name","") or "", font=FONT_B, size=9.5,
                             max_w=left_w - 4*mm, leading=4*mm)
        by -= max(buyer_name_h, 4*mm) + 0.8*mm
        for ln in buyer_addr_lines:
            consumed = text(left + 2*mm, by, ln, size=8.3, max_w=left_w - 4*mm, leading=3.8*mm)
            by -= max(consumed, 3.8*mm)
        if c.get("buyer_gstin"):
            text(left + 2*mm, by, f"GSTIN/UIN: {c['buyer_gstin']}", size=8.3); by -= 4*mm
        if c.get("buyer_state"):
            text(left + 2*mm, by, f"State Name: {c['buyer_state']}, Code: {c.get('buyer_state_code','')}", size=8.3); by -= 4*mm
        if c.get("place_of_supply"):
            text(left + 2*mm, by, f"Place of Supply: {c['place_of_supply']}", size=8.3); by -= 4*mm

        # Right meta grid: 6 rows x 2 cols + terms-of-delivery row
        meta_rows = [
            ("Invoice No.", c.get("challan_no","")), ("Dated", c.get("challan_date","")),
            ("Delivery Note", c.get("delivery_note","")), ("Mode/Terms of Payment", c.get("mode_of_payment","")),
            ("Reference No. & Date.", c.get("reference_no","")), ("Other References", c.get("other_references","")),
            ("Buyer's Order No.", c.get("buyers_order_no","")), ("Dated", c.get("buyers_order_date","")),
            ("Dispatch Doc No.", c.get("dispatch_doc_no","")), ("Delivery Note Date", c.get("delivery_note_date","")),
            ("Dispatched through", c.get("dispatched_through","")), ("Destination", c.get("destination","")),
        ]
        terms_h = 8.5*mm + 4.5*mm
        meta_row_h = (header_h - terms_h) / 6.0
        for i in range(6):
            ry_top = y - i*meta_row_h
            if i > 0:
                cv.line(rx, ry_top, right, ry_top)
            cv.line(mid, ry_top - meta_row_h, mid, ry_top)
            lbl1, val1 = meta_rows[i*2]
            lbl2, val2 = meta_rows[i*2+1]
            text(rx + 1.5*mm, ry_top - 3.3*mm, lbl1, size=7.6)
            text(rx + 1.5*mm, ry_top - 7.2*mm, val1, font=FONT_B, size=8.3, max_w=mid - rx - 3*mm)
            text(mid + 1.5*mm, ry_top - 3.3*mm, lbl2, size=7.6)
            text(mid + 1.5*mm, ry_top - 7.2*mm, val2, font=FONT_B, size=8.3, max_w=right - mid - 3*mm)
        terms_top = y - 6*meta_row_h
        cv.line(rx, terms_top, right, terms_top)
        text(rx + 1.5*mm, terms_top - 4.2*mm, "Terms of Delivery", size=7.8)
        text(rx + 1.5*mm, terms_top - 8.5*mm, c.get("terms_of_delivery","") or "", size=8.3, max_w=right_w - 3*mm)

        return y - header_h

    def draw_table_header(y):
        cv.setLineWidth(1)
        cv.rect(left, y - tbl_hdr_h, box_w, tbl_hdr_h)
        x = left
        for col in cols:
            if x > left:
                cv.line(x, y - tbl_hdr_h, x, y)
            lines = headers[col].split("\n")
            ly = y - 3.6*mm
            for ln in lines:
                text(x + 1.5*mm, ly, ln, font=FONT_B, size=8.2)
                ly -= 3.6*mm
            x += col_w[col]
        return y - tbl_hdr_h

    def draw_footer(y):
        """Declaration / signatory / jurisdiction strip. Returns new y."""
        footer_h = 22*mm
        cv.setLineWidth(1)
        cv.rect(left, y - footer_h, box_w, footer_h)
        fx = left + box_w*0.55
        cv.line(fx, y - footer_h, fx, y)
        text(left + 2*mm, y - 4*mm, "Declaration", font=FONT_B, size=8)
        cv.setLineWidth(0.6)
        cv.line(left + 2*mm, y - 5*mm, left + 22*mm, y - 5*mm)
        cv.setLineWidth(1)
        text(left + 2*mm, y - 8.5*mm, c.get("declaration","") or "", size=7.8, max_w=fx - left - 4*mm, leading=3.6*mm)

        sig_w = right - fx - 4*mm
        sig_cx = fx + (right - fx) / 2
        sig_text = f"for {c.get('seller_name','')}"
        sig_lines = simpleSplit(sig_text, FONT_B, 8.5, sig_w)
        sig_y = y - 4.5*mm
        cv.setFont(FONT_B, 8.5)
        for ln in sig_lines:
            cv.drawCentredString(sig_cx, sig_y, ln)
            sig_y -= 3.6*mm
        cv.setFont(FONT, 8.5)
        cv.drawCentredString(sig_cx, y - footer_h + 4.5*mm, "Authorised Signatory")
        y -= footer_h

        jur_h = 7*mm
        cv.rect(left, y - jur_h, box_w, jur_h)
        cv.setFont(FONT, 9)
        cv.drawCentredString(W/2, y - jur_h/2 - 1.2*mm, c.get("jurisdiction","") or "")
        y -= jur_h
        return y

    # ---- Pre-measure every item row's height (may need multiple lines) ----
    item_row_heights = []
    for it in items:
        n_lines = measure_lines(it.get("description",""), FONT, 8.5, desc_w)
        item_row_heights.append(max(base_row_h, n_lines * line_h + 3.4*mm))

    footer_reserve_first_page = 22*mm + 7*mm  # declaration + jurisdiction, only reserved on the last page

    # ---- Page 1: title + header block + start of items table ----
    page_num = 1
    y = top
    y = draw_title(y)
    y = draw_header_block(y)
    y = draw_table_header(y)
    table_top_of_page = y

    idx = 0
    n_items = len(items)
    min_blank_rows_total = max(16 - n_items, 0)  # keep a similar "blank rows" feel to the original on short lists
    blanks_drawn = 0

    while True:
        # How much vertical room is left on this page for item rows?
        is_last_chunk_guess = False
        avail_h = (y - bottom_limit) - footer_reserve_first_page
        rows_drawn_this_page = []

        while idx < n_items:
            rh = item_row_heights[idx]
            if rh > avail_h:
                break
            rows_drawn_this_page.append((idx, rh))
            avail_h -= rh
            idx += 1

        # If nothing fit at all (shouldn't normally happen), force at least one row to avoid an infinite loop
        if not rows_drawn_this_page and idx < n_items:
            rows_drawn_this_page.append((idx, item_row_heights[idx]))
            avail_h -= item_row_heights[idx]
            idx += 1

        all_items_done = idx >= n_items

        # On the page that finishes all items, also pad with blank rows (like the original blank-row look)
        blanks_this_page = 0
        if all_items_done:
            remaining_blanks = max(min_blank_rows_total - blanks_drawn, 0)
            while remaining_blanks > 0 and avail_h >= base_row_h:
                blanks_this_page += 1
                remaining_blanks -= 1
                avail_h -= base_row_h
            blanks_drawn += blanks_this_page

        rows_h_total = sum(rh for _, rh in rows_drawn_this_page) + blanks_this_page * base_row_h
        table_bottom = table_top_of_page - rows_h_total

        # Draw table outer rect + column separators for this page's chunk
        cv.setLineWidth(1)
        cv.rect(left, table_bottom, box_w, table_top_of_page - table_bottom)
        x = left
        for col in cols:
            if x > left:
                cv.line(x, table_bottom, x, table_top_of_page)
            x += col_w[col]

        # Row gridlines + cell text
        cv.setLineWidth(0.4)
        cv.setStrokeColor(colors.Color(0.75, 0.75, 0.75))
        ry = table_top_of_page
        row_tops = []
        for _, rh in rows_drawn_this_page:
            row_tops.append(ry)
            ry -= rh
            cv.line(left, ry, right, ry)
        for _ in range(blanks_this_page):
            ry -= base_row_h
            cv.line(left, ry, right, ry)
        cv.setStrokeColor(colors.black)
        cv.setLineWidth(1)

        for (item_idx, rh), ry_top in zip(rows_drawn_this_page, row_tops):
            it = items[item_idx]
            x = left
            text(x + col_w["sl"]/2 - 1.5*mm, ry_top - 4.6*mm, str(item_idx+1), size=8.5)
            x += col_w["sl"]
            text(x + 1.5*mm, ry_top - 4.6*mm, it.get("description",""), size=8.5, max_w=desc_w, leading=line_h)
            x += col_w["desc"]
            cv.drawCentredString(x + col_w["hsn"]/2, ry_top - 4.6*mm, it.get("hsn","") or "")
            x += col_w["hsn"]
            cv.drawCentredString(x + col_w["qty"]/2, ry_top - 4.6*mm, it.get("qty","") or "")
            x += col_w["qty"]
            cv.drawCentredString(x + col_w["disc"]/2, ry_top - 4.6*mm, it.get("disc","") or "")
            x += col_w["disc"]
            amt = it.get("amount","") or ""
            cv.setFont(FONT, 8.5)
            cv.drawRightString(right - 2*mm, ry_top - 4.6*mm, amt)

        y = table_bottom

        if all_items_done:
            y = draw_footer(y)
            break
        else:
            # More items remain: start a new page with a repeated table header
            cv.showPage()
            page_num += 1
            y = top
            y = draw_table_header(y)
            table_top_of_page = y

    cv.save()
    buf.seek(0)
    return buf


@app.route("/challan/<int:challan_id>/pdf")
def challan_pdf(challan_id):
    if not logged_in(): return redirect(url_for("login"))
    if not has_perm("can_challan"): return redirect(url_for("no_access"))
    import json
    from flask import send_file
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM challans WHERE id=%s", (challan_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        return redirect(url_for("challan_list"))
    c = dict(row)
    try:
        items = json.loads(c["items"]) if c["items"] else []
    except Exception:
        items = []
    pdf_buf = _build_challan_pdf(c, items)
    fname = (c.get("challan_no") or f"challan_{challan_id}").replace("/", "-") + ".pdf"
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=False, download_name=fname)


# ══════════════════════════════════════════
#  ADVANCED EMPLOYEE MANAGEMENT — DEPARTMENTS + HR PROFILES
# ══════════════════════════════════════════
def init_employee_management_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id              SERIAL PRIMARY KEY,
            name            TEXT UNIQUE NOT NULL,
            description     TEXT,
            head_emp_code   TEXT REFERENCES users(emp_code) ON DELETE SET NULL,
            is_active       BOOLEAN DEFAULT TRUE,
            created_at      TEXT,
            created_by      TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employee_profiles (
            emp_code                TEXT PRIMARY KEY REFERENCES users(emp_code) ON DELETE CASCADE,
            position                TEXT,
            phone                   TEXT,
            personal_email          TEXT,
            dob                     TEXT,
            gender                  TEXT,
            blood_group             TEXT,
            address                 TEXT,
            emergency_contact_name  TEXT,
            emergency_contact_phone TEXT,
            id_proof_type           TEXT,
            id_proof_number         TEXT,
            joining_date            TEXT,
            notes                   TEXT,
            updated_at              TEXT,
            updated_by              TEXT
        )
    """)
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL")
    cur.execute("ALTER TABLE supervisor_permissions ADD COLUMN IF NOT EXISTS can_add_employees BOOLEAN DEFAULT FALSE")
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_employee_management_db()
        print("✅ Department + Employee HR profile tables ready")
    except Exception as e:
        print(f"⚠️ Employee management init error: {e}")


# ══════════════════════════════════════════
#  ROUTES — DEPARTMENTS (SUPER ADMIN)
# ══════════════════════════════════════════
@app.route("/manager/departments")
def manage_departments():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT d.*, h.name AS head_name,
               (SELECT COUNT(*) FROM users u WHERE u.department_id = d.id AND u.is_active = TRUE) AS member_count
        FROM departments d
        LEFT JOIN users h ON h.emp_code = d.head_emp_code
        ORDER BY d.name
    """)
    departments = cur.fetchall()
    cur.execute("SELECT emp_code, name FROM users WHERE is_active=TRUE ORDER BY name")
    employee_choices = cur.fetchall()
    cur.close(); conn.close()
    return render_template("manage_departments.html",
        departments=departments, employee_choices=employee_choices,
        flash=request.args.get("flash",""), flash_type=request.args.get("flash_type","success"))


@app.route("/manager/departments/create", methods=["POST"])
def create_department():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    name = request.form.get("name","").strip()
    description = request.form.get("description","").strip()
    head_emp_code = request.form.get("head_emp_code","").strip() or None
    if not name:
        return redirect(url_for("manage_departments", flash="Department name is required.", flash_type="error"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO departments (name, description, head_emp_code, created_at, created_by)
            VALUES (%s,%s,%s,%s,%s)
        """, (name, description, head_emp_code, now, session.get("name","manager")))
        conn.commit()
        return redirect(url_for("manage_departments", flash=f"Department '{name}' created.", flash_type="success"))
    except Exception as e:
        conn.rollback()
        return redirect(url_for("manage_departments", flash=f"Error: a department with this name may already exist.", flash_type="error"))
    finally:
        cur.close(); conn.close()


@app.route("/manager/departments/<int:dept_id>/update", methods=["POST"])
def update_department(dept_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    name = request.form.get("name","").strip()
    description = request.form.get("description","").strip()
    head_emp_code = request.form.get("head_emp_code","").strip() or None
    if not name:
        return redirect(url_for("manage_departments", flash="Department name is required.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE departments SET name=%s, description=%s, head_emp_code=%s
            WHERE id=%s
        """, (name, description, head_emp_code, dept_id))
        conn.commit()
        return redirect(url_for("manage_departments", flash=f"Department updated.", flash_type="success"))
    except Exception as e:
        conn.rollback()
        return redirect(url_for("manage_departments", flash=f"Error updating department.", flash_type="error"))
    finally:
        cur.close(); conn.close()


@app.route("/manager/departments/<int:dept_id>/toggle-active", methods=["POST"])
def toggle_department_active(dept_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT is_active FROM departments WHERE id=%s", (dept_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return redirect(url_for("manage_departments", flash="Department not found.", flash_type="error"))
    new_status = not row["is_active"]
    cur.execute("UPDATE departments SET is_active=%s WHERE id=%s", (new_status, dept_id))
    conn.commit(); cur.close(); conn.close()
    verb = "reactivated" if new_status else "archived"
    return redirect(url_for("manage_departments", flash=f"Department {verb}.", flash_type="success"))


# ══════════════════════════════════════════
#  ROUTES — EMPLOYEE PROFILE DETAIL (SUPER ADMIN)
# ══════════════════════════════════════════
@app.route("/manager/employee-profiles/<emp_code>")
def employee_profile_detail(emp_code):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT u.*, d.name AS department_name
        FROM users u
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.emp_code = %s
    """, (emp_code,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        return redirect(url_for("employee_profiles", flash="Employee not found.", flash_type="error"))
    cur.execute("SELECT * FROM employee_profiles WHERE emp_code=%s", (emp_code,))
    profile = cur.fetchone()
    cur.execute("SELECT id, name FROM departments WHERE is_active=TRUE ORDER BY name")
    departments = cur.fetchall()
    # Supervisors currently reporting-to relationships are inferred ad hoc from
    # reports/jobs elsewhere in this app (no fixed org-chart field), so here we
    # just show whichever supervisor(s) this employee has most recently logged
    # work reports against, as a read-only hint.
    cur.execute("""
        SELECT DISTINCT supervisor_name FROM reports
        WHERE emp_code=%s AND supervisor_name IS NOT NULL AND supervisor_name != ''
        ORDER BY supervisor_name LIMIT 5
    """, (emp_code,))
    recent_supervisors = [r["supervisor_name"] for r in cur.fetchall()]
    cur.close(); conn.close()
    return render_template("employee_profile_detail.html",
        user=user, profile=profile or {}, departments=departments,
        recent_supervisors=recent_supervisors,
        flash=request.args.get("flash",""), flash_type=request.args.get("flash_type","success"))


@app.route("/manager/employee-profiles/<emp_code>/update", methods=["POST"])
def update_employee_profile(emp_code):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE emp_code=%s", (emp_code,))
    if not cur.fetchone():
        cur.close(); conn.close()
        return redirect(url_for("employee_profiles", flash="Employee not found.", flash_type="error"))

    department_id = request.form.get("department_id","").strip() or None
    cur.execute("UPDATE users SET department_id=%s WHERE emp_code=%s", (department_id, emp_code))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields = {
        "position":                request.form.get("position","").strip(),
        "phone":                   request.form.get("phone","").strip(),
        "personal_email":          request.form.get("personal_email","").strip(),
        "dob":                     request.form.get("dob","").strip(),
        "gender":                  request.form.get("gender","").strip(),
        "blood_group":             request.form.get("blood_group","").strip(),
        "address":                 request.form.get("address","").strip(),
        "emergency_contact_name":  request.form.get("emergency_contact_name","").strip(),
        "emergency_contact_phone": request.form.get("emergency_contact_phone","").strip(),
        "id_proof_type":           request.form.get("id_proof_type","").strip(),
        "id_proof_number":         request.form.get("id_proof_number","").strip(),
        "joining_date":            request.form.get("joining_date","").strip(),
        "notes":                   request.form.get("notes","").strip(),
    }
    cur.execute("""
        INSERT INTO employee_profiles
            (emp_code, position, phone, personal_email, dob, gender, blood_group,
             address, emergency_contact_name, emergency_contact_phone,
             id_proof_type, id_proof_number, joining_date, notes, updated_at, updated_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (emp_code) DO UPDATE SET
            position=%s, phone=%s, personal_email=%s, dob=%s, gender=%s, blood_group=%s,
            address=%s, emergency_contact_name=%s, emergency_contact_phone=%s,
            id_proof_type=%s, id_proof_number=%s, joining_date=%s, notes=%s,
            updated_at=%s, updated_by=%s
    """, (
        emp_code, fields["position"], fields["phone"], fields["personal_email"], fields["dob"],
        fields["gender"], fields["blood_group"], fields["address"],
        fields["emergency_contact_name"], fields["emergency_contact_phone"],
        fields["id_proof_type"], fields["id_proof_number"], fields["joining_date"], fields["notes"],
        now, session.get("name","Super Admin"),
        fields["position"], fields["phone"], fields["personal_email"], fields["dob"],
        fields["gender"], fields["blood_group"], fields["address"],
        fields["emergency_contact_name"], fields["emergency_contact_phone"],
        fields["id_proof_type"], fields["id_proof_number"], fields["joining_date"], fields["notes"],
        now, session.get("name","Super Admin"),
    ))
    conn.commit(); cur.close(); conn.close()
    refresh_employees()
    return redirect(url_for("employee_profile_detail", emp_code=emp_code,
        flash="Profile updated.", flash_type="success"))


# ══════════════════════════════════════════
#  ROUTES — SUPERVISOR: ADD EMPLOYEE
# ══════════════════════════════════════════
@app.route("/supervisor/add-employee", methods=["GET", "POST"])
def supervisor_add_employee():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_add_employees"): return redirect(url_for("no_access"))

    conn = get_db(); cur = conn.cursor()

    if request.method == "POST":
        emp_code = request.form.get("emp_code", "").strip()
        name     = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().lower()
        company  = request.form.get("company", "").strip()
        password = request.form.get("password", "").strip()
        department_id = request.form.get("department_id", "").strip() or None
        position = request.form.get("position", "").strip()
        # Supervisors can only create plain employees — no role escalation.
        can_work_report = "can_work_report" in request.form
        can_sales_visit = "can_sales_visit" in request.form
        can_my_jobs     = "can_my_jobs" in request.form
        can_ta          = "can_ta" in request.form
        can_support     = "can_support" in request.form

        if not emp_code or not name or not username or not password:
            cur.close(); conn.close()
            return redirect(url_for("supervisor_add_employee", flash="All fields are required.", flash_type="error"))
        if len(password) < 6:
            cur.close(); conn.close()
            return redirect(url_for("supervisor_add_employee", flash="Password must be at least 6 characters.", flash_type="error"))

        try:
            cur.execute("SELECT 1 FROM users WHERE emp_code=%s OR username=%s", (emp_code, username))
            if cur.fetchone():
                cur.close(); conn.close()
                return redirect(url_for("supervisor_add_employee", flash="Employee code or username already exists.", flash_type="error"))

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
                INSERT INTO users (emp_code, name, username, password_hash, company, department_id,
                                    is_active, user_role, can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support,
                                    created_at, created_by)
                VALUES (%s,%s,%s,%s,%s,%s, TRUE, 'employee', %s,%s,%s,%s,%s, %s,%s)
            """, (emp_code, name, username, hash_password(password), company, department_id,
                  can_work_report, can_sales_visit, can_my_jobs, can_ta, can_support,
                  now, session.get("name", "supervisor")))
            conn.commit()
            if position:
                cur.execute("""
                    INSERT INTO employee_profiles (emp_code, position, joining_date, updated_at, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
                """, (emp_code, position, now[:10], now, session.get("name","supervisor")))
                conn.commit()
            refresh_employees()
            cur.close(); conn.close()
            return redirect(url_for("supervisor_add_employee", flash=f"Employee '{name}' created.", flash_type="success"))
        except Exception as e:
            conn.rollback()
            cur.close(); conn.close()
            return redirect(url_for("supervisor_add_employee", flash=f"Error creating employee: {e}", flash_type="error"))

    cur.execute("SELECT id, name FROM departments WHERE is_active=TRUE ORDER BY name")
    department_choices = cur.fetchall()
    cur.close(); conn.close()
    return render_template("supervisor_add_employee.html",
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}),
        department_choices=department_choices,
        flash=request.args.get("flash",""), flash_type=request.args.get("flash_type","success"))


# ══════════════════════════════════════════
#  ROUTES — SUPERVISOR: JOBS / TA / SALES / SUPPORT / CLIENTS (view-only)
# ══════════════════════════════════════════
@app.route("/supervisor/jobs")
def supervisor_jobs():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_view_jobs"): return redirect(url_for("no_access"))
    f_status = request.args.get("status", "")
    f_search = request.args.get("search", "")
    conn = get_db(); cur = conn.cursor()
    query = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if f_status:
        query += " AND status=%s"; params.append(f_status)
    if f_search:
        query += " AND (job_title ILIKE %s OR company ILIKE %s OR location ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s]
    query += " ORDER BY id DESC LIMIT 200"
    cur.execute(query, params)
    jobs = cur.fetchall()
    cur.close(); conn.close()
    return render_template("supervisor_jobs.html",
        jobs=jobs, record_count=len(jobs), filters={"status": f_status, "search": f_search},
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))


@app.route("/supervisor/ta")
def supervisor_ta():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_view_ta"): return redirect(url_for("no_access"))
    can_approve = has_sup_perm("can_approve_ta")
    f_status   = request.args.get("status", "")
    f_approval = request.args.get("approval", "")
    f_search   = request.args.get("search", "")
    conn = get_db(); cur = conn.cursor()
    query = "SELECT * FROM ta_reports WHERE 1=1"
    params = []
    if f_status:
        query += " AND payment_status=%s"; params.append(f_status)
    if f_approval:
        query += " AND approval_status=%s"; params.append(f_approval)
    if f_search:
        query += " AND (from_place ILIKE %s OR to_place ILIKE %s OR description ILIKE %s OR emp_name ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s, s]
    query += " ORDER BY travel_date DESC, id DESC LIMIT 200"
    cur.execute(query, params)
    reports = cur.fetchall()
    cur.execute("SELECT COALESCE(SUM(expense_cost),0) AS total FROM ta_reports WHERE approval_status='Not Approved'")
    pending_approval_total = float(cur.fetchone()["total"])
    cur.close(); conn.close()
    filtered_total = sum(float(r["expense_cost"] or 0) for r in reports)
    return render_template("supervisor_ta.html",
        reports=reports, record_count=len(reports), filtered_total=filtered_total,
        pending_approval_total=pending_approval_total, can_approve=can_approve,
        filters={"status": f_status, "approval": f_approval, "search": f_search},
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))


@app.route("/supervisor/ta/<int:report_id>/approve", methods=["POST"])
def supervisor_ta_approve(report_id):
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_approve_ta"): return redirect(url_for("no_access"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE ta_reports SET approval_status='Approved', last_edited=%s, last_edited_by=%s
        WHERE id=%s
    """, (now, session.get("name", "Supervisor"), report_id))
    conn.commit()
    cur.execute("SELECT emp_code FROM ta_reports WHERE id=%s", (report_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row and row["emp_code"]:
        send_push(row["emp_code"], "Travel expense approved", "Your TA report was approved.", url="/ta-report")
    return redirect(url_for("supervisor_ta", **{k: v for k, v in request.args.items()}))


@app.route("/supervisor/ta/<int:report_id>/unapprove", methods=["POST"])
def supervisor_ta_unapprove(report_id):
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_approve_ta"): return redirect(url_for("no_access"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE ta_reports SET approval_status='Not Approved', last_edited=%s, last_edited_by=%s
        WHERE id=%s
    """, (now, session.get("name", "Supervisor"), report_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("supervisor_ta", **{k: v for k, v in request.args.items()}))


@app.route("/supervisor/sales")
def supervisor_sales():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_view_sales"): return redirect(url_for("no_access"))
    f_search = request.args.get("search", "")
    conn = get_db(); cur = conn.cursor()
    query = "SELECT * FROM sales_visits WHERE 1=1"
    params = []
    if f_search:
        query += " AND (client_name ILIKE %s OR salesperson_name ILIKE %s OR address ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s]
    query += " ORDER BY visit_date DESC, id DESC LIMIT 200"
    cur.execute(query, params)
    visits = cur.fetchall()
    cur.close(); conn.close()
    return render_template("supervisor_sales.html",
        visits=visits, record_count=len(visits), filters={"search": f_search},
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))


@app.route("/supervisor/support")
def supervisor_support():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_view_support"): return redirect(url_for("no_access"))
    f_status = request.args.get("status", "")
    f_search = request.args.get("search", "")
    conn = get_db(); cur = conn.cursor()
    query = "SELECT * FROM support_reports WHERE 1=1"
    params = []
    if f_status:
        query += " AND status=%s"; params.append(f_status)
    if f_search:
        query += " AND (company ILIKE %s OR contact_person ILIKE %s OR issue_description ILIKE %s)"
        s = f"%{f_search}%"; params += [s, s, s]
    query += " ORDER BY id DESC LIMIT 200"
    cur.execute(query, params)
    tickets = cur.fetchall()
    cur.close(); conn.close()
    return render_template("supervisor_support.html",
        tickets=tickets, record_count=len(tickets), filters={"status": f_status, "search": f_search},
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))


@app.route("/supervisor/clients")
def supervisor_clients():
    if not logged_in() or not is_supervisor(): return redirect(url_for("index"))
    if not has_sup_perm("can_view_clients"): return redirect(url_for("no_access"))
    f_search = request.args.get("search", "")
    conn = get_db(); cur = conn.cursor()
    query = """
        SELECT c.*,
               COUNT(DISTINCT v.id) AS visit_count,
               MAX(v.visit_date)    AS last_visit_date
        FROM companies c
        LEFT JOIN sales_visits v ON v.company_id = c.id
        WHERE 1=1
    """
    params = []
    if f_search:
        query += " AND c.name ILIKE %s"; params.append(f"%{f_search}%")
    query += " GROUP BY c.id ORDER BY c.name ASC"
    cur.execute(query, params)
    companies = cur.fetchall()
    cur.close(); conn.close()
    return render_template("supervisor_clients.html",
        companies=companies, record_count=len(companies), filters={"search": f_search},
        name=session.get("name", "Supervisor"),
        perms=session.get("perms", {}), sup_perms=session.get("sup_perms", {}))


# ══════════════════════════════════════════
#  WEB PUSH NOTIFICATIONS
# ══════════════════════════════════════════
# VAPID keypair: override via environment variables in production. The
# defaults below are a real, valid P-256 keypair generated for this app so
# push works out of the box; rotate them via env vars if you ever need to.
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY",  "BC9KKwi89ZHPXy2rK9D5AaLlX3MNSqDbigDuZkXl80lbKha_e6owhCmCd9xkbDwlM88tTNyg9P0Y8cwUPywTxk4")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "Gn60emJzZsLlUNxBUZoOi7EMw4Q_L-BmHoPAfviyqsk")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:admin@workreport.app")

try:
    from pywebpush import webpush, WebPushException
    _PUSH_AVAILABLE = True
except Exception:
    _PUSH_AVAILABLE = False

def init_push_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id          SERIAL PRIMARY KEY,
            emp_code    TEXT NOT NULL,
            endpoint    TEXT NOT NULL UNIQUE,
            p256dh      TEXT NOT NULL,
            auth        TEXT NOT NULL,
            created_at  TEXT
        )
    """)
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_push_db()
        print("✅ Push subscription table ready")
    except Exception as e:
        print(f"⚠️ Push DB init error: {e}")


def send_push(emp_code, title, body, url="/"):
    """Best-effort push to all of an employee's subscribed devices.
    Never raises — a failed/expired subscription is just removed and the
    rest of the app continues normally, since notifications are a nice-to-have
    and must never block or break the action that triggered them."""
    if not _PUSH_AVAILABLE:
        return
    import json as _json
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM push_subscriptions WHERE emp_code=%s", (emp_code,))
        subs = cur.fetchall()
        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub["endpoint"],
                        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                    },
                    data=_json.dumps({"title": title, "body": body, "url": url}),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
                )
            except WebPushException as e:
                # 404/410 = subscription expired or revoked by the browser; clean it up.
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):
                    cur.execute("DELETE FROM push_subscriptions WHERE id=%s", (sub["id"],))
                    conn.commit()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        cur.close(); conn.close()


@app.route("/push/vapid-public-key")
def push_vapid_public_key():
    if not logged_in(): return jsonify({"error": "not logged in"}), 401
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    if not logged_in(): return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "")
    keys = data.get("keys", {})
    p256dh = keys.get("p256dh", "")
    auth = keys.get("auth", "")
    if not endpoint or not p256dh or not auth:
        return jsonify({"ok": False, "error": "Incomplete subscription"}), 400
    emp_code = get_emp_code()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO push_subscriptions (emp_code, endpoint, p256dh, auth, created_at)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (endpoint) DO UPDATE SET emp_code=%s, p256dh=%s, auth=%s, created_at=%s
        """, (emp_code, endpoint, p256dh, auth, now, emp_code, p256dh, auth, now))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        cur.close(); conn.close()


@app.route("/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    if not logged_in(): return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "")
    if endpoint:
        conn = get_db(); cur = conn.cursor()
        cur.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (endpoint,))
        conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})


# ══════════════════════════════════════════
#  FORM BUILDER — SCHEMA + HELPERS
# ══════════════════════════════════════════
def init_form_builder_db():
    conn = get_db(); cur = conn.cursor()
    # Custom field definitions per form
    cur.execute("""
        CREATE TABLE IF NOT EXISTS form_fields (
            id              SERIAL PRIMARY KEY,
            form_type       TEXT NOT NULL,
            field_key       TEXT NOT NULL UNIQUE,
            label           TEXT NOT NULL,
            field_type      TEXT NOT NULL DEFAULT 'text',
            dropdown_opts   TEXT,
            is_required     BOOLEAN DEFAULT FALSE,
            sort_order      INT DEFAULT 0,
            is_active       BOOLEAN DEFAULT TRUE,
            created_by      TEXT,
            created_at      TEXT
        )
    """)
    # Which built-in fields are hidden per form
    cur.execute("""
        CREATE TABLE IF NOT EXISTS form_field_visibility (
            form_type   TEXT NOT NULL,
            field_key   TEXT NOT NULL,
            is_visible  BOOLEAN DEFAULT TRUE,
            PRIMARY KEY (form_type, field_key)
        )
    """)
    # Custom field values per submission
    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_field_values (
            id          SERIAL PRIMARY KEY,
            form_type   TEXT NOT NULL,
            record_id   INT  NOT NULL,
            field_id    INT  NOT NULL REFERENCES form_fields(id) ON DELETE CASCADE,
            value       TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS cfv_lookup ON custom_field_values(form_type, record_id)")
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_form_builder_db()
        print("✅ Form builder tables ready")
    except Exception as e:
        print(f"⚠️ Form builder init error: {e}")


def get_form_config(form_type):
    """Return (active_custom_fields, visibility_map) for a form type.
    visibility_map: {field_key: True/False} for all built-in fields.
    active_custom_fields: list of dicts for active custom fields, ordered by sort_order."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT * FROM form_fields
        WHERE form_type=%s AND is_active=TRUE
        ORDER BY sort_order, id
    """, (form_type,))
    custom_fields = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT field_key, is_visible FROM form_field_visibility
        WHERE form_type=%s
    """, (form_type,))
    visibility_map = {r["field_key"]: r["is_visible"] for r in cur.fetchall()}
    cur.close(); conn.close()
    return custom_fields, visibility_map


def save_custom_field_values(form_type, record_id, custom_fields, form_data):
    """Save submitted custom field values for a job or report."""
    if not custom_fields: return
    conn = get_db(); cur = conn.cursor()
    # Delete old values for this record first (handles edit case)
    cur.execute("DELETE FROM custom_field_values WHERE form_type=%s AND record_id=%s",
                (form_type, record_id))
    for f in custom_fields:
        val = form_data.get(f"cf_{f['id']}", "").strip()
        if val:
            cur.execute("""
                INSERT INTO custom_field_values (form_type, record_id, field_id, value)
                VALUES (%s,%s,%s,%s)
            """, (form_type, record_id, f["id"], val))
    conn.commit(); cur.close(); conn.close()


def load_custom_field_values(form_type, record_ids):
    """Return {record_id: {field_id: value}} for a list of record IDs."""
    if not record_ids: return {}
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT record_id, field_id, value FROM custom_field_values
        WHERE form_type=%s AND record_id = ANY(%s)
    """, (form_type, list(record_ids)))
    result = {}
    for r in cur.fetchall():
        result.setdefault(r["record_id"], {})[r["field_id"]] = r["value"]
    cur.close(); conn.close()
    return result


def field_is_visible(visibility_map, field_key, default=True):
    """True if a built-in field should be shown (defaults to visible if not configured)."""
    return visibility_map.get(field_key, default)


# ══════════════════════════════════════════
#  ROUTES — FORM BUILDER (MANAGER ONLY)
# ══════════════════════════════════════════
# Built-in fields that can be toggled visible/hidden per form type.
# 'locked' fields cannot be hidden because the system depends on them.
BUILTIN_FIELDS = {
    "job": [
        {"key": "job_title",       "label": "Job Title",       "locked": True},
        {"key": "job_description", "label": "Job Description",  "locked": False},
        {"key": "location",        "label": "Location",         "locked": False},
        {"key": "company",         "label": "Company / Client", "locked": False},
        {"key": "start_date",      "label": "Start Date",       "locked": False},
        {"key": "end_date",        "label": "End Date",         "locked": False},
        {"key": "status",          "label": "Status",           "locked": True},
    ],
    "report": [
        {"key": "work_type",   "label": "Work Type",       "locked": True},
        {"key": "client_name", "label": "Client Name",     "locked": False},
        {"key": "location",    "label": "Location",        "locked": False},
        {"key": "supervisor",  "label": "Supervisor",      "locked": False},
        {"key": "summary",     "label": "Job Details",     "locked": True},
        {"key": "remarks",     "label": "Remarks / Notes", "locked": False},
    ],
}


@app.route("/manager/form-builder")
def form_builder():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    result = {}
    for ft in ["job", "report"]:
        cur.execute("""
            SELECT * FROM form_fields WHERE form_type=%s ORDER BY sort_order, id
        """, (ft,))
        custom = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT field_key, is_visible FROM form_field_visibility WHERE form_type=%s", (ft,))
        vis = {r["field_key"]: r["is_visible"] for r in cur.fetchall()}
        result[ft] = {"custom": custom, "visibility": vis}
    cur.close(); conn.close()
    return render_template("form_builder.html",
        name=session.get("name",""),
        job_fields=result["job"]["custom"],
        report_fields=result["report"]["custom"],
        job_visibility=result["job"]["visibility"],
        report_visibility=result["report"]["visibility"],
        builtin_fields=BUILTIN_FIELDS,
        flash=request.args.get("flash",""),
        flash_type=request.args.get("flash_type","success"))


@app.route("/manager/form-builder/field/add", methods=["POST"])
def form_builder_add_field():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    form_type   = request.form.get("form_type","").strip()
    label       = request.form.get("label","").strip()
    field_type  = request.form.get("field_type","text").strip()
    dropdown_opts = request.form.get("dropdown_opts","").strip()
    is_required = "is_required" in request.form
    if not form_type or not label:
        return redirect(url_for("form_builder", flash="Label is required.", flash_type="error"))
    if form_type not in ("job","report"):
        return redirect(url_for("form_builder", flash="Invalid form type.", flash_type="error"))
    # Generate a unique field_key from label
    import re as _re
    field_key = "cf_" + _re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]
    field_key += f"_{form_type}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM form_fields WHERE form_type=%s", (form_type,))
        next_order = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO form_fields (form_type, field_key, label, field_type, dropdown_opts,
                                     is_required, sort_order, is_active, created_by, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)
        """, (form_type, field_key, label, field_type, dropdown_opts or None,
              is_required, next_order, session.get("name","Manager"), now))
        conn.commit()
        return redirect(url_for("form_builder", flash=f"Field '{label}' added.", flash_type="success"))
    except Exception as e:
        conn.rollback()
        return redirect(url_for("form_builder", flash=f"Error: {e}", flash_type="error"))
    finally:
        cur.close(); conn.close()


@app.route("/manager/form-builder/field/<int:field_id>/update", methods=["POST"])
def form_builder_update_field(field_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    label         = request.form.get("label","").strip()
    dropdown_opts = request.form.get("dropdown_opts","").strip()
    is_required   = "is_required" in request.form
    if not label:
        return redirect(url_for("form_builder", flash="Label is required.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE form_fields SET label=%s, dropdown_opts=%s, is_required=%s WHERE id=%s
    """, (label, dropdown_opts or None, is_required, field_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("form_builder", flash="Field updated.", flash_type="success"))


@app.route("/manager/form-builder/field/<int:field_id>/toggle", methods=["POST"])
def form_builder_toggle_field(field_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT is_active FROM form_fields WHERE id=%s", (field_id,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE form_fields SET is_active=%s WHERE id=%s",
                    (not row["is_active"], field_id))
        conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("form_builder", flash="Field updated.", flash_type="success"))


@app.route("/manager/form-builder/field/<int:field_id>/delete", methods=["POST"])
def form_builder_delete_field(field_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM custom_field_values WHERE field_id=%s", (field_id,))
    count = cur.fetchone()["c"]
    if count > 0:
        cur.close(); conn.close()
        return redirect(url_for("form_builder",
            flash=f"Cannot delete: {count} submission(s) have values for this field. Deactivate it instead.",
            flash_type="error"))
    cur.execute("DELETE FROM form_fields WHERE id=%s", (field_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("form_builder", flash="Field deleted.", flash_type="success"))


@app.route("/manager/form-builder/field/<int:field_id>/reorder", methods=["POST"])
def form_builder_reorder_field(field_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    direction = request.form.get("direction","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT form_type, sort_order FROM form_fields WHERE id=%s", (field_id,))
    row = cur.fetchone()
    if row:
        ft, so = row["form_type"], row["sort_order"]
        if direction == "up":
            cur.execute("""
                SELECT id FROM form_fields WHERE form_type=%s AND sort_order<%s
                ORDER BY sort_order DESC LIMIT 1
            """, (ft, so))
        else:
            cur.execute("""
                SELECT id FROM form_fields WHERE form_type=%s AND sort_order>%s
                ORDER BY sort_order ASC LIMIT 1
            """, (ft, so))
        other = cur.fetchone()
        if other:
            cur.execute("SELECT sort_order FROM form_fields WHERE id=%s", (other["id"],))
            other_so = cur.fetchone()["sort_order"]
            cur.execute("UPDATE form_fields SET sort_order=%s WHERE id=%s", (other_so, field_id))
            cur.execute("UPDATE form_fields SET sort_order=%s WHERE id=%s", (so, other["id"]))
            conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("form_builder"))


@app.route("/manager/form-builder/visibility", methods=["POST"])
def form_builder_visibility():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    form_type = request.form.get("form_type","")
    field_key = request.form.get("field_key","")
    is_visible = request.form.get("is_visible","1") == "1"
    if form_type not in ("job","report") or not field_key:
        return redirect(url_for("form_builder", flash="Invalid request.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO form_field_visibility (form_type, field_key, is_visible)
        VALUES (%s,%s,%s)
        ON CONFLICT (form_type, field_key) DO UPDATE SET is_visible=%s
    """, (form_type, field_key, is_visible, is_visible))
    conn.commit(); cur.close(); conn.close()
    verb = "shown" if is_visible else "hidden"
    return redirect(url_for("form_builder", flash=f"Field {verb}.", flash_type="success"))


# ══════════════════════════════════════════
#  ADMIN ACCOUNTS TABLE
# ══════════════════════════════════════════
def init_admin_accounts_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_accounts (
            id            SERIAL PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            name          TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_active     BOOLEAN DEFAULT TRUE,
            is_super      BOOLEAN DEFAULT FALSE,
            last_login    TEXT,
            created_at    TEXT,
            created_by    TEXT,
            notes         TEXT
        )
    """)
    # Seed default admin from legacy MANAGERS dict if table is empty
    cur.execute("SELECT COUNT(*) AS c FROM admin_accounts")
    if cur.fetchone()["c"] == 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for uname, info in MANAGERS.items():
            cur.execute("""
                INSERT INTO admin_accounts (username, name, password_hash, is_active, is_super, created_at, created_by)
                VALUES (%s, %s, %s, TRUE, TRUE, %s, 'system')
                ON CONFLICT (username) DO NOTHING
            """, (uname, info["name"], hash_password(info["password"]), now))
    conn.commit(); cur.close(); conn.close()

with app.app_context():
    try:
        init_admin_accounts_db()
        print("✅ Admin accounts table ready")
    except Exception as e:
        print(f"⚠️ Admin accounts DB init error: {e}")

def get_admin_by_username(username):
    """Fetch admin account row by username."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM admin_accounts WHERE username=%s", (username,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row



# ══════════════════════════════════════════
#  ROUTES — SUPER ADMIN ACCOUNT MANAGEMENT
# ══════════════════════════════════════════

@app.route("/manager/admins")
def manage_admins():
    """List and manage all super admin accounts."""
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM admin_accounts ORDER BY is_super DESC, is_active DESC, name ASC")
    admins = cur.fetchall()
    cur.close(); conn.close()
    return render_template("manage_admins.html",
        name=session.get("name",""),
        admins=admins,
        current_admin_id=session.get("admin_id"),
        flash=request.args.get("flash",""),
        flash_type=request.args.get("flash_type","success"),
        temp_password=request.args.get("temp_password",""),
        temp_password_for=request.args.get("temp_password_for",""),
    )

@app.route("/manager/admins/create", methods=["POST"])
def create_admin():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    username = request.form.get("username","").strip().lower()
    name     = request.form.get("name","").strip()
    password = request.form.get("password","").strip()
    is_super = "is_super" in request.form
    notes    = request.form.get("notes","").strip()
    if not username or not name or not password:
        return redirect(url_for("manage_admins", flash="All fields required.", flash_type="error"))
    if len(password) < 6:
        return redirect(url_for("manage_admins", flash="Password must be at least 6 characters.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            INSERT INTO admin_accounts (username, name, password_hash, is_active, is_super, created_at, created_by, notes)
            VALUES (%s,%s,%s,TRUE,%s,%s,%s,%s)
        """, (username, name, hash_password(password), is_super, now, session.get("name","Super Admin"), notes))
        conn.commit()
        return redirect(url_for("manage_admins", flash=f"Admin '{name}' created.", flash_type="success"))
    except Exception as e:
        conn.rollback()
        return redirect(url_for("manage_admins", flash=f"Error: {e}", flash_type="error"))
    finally:
        cur.close(); conn.close()

@app.route("/manager/admins/<int:admin_id>/reset-password", methods=["POST"])
def reset_admin_password(admin_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    custom_pw = request.form.get("new_password","").strip()
    new_pw = custom_pw if custom_pw else generate_temp_password()
    if len(new_pw) < 6:
        return redirect(url_for("manage_admins", flash="Password too short.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name FROM admin_accounts WHERE id=%s", (admin_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return redirect(url_for("manage_admins", flash="Admin not found.", flash_type="error"))
    cur.execute("UPDATE admin_accounts SET password_hash=%s WHERE id=%s", (hash_password(new_pw), admin_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manage_admins",
        flash=f"Password reset for {row['name']}.", flash_type="success",
        temp_password=new_pw, temp_password_for=row["name"]))

@app.route("/manager/admins/<int:admin_id>/toggle-active", methods=["POST"])
def toggle_admin_active(admin_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    # Prevent self-deactivation
    if session.get("admin_id") == admin_id:
        return redirect(url_for("manage_admins", flash="You cannot deactivate your own account.", flash_type="error"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name, is_active FROM admin_accounts WHERE id=%s", (admin_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return redirect(url_for("manage_admins", flash="Admin not found.", flash_type="error"))
    new_status = not row["is_active"]
    cur.execute("UPDATE admin_accounts SET is_active=%s WHERE id=%s", (new_status, admin_id))
    conn.commit(); cur.close(); conn.close()
    verb = "reactivated" if new_status else "deactivated"
    return redirect(url_for("manage_admins", flash=f"{row['name']} has been {verb}.", flash_type="success"))

@app.route("/manager/admins/<int:admin_id>/toggle-super", methods=["POST"])
def toggle_admin_super(admin_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name, is_super FROM admin_accounts WHERE id=%s", (admin_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return redirect(url_for("manage_admins", flash="Admin not found.", flash_type="error"))
    cur.execute("UPDATE admin_accounts SET is_super=%s WHERE id=%s", (not row["is_super"], admin_id))
    conn.commit(); cur.close(); conn.close()
    label = "granted Super privileges" if not row["is_super"] else "removed Super privileges"
    return redirect(url_for("manage_admins", flash=f"{row['name']}: {label}.", flash_type="success"))

@app.route("/manager/admins/<int:admin_id>/update-info", methods=["POST"])
def update_admin_info(admin_id):
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    name  = request.form.get("name","").strip()
    notes = request.form.get("notes","").strip()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE admin_accounts SET name=COALESCE(NULLIF(%s,''),name), notes=%s WHERE id=%s",
                (name, notes, admin_id))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("manage_admins", flash="Admin info updated.", flash_type="success"))


# ══════════════════════════════════════════════════════════════
#  DATA DELETE MANAGER  (Super Admin only)
# ══════════════════════════════════════════════════════════════

@app.route("/manager/delete-manager")
def delete_manager():
    if not logged_in() or not is_manager(): return redirect(url_for("index"))
    return render_template("delete_manager.html",
                           name=session.get("name",""),
                           role=session.get("role",""),
                           perms=session.get("perms", {}),
                           sup_perms=session.get("sup_perms", {}),
                           active_page="delete_manager")

@app.route("/manager/delete-manager/counts")
def delete_manager_counts():
    """Return row counts for each deletable table — used by the page via fetch()."""
    if not logged_in() or not is_manager():
        return {"error": "Unauthorized"}, 403
    conn = get_db(); cur = conn.cursor()
    tables = {
        "reports":         "SELECT COUNT(*) AS c FROM reports",
        "jobs":            "SELECT COUNT(*) AS c FROM jobs",
        "job_edit_requests": "SELECT COUNT(*) AS c FROM job_edit_requests",
        "sales_visits":    "SELECT COUNT(*) AS c FROM sales_visits",
        "ta_reports":      "SELECT COUNT(*) AS c FROM ta_reports",
        "support_reports": "SELECT COUNT(*) AS c FROM support_reports",
        "challans":        "SELECT COUNT(*) AS c FROM challans",
        "stock_uploads":   "SELECT COUNT(*) AS c FROM stock_uploads",
        "stock_upload_log":"SELECT COUNT(*) AS c FROM stock_upload_log",
        "products":        "SELECT COUNT(*) AS c FROM products",
        "product_brands":  "SELECT COUNT(*) AS c FROM product_brands",
        "companies":       "SELECT COUNT(*) AS c FROM companies",
        "departments":     "SELECT COUNT(*) AS c FROM departments",
        "employee_profiles":"SELECT COUNT(*) AS c FROM employee_profiles",
        "users":           "SELECT COUNT(*) AS c FROM users",
    }
    counts = {}
    for key, sql in tables.items():
        try:
            cur.execute(sql)
            counts[key] = cur.fetchone()["c"]
        except Exception:
            counts[key] = "?"
    cur.close(); conn.close()
    return counts

@app.route("/manager/delete-manager/execute", methods=["POST"])
def delete_manager_execute():
    """
    Unified delete endpoint.
    JSON body:
      {
        "table":      "reports" | "jobs" | ... ,
        "mode":       "date_range" | "multi_select" | "all",
        "date_from":  "YYYY-MM-DD",   // for date_range
        "date_to":    "YYYY-MM-DD",   // for date_range
        "ids":        [1,2,3]         // for multi_select
      }
    """
    from flask import jsonify
    if not logged_in() or not is_manager():
        return jsonify({"error": "Unauthorized"}), 403

    data      = request.get_json(force=True, silent=True) or {}
    table     = data.get("table", "").strip()
    mode      = data.get("mode", "").strip()       # date_range | multi_select | all
    date_from = data.get("date_from", "").strip()
    date_to   = data.get("date_to",   "").strip()
    ids       = data.get("ids", [])

    # ── Allowed tables and their date columns ──────────────────
    ALLOWED = {
        "reports":           "date",
        "jobs":              "created_at",
        "job_edit_requests": "submitted_at",
        "sales_visits":      "visit_date",
        "ta_reports":        "travel_date",
        "support_reports":   "report_date",
        "challans":          "challan_date",
        "stock_uploads":     "uploaded_at",
        "stock_upload_log":  "uploaded_at",
        "products":          None,
        "product_brands":    None,
        "companies":         None,
        "departments":       None,
        "employee_profiles": None,
        "users":             "created_at",
    }

    if table not in ALLOWED:
        return jsonify({"error": f"Table '{table}' is not allowed."}), 400

    date_col = ALLOWED[table]

    conn = get_db(); cur = conn.cursor()
    deleted = 0

    try:
        if mode == "all":
            cur.execute(f"DELETE FROM {table}")
            deleted = cur.rowcount

        elif mode == "date_range":
            if not date_col:
                return jsonify({"error": f"Table '{table}' does not support date-range deletion."}), 400
            if not date_from or not date_to:
                return jsonify({"error": "date_from and date_to are required."}), 400
            cur.execute(
                f"DELETE FROM {table} WHERE {date_col}::date BETWEEN %s AND %s",
                (date_from, date_to)
            )
            deleted = cur.rowcount

        elif mode == "multi_select":
            if not ids or not isinstance(ids, list):
                return jsonify({"error": "ids list is required for multi_select."}), 400
            ids = [int(i) for i in ids]
            cur.execute(f"DELETE FROM {table} WHERE id = ANY(%s)", (ids,))
            deleted = cur.rowcount

        else:
            return jsonify({"error": "Invalid mode."}), 400

        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close(); conn.close()
        return jsonify({"error": str(e)}), 500

    cur.close(); conn.close()
    return jsonify({"deleted": deleted, "table": table, "mode": mode})


@app.route("/manager/delete-manager/preview")
def delete_manager_preview():
    """
    Returns sample rows so the user can see what they're about to delete.
    Query params: table, mode, date_from, date_to, ids (comma-separated)
    """
    from flask import jsonify
    if not logged_in() or not is_manager():
        return jsonify({"error": "Unauthorized"}), 403

    table     = request.args.get("table","").strip()
    mode      = request.args.get("mode","").strip()
    date_from = request.args.get("date_from","").strip()
    date_to   = request.args.get("date_to","").strip()
    ids_raw   = request.args.get("ids","").strip()

    ALLOWED_DATE = {
        "reports":           "date",
        "jobs":              "created_at",
        "job_edit_requests": "submitted_at",
        "sales_visits":      "visit_date",
        "ta_reports":        "travel_date",
        "support_reports":   "report_date",
        "challans":          "challan_date",
        "stock_uploads":     "uploaded_at",
        "stock_upload_log":  "uploaded_at",
        "products":          None,
        "product_brands":    None,
        "companies":         None,
        "departments":       None,
        "employee_profiles": None,
        "users":             "created_at",
    }
    if table not in ALLOWED_DATE:
        return jsonify({"error": "Invalid table."}), 400

    date_col = ALLOWED_DATE[table]
    conn = get_db(); cur = conn.cursor()
    rows = []

    try:
        if mode == "all":
            cur.execute(f"SELECT * FROM {table} LIMIT 5")
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
            total = cur.fetchone()["c"]
        elif mode == "date_range" and date_col:
            cur.execute(
                f"SELECT * FROM {table} WHERE {date_col}::date BETWEEN %s AND %s LIMIT 5",
                (date_from, date_to)
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE {date_col}::date BETWEEN %s AND %s",
                (date_from, date_to)
            )
            total = cur.fetchone()["c"]
        elif mode == "multi_select":
            ids = [int(i) for i in ids_raw.split(",") if i.strip().isdigit()]
            cur.execute(f"SELECT * FROM {table} WHERE id = ANY(%s) LIMIT 5", (ids,))
            rows = [dict(r) for r in cur.fetchall()]
            total = len(ids)
        else:
            total = 0
    except Exception as e:
        cur.close(); conn.close()
        return jsonify({"error": str(e)}), 500

    cur.close(); conn.close()
    # Convert to plain strings so JSON serialises fine
    rows = [{k: str(v) if v is not None else "" for k, v in r.items()} for r in rows]
    return jsonify({"rows": rows, "total": total})
