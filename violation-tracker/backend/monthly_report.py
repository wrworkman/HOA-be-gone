#!/usr/bin/env python3
"""
SBR Violation Tracker — Monthly Board Report
=============================================
Generates and emails two reports on the 1st of each month:

REPORT 1 — Board Report (HTML email):
  • Total violations by category (with counts)
  • Active open violations with photos
  • Resolved violations with before/after photos
  • Fine revenue collected this month
  • Repeat offender summary
  • Month-over-month trend (this month vs prior 3 months)

REPORT 2 — Resolved Violations Report (HTML email):
  • Every violation closed this month
  • Violation photo + resolution photo side by side
  • Closed date and days to resolve

Scheduled via GitHub Actions — see .github/workflows/monthly_report.yml
"""

import os, smtplib, logging
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from supabase import create_client

log = logging.getLogger("monthly_report")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GMAIL_FROM           = os.environ["GMAIL_FROM"]
GMAIL_APP_PWD        = os.environ["GMAIL_APP_PASSWORD"]
CRYSTAL_EMAIL        = os.environ.get("CRYSTAL_EMAIL", GMAIL_FROM)
BOARD_EMAIL          = os.environ.get("BOARD_EMAIL", CRYSTAL_EMAIL)

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
today         = date.today()
report_month  = today.replace(day=1) - relativedelta(months=1)  # last month
month_start   = report_month.isoformat()
month_end     = (report_month + relativedelta(months=1)).isoformat()
month_label   = report_month.strftime("%B %Y")

# ══════════════════════════════════════════════════════════
# DATA FETCHES
# ══════════════════════════════════════════════════════════
def fetch_violations_this_month():
    r = sb.table("violations") \
        .select("*") \
        .gte("created_at", month_start) \
        .lt("created_at", month_end) \
        .execute()
    return r.data or []


def fetch_resolved_this_month():
    r = sb.table("violations") \
        .select("*") \
        .eq("status", "resolved") \
        .gte("resolved_at", month_start) \
        .lt("resolved_at", month_end) \
        .order("resolved_at") \
        .execute()
    return r.data or []


def fetch_active_violations():
    r = sb.table("violations") \
        .select("*") \
        .in_("status", ["open", "pending_resolution"]) \
        .order("created_at") \
        .execute()
    return r.data or []


def fetch_monthly_counts_last_4():
    """Get violation counts for the past 4 months for trend chart."""
    counts = []
    for i in range(4, 0, -1):
        m_start = (today.replace(day=1) - relativedelta(months=i)).isoformat()
        m_end   = (today.replace(day=1) - relativedelta(months=i-1)).isoformat()
        m_label = (today.replace(day=1) - relativedelta(months=i)).strftime("%b '%y")
        r = sb.table("violations").select("id", count="exact") \
            .gte("created_at", m_start).lt("created_at", m_end).execute()
        counts.append((m_label, r.count or 0))
    return counts

# ══════════════════════════════════════════════════════════
# HTML HELPERS
# ══════════════════════════════════════════════════════════
def photo_cell(url, label="Photo", width=160) -> str:
    if not url:
        return f"<div style='width:{width}px;height:{width}px;background:#f3f4f6;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:12px;color:#9ca3af'>{label}</div>"
    return f"<img src='{url}' style='width:{width}px;height:{width}px;object-fit:cover;border-radius:6px;display:block'>"


def badge(status) -> str:
    colors = {
        "open":               ("fee2e2", "991b1b"),
        "pending_resolution": ("fef3c7", "92400e"),
        "resolved":           ("d1fae5", "065f46"),
    }
    bg, fg = colors.get(status, ("e5e7eb", "374151"))
    label = {"open":"Open","pending_resolution":"Pending","resolved":"Resolved"}.get(status, status)
    return f"<span style='background:#{bg};color:#{fg};padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700'>{label}</span>"


