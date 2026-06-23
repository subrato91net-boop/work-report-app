"""
TA Bulk PDF Route
-----------------
Add this route to your main Flask app (app.py / main.py).

Usage: GET /ta-report/bulk-pdf?company=conneqtor&ids=1&ids=2&ids=3

company: 'conneqtor'  →  Conneqtor Technology Pvt. Ltd. (logo: conneqtor_logo.jpg)
         'imax'       →  IMAX Solutions                  (logo: imax_logo.jpg)
ids:      one or more TA record IDs (approved only)
"""

from flask import request, render_template, session, redirect, url_for
from datetime import date

# ── Paste this route into your main app.py ────────────────────────────────────

@app.route('/ta-report/bulk-pdf')
def ta_bulk_pdf():
    """Render a print-ready HTML page for one or more approved TA records."""
    if 'user' not in session:
        return redirect(url_for('login'))

    user      = session['user']          # username / employee identifier
    company   = request.args.get('company', '').strip()
    ids_raw   = request.args.getlist('ids')

    # Validate company
    allowed_companies = {'conneqtor', 'imax'}
    if company not in allowed_companies:
        return "Invalid company selected.", 400

    # Parse IDs
    try:
        ids = [int(i) for i in ids_raw if i.strip().isdigit()]
    except Exception:
        return "Invalid TA IDs.", 400

    if not ids:
        return "No TA records selected.", 400

    # ── Fetch records from DB ──────────────────────────────────────────────────
    # Replace this block with your actual DB query pattern.
    # Only return records owned by this user AND approved.
    #
    # Example using psycopg2 with your existing `get_db()` helper:
    #
    #   conn = get_db()
    #   cur  = conn.cursor()
    #   placeholders = ','.join(['%s'] * len(ids))
    #   cur.execute(f"""
    #       SELECT id, travel_date, from_place, to_place, travel_by,
    #              description, expense_cost, payment_status, approval_status
    #       FROM ta_reports
    #       WHERE id IN ({placeholders})
    #         AND username = %s
    #         AND approval_status = 'Approved'
    #       ORDER BY travel_date
    #   """, ids + [user])
    #   rows = cur.fetchall()
    #   columns = [d[0] for d in cur.description]
    #   records = [dict(zip(columns, row)) for row in rows]
    #   cur.close()
    #
    # ─── PLACEHOLDER (remove once wired to real DB) ───────────────────────────
    records = []          # replace with actual DB rows (list of dicts)
    employee_name = user  # replace with full name from DB if available
    department    = ''    # replace with department from DB if available
    # ─────────────────────────────────────────────────────────────────────────

    if not records:
        return "No approved TA records found for the selected IDs.", 404

    total_amount = sum(float(r.get('expense_cost') or 0) for r in records)

    # Period string (min date → max date)
    dates = sorted(str(r.get('travel_date', '')) for r in records if r.get('travel_date'))
    period = f"{dates[0]} to {dates[-1]}" if len(dates) > 1 else (dates[0] if dates else '—')

    return render_template(
        'ta_pdf_bulk.html',
        records        = records,
        employee_name  = employee_name,
        department     = department,
        company        = company,
        total_amount   = total_amount,
        period         = period,
        generated_date = date.today().strftime('%d %b %Y'),
    )