def bar_chart(counts) -> str:
    """Inline SVG bar chart."""
    if not counts:
        return ""
    max_v   = max(c for _, c in counts) or 1
    width   = 480
    h_bar   = 100
    padding = 40
    n       = len(counts)
    bar_w   = (width - padding * 2) // n - 8

    bars = ""
    for i, (label, count) in enumerate(counts):
        x   = padding + i * ((width - padding * 2) // n)
        bar_h = int(count / max_v * h_bar)
        y   = h_bar - bar_h
        bars += f"""
        <rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" fill="#1e40af" rx="3"/>
        <text x="{x + bar_w//2}" y="{y - 4}" text-anchor="middle" font-size="11" fill="#374151">{count}</text>
        <text x="{x + bar_w//2}" y="{h_bar + 14}" text-anchor="middle" font-size="11" fill="#6b7280">{label}</text>"""

    return f"""<svg width="{width}" height="{h_bar + 30}" style="display:block;margin:12px 0">{bars}</svg>"""

# ══════════════════════════════════════════════════════════
# BOARD REPORT
# ══════════════════════════════════════════════════════════
def build_board_report(this_month, resolved, active, trend_counts) -> str:
    # By category
    by_cat = defaultdict(int)
    for v in this_month:
        by_cat[v.get("category_id", "unknown")] += 1

    cat_labels = {
        "exterior_maintenance": "Exterior Maintenance",
        "landscaping":          "Landscaping",
        "structures":           "Structures",
        "vehicles_parking":     "Vehicles & Parking",
        "trash_containers":     "Trash & Containers",
        "recreation":           "Recreation",
        "general_conduct":      "General Conduct",
        "common_areas":         "Common Areas",
    }
    cat_rows = "".join(
        f"<tr><td style='padding:6px 12px'>{cat_labels.get(cat, cat)}</td><td style='padding:6px 12px;text-align:center;font-weight:700'>{cnt}</td></tr>"
        for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1])
    )

    # Fine revenue
    total_fines = sum(float(v.get("fine_amount") or 0) for v in this_month)
    collected   = sum(float(v.get("fine_amount") or 0) for v in resolved)

    # Repeat offenders
    addr_counts = defaultdict(int)
    for v in active:
        addr_counts[v.get("address","?")] += 1
    repeats = [(addr, cnt) for addr, cnt in sorted(addr_counts.items(), key=lambda x: -x[1]) if cnt > 1]
    repeat_rows = "".join(
        f"<tr><td style='padding:6px 12px'>{addr}</td><td style='padding:6px 12px;text-align:center;font-weight:700;color:#dc2626'>{cnt}</td></tr>"
        for addr, cnt in repeats[:10]
    ) or "<tr><td colspan='2' style='padding:8px 12px;color:#9ca3af'>No repeat offenders this month.</td></tr>"

    # Active violation snapshot
    active_rows = ""
    for v in active[:20]:
        active_rows += f"""<tr style="border-bottom:1px solid #f0f0f0">
          <td style="padding:6px 8px;font-family:monospace;font-size:11px">{v.get('violation_ref','—')}</td>
          <td style="padding:6px 8px;font-size:12px">{v.get('address','—')}</td>
          <td style="padding:6px 8px;font-size:12px">{v.get('violation_label','—')}</td>
          <td style="padding:6px 8px;text-align:center">{badge(v.get('status'))}</td>
          <td style="padding:6px 8px;text-align:center">Stage {v.get('stage','?')}</td>
          <td style="padding:6px 8px;font-size:11px">{v.get('deadline_date','—')}</td>
        </tr>"""
    if not active_rows:
        active_rows = "<tr><td colspan='6' style='padding:12px;color:#9ca3af;text-align:center'>No active violations.</td></tr>"

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:800px;margin:0 auto;color:#1f2937">

<div style="background:#1e40af;padding:24px;border-radius:8px 8px 0 0">
  <h1 style="color:white;margin:0;font-size:22px">Signal Butte Ranch HOA</h1>
  <h2 style="color:rgba(255,255,255,0.85);margin:6px 0 0;font-size:16px;font-weight:500">Monthly Violation Report — {month_label}</h2>
</div>
<div style="background:#f9fafb;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px">

  <!-- Summary cards -->
  <div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px">
    {_summary(f'New Violations', len(this_month), '#1e40af')}
    {_summary(f'Resolved', len(resolved), '#059669')}
    {_summary(f'Active Open', len(active), '#dc2626')}
    {_summary(f'Fines Issued', f'${total_fines:,.0f}', '#7c3aed')}
  </div>

  <!-- Violations by category -->
  <h3 style="font-size:15px;margin-bottom:10px;color:#374151">Violations by Category — {month_label}</h3>
  <table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:24px">
    <thead style="background:#f3f4f6">
      <tr><th style="padding:8px 12px;text-align:left">Category</th><th style="padding:8px 12px;text-align:center">Count</th></tr>
    </thead>
    <tbody>{cat_rows or "<tr><td colspan='2' style='padding:8px 12px;color:#9ca3af'>No violations.</td></tr>"}</tbody>
  </table>

  <!-- Trend chart -->
  <h3 style="font-size:15px;margin-bottom:8px;color:#374151">Monthly Trend (Last 4 Months)</h3>
  {bar_chart(trend_counts)}

  <!-- Repeat offenders -->
  <h3 style="font-size:15px;margin:24px 0 10px;color:#374151">Repeat Offenders (2+ Active Violations)</h3>
  <table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:24px">
    <thead style="background:#f3f4f6">
      <tr><th style="padding:8px 12px;text-align:left">Address</th><th style="padding:8px 12px;text-align:center">Active Violations</th></tr>
    </thead>
    <tbody>{repeat_rows}</tbody>
  </table>

  <!-- Active violations snapshot -->
  <h3 style="font-size:15px;margin-bottom:10px;color:#374151">Active Open Violations ({len(active)})</h3>
  <table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-bottom:24px">
    <thead style="background:#f3f4f6">
      <tr>
        <th style="padding:6px 8px;text-align:left">Ref</th>
        <th style="padding:6px 8px;text-align:left">Address</th>
        <th style="padding:6px 8px;text-align:left">Violation</th>
        <th style="padding:6px 8px;text-align:center">Status</th>
        <th style="padding:6px 8px;text-align:center">Stage</th>
        <th style="padding:6px 8px;text-align:left">Deadline</th>
      </tr>
    </thead>
    <tbody>{active_rows}</tbody>
  </table>

  <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">
  <p style="font-size:12px;color:#9ca3af">
    Signal Butte Ranch HOA · P.O. Box 98526, Phoenix, AZ 85038 · 480-648-4861<br>
    This report was generated automatically on {today.strftime('%B %d, %Y')}.
  </p>
</div>
</body></html>"""


def _summary(label, value, color) -> str:
    return f"""<div style="background:white;border:1px solid #e5e7eb;border-radius:8px;padding:14px 18px;min-width:140px;flex:1">
      <div style="font-size:26px;font-weight:800;color:{color}">{value}</div>
      <div style="font-size:12px;color:#6b7280;margin-top:2px">{label}</div>
    </div>"""

# ══════════════════════════════════════════════════════════
# RESOLVED VIOLATIONS REPORT (before/after photos)
# ══════════════════════════════════════════════════════════
def build_resolved_report(resolved) -> str:
    if not resolved:
        return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;max-width:700px;margin:0 auto;color:#1f2937">
<div style="background:#1e40af;padding:24px;border-radius:8px 8px 0 0">
  <h1 style="color:white;margin:0;font-size:22px">Signal Butte Ranch HOA</h1>
  <h2 style="color:rgba(255,255,255,0.85);font-weight:500;font-size:16px;margin:6px 0 0">Resolved Violations — {month_label}</h2>
</div>
<div style="padding:24px;border:1px solid #e5e7eb;border-top:none;color:#6b7280">
  No violations were resolved in {month_label}.
</div></body></html>"""

    cards = ""
    for v in resolved:
        created = v.get("created_at", "")[:10]
        resolved_dt = v.get("resolved_at", "")[:10]
        days = ""
        if created and resolved_dt:
            delta = (date.fromisoformat(resolved_dt) - date.fromisoformat(created)).days
            days = f"{delta} day{'s' if delta != 1 else ''} to resolve"

        cards += f"""
<div style="background:white;border:1px solid #e5e7eb;border-radius:12px;padding:18px;margin-bottom:16px">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;flex-wrap:wrap;gap:6px">
    <div>
      <div style="font-size:11px;color:#9ca3af;font-family:monospace">{v.get('violation_ref','—')}</div>
      <div style="font-size:16px;font-weight:700;margin-top:2px">{v.get('violation_label','—')}</div>
      <div style="font-size:13px;color:#6b7280">{v.get('address','—')}</div>
    </div>
    <div style="text-align:right">
      <span style="background:#d1fae5;color:#065f46;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700">Resolved</span>
      <div style="font-size:12px;color:#9ca3af;margin-top:4px">{days}</div>
    </div>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <div style="flex:1;min-width:140px">
      <div style="font-size:11px;color:#6b7280;margin-bottom:4px;font-weight:600">VIOLATION PHOTO</div>
      {photo_cell(v.get('photo_url'), 'No photo')}
    </div>
    <div style="flex:1;min-width:140px">
      <div style="font-size:11px;color:#6b7280;margin-bottom:4px;font-weight:600">RESOLUTION PHOTO</div>
      {photo_cell(v.get('resolution_photo'), 'No photo')}
    </div>
    <div style="flex:1;min-width:140px;font-size:12px;color:#374151">
      <div><strong>Stage:</strong> {v.get('stage','—')}</div>
      <div style="margin-top:4px"><strong>Logged:</strong> {created}</div>
      <div style="margin-top:4px"><strong>Resolved:</strong> {resolved_dt}</div>
      <div style="margin-top:4px"><strong>Fine:</strong> ${float(v.get('fine_amount') or 0):.2f}</div>
      <div style="margin-top:4px;color:#10b981"><strong>AI Verdict:</strong> {v.get('ai_verdict','manual')}</div>
    </div>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;color:#1f2937;background:#f0f4f8;padding:0 0 32px">
<div style="background:#1e40af;padding:24px;border-radius:8px 8px 0 0">
  <h1 style="color:white;margin:0;font-size:22px">Signal Butte Ranch HOA</h1>
  <h2 style="color:rgba(255,255,255,0.85);font-weight:500;font-size:16px;margin:6px 0 0">Resolved Violations — {month_label} ({len(resolved)} total)</h2>
</div>
<div style="padding:20px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;background:#f9fafb">
  {cards}
  <p style="font-size:12px;color:#9ca3af;text-align:center;margin-top:16px">
    Generated {today.strftime('%B %d, %Y')} · Signal Butte Ranch HOA
  </p>
</div>
</body></html>"""

# ══════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════
def send_report(to: str, subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"SBR HOA Reports <{GMAIL_FROM}>"
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_FROM, GMAIL_APP_PWD)
        smtp.sendmail(GMAIL_FROM, to, msg.as_string())
    log.info(f"Report sent to {to}: {subject}")

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def run():
    log.info(f"Building monthly reports for {month_label}…")

    this_month   = fetch_violations_this_month()
    resolved     = fetch_resolved_this_month()
    active       = fetch_active_violations()
    trend_counts = fetch_monthly_counts_last_4()

    log.info(f"  New violations: {len(this_month)}")
    log.info(f"  Resolved: {len(resolved)}")
    log.info(f"  Active: {len(active)}")

    # Board report → to board email (and Crystal)
    board_html = build_board_report(this_month, resolved, active, trend_counts)
    send_report(
        BOARD_EMAIL,
        f"SBR HOA Monthly Report — {month_label}",
        board_html,
    )

    # Resolved report → to Crystal only
    resolved_html = build_resolved_report(resolved)
    send_report(
        CRYSTAL_EMAIL,
        f"SBR HOA Resolved Violations — {month_label} ({len(resolved)})",
        resolved_html,
    )

    log.info("Monthly reports complete.")


if __name__ == "__main__":
    run()
