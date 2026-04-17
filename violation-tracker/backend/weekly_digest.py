#!/usr/bin/env python3
"""
SBR Violation Tracker — Weekly Digest (Monday mornings for Crystal)
====================================================================
Sends Crystal a summary email every Monday at 7 AM covering:
  • Violations awaiting her manual review (pending_resolution, no AI verdict)
  • Violations with deadlines expiring in the next 5 days
  • New violations submitted this past week
  • Stage 6–7 violations awaiting board approval

Scheduled via GitHub Actions — see .github/workflows/weekly_digest.yml
"""

import os, smtplib, logging
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from supabase import create_client

log = logging.getLogger("weekly_digest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GMAIL_FROM           = os.environ["GMAIL_FROM"]
GMAIL_APP_PWD        = os.environ["GMAIL_APP_PASSWORD"]
CRYSTAL_EMAIL        = os.environ.get("CRYSTAL_EMAIL", GMAIL_FROM)
PORTAL_URL           = os.environ.get("RESOLUTION_PORTAL_URL",
                                       "https://sbrhoa.github.io/violations/resolve")

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
today = date.today()
week_ago = today - timedelta(days=7)
in_5_days = today + timedelta(days=5)


def fetch_pending_review():
    """Violations homeowner submitted a photo for, awaiting Crystal's review."""
    r = sb.table("violations") \
        .select("violation_ref, address, violation_label, stage, resolution_photo, ai_verdict, ai_notes, deadline_date") \
        .eq("status", "pending_resolution") \
        .execute()
    return r.data or []


def fetch_approaching_deadlines():
    """Open violations with deadlines in the next 5 days."""
    r = sb.table("violations") \
        .select("violation_ref, address, violation_label, stage, deadline_date, fine_amount") \
        .eq("status", "open") \
        .eq("cascade_processed", True) \
        .lte("deadline_date", in_5_days.isoformat()) \
        .gte("deadline_date", today.isoformat()) \
        .order("deadline_date") \
        .execute()
    return r.data or []


def fetch_new_this_week():
    """Violations submitted in the past 7 days."""
    r = sb.table("violations") \
        .select("violation_ref, address, violation_label, stage, status, created_at, officer") \
        .gte("created_at", week_ago.isoformat()) \
        .order("created_at", desc=True) \
        .execute()
    return r.data or []


def fetch_board_approval_pending():
    """Violations awaiting board approval (stages 6–7)."""
    r = sb.table("violations") \
        .select("violation_ref, address, violation_label, stage, fine_amount, created_at") \
        .eq("board_approved", False) \
        .execute()
    return r.data or []


def violation_row(v, include_photo=False) -> str:
    deadline = v.get("deadline_date", "—")
    days_left = ""
    if deadline and deadline != "—":
        d = (date.fromisoformat(deadline) - today).days
        days_left = f" <span style='color:{'#dc2626' if d <= 3 else '#f59e0b'};font-weight:700'>({d}d left)</span>"

    photo_cell = ""
    if include_photo and v.get("resolution_photo"):
        photo_cell = f"<td style='padding:6px 12px'><a href='{v['resolution_photo']}'>📷 View</a></td>"

    portal = f"{PORTAL_URL}?id={v.get('violation_ref', '')}"
    return f"""
    <tr style="border-bottom:1px solid #f0f0f0">
      <td style="padding:8px 12px;font-family:monospace;font-size:12px"><a href="{portal}">{v.get('violation_ref','—')}</a></td>
      <td style="padding:8px 12px">{v.get('address','—')}</td>
      <td style="padding:8px 12px">{v.get('violation_label','—')}</td>
      <td style="padding:8px 12px;text-align:center">Stage {v.get('stage','?')}</td>
      <td style="padding:8px 12px">{deadline}{days_left}</td>
      {photo_cell}
    </tr>"""


def build_digest_html(pending, deadlines, new_vios, board_pending) -> str:
    def section(title, color, rows_html, count, extra_col_header=""):
        if count == 0:
            return f"""
            <h3 style="font-size:15px;color:{color};margin:24px 0 6px">✓ {title} — None</h3>
            <p style="color:#9ca3af;font-size:13px;margin:0">All clear.</p>"""
        photo_th = "<th style='padding:8px 12px'>Photo</th>" if extra_col_header else ""
        return f"""
        <h3 style="font-size:15px;color:{color};margin:24px 0 6px">⚠️ {title} ({count})</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
          <thead style="background:#f9fafb">
            <tr>
              <th style="padding:8px 12px;text-align:left">Ref</th>
              <th style="padding:8px 12px;text-align:left">Address</th>
              <th style="padding:8px 12px;text-align:left">Violation</th>
              <th style="padding:8px 12px;text-align:center">Stage</th>
              <th style="padding:8px 12px;text-align:left">Deadline</th>
              {photo_th}
            </tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>"""

    pending_rows = [violation_row(v, include_photo=True) for v in pending]
    deadline_rows= [violation_row(v) for v in deadlines]
    new_rows     = [violation_row(v) for v in new_vios]
    board_rows   = [violation_row(v) for v in board_pending]

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:800px;margin:0 auto;color:#1f2937">

<div style="background:#1e40af;padding:20px 24px;border-radius:8px 8px 0 0">
  <h1 style="color:white;margin:0;font-size:20px">SBR Weekly Violation Digest</h1>
  <p style="color:rgba(255,255,255,0.75);margin:4px 0 0;font-size:13px">Week ending {today.strftime('%B %d, %Y')} · Signal Butte Ranch HOA</p>
</div>

<div style="background:#f9fafb;padding:20px 24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px">

  <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">
    {summary_card('Pending Review', len(pending), '#f59e0b')}
    {summary_card('Deadlines ≤5 Days', len(deadlines), '#dc2626')}
    {summary_card('New This Week', len(new_vios), '#1e40af')}
    {summary_card('Board Approval Needed', len(board_pending), '#7c3aed')}
  </div>

  {section('Resolutions Awaiting Your Review', '#92400e', pending_rows, len(pending), 'Photo')}
  <p style="font-size:12px;color:#6b7280;margin-top:6px">
    These homeowners submitted a resolution photo. Click the photo link to view it and decide whether to close the violation.
  </p>

  {section('Deadlines Expiring Within 5 Days', '#dc2626', deadline_rows, len(deadlines))}

  {section('New Violations This Week', '#1e40af', new_rows, len(new_vios))}

  {section('Board Approval Needed (Stages 6–7)', '#7c3aed', board_rows, len(board_pending))}

  <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
  <p style="font-size:12px;color:#9ca3af">
    Signal Butte Ranch HOA · P.O. Box 98526, Phoenix, AZ 85038 · 480-648-4861<br>
    This is an automated weekly digest. Reply to this email with any questions.
  </p>
</div>
</body></html>"""


def summary_card(label, count, color) -> str:
    return f"""<div style="background:white;border:1px solid #e5e7eb;border-radius:8px;padding:14px 18px;min-width:150px;flex:1">
      <div style="font-size:28px;font-weight:800;color:{color}">{count}</div>
      <div style="font-size:12px;color:#6b7280;margin-top:2px">{label}</div>
    </div>"""


def send_digest(html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"SBR Weekly Violation Digest — {today.strftime('%B %d, %Y')}"
    msg["From"]    = f"SBR Violations <{GMAIL_FROM}>"
    msg["To"]      = CRYSTAL_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_FROM, GMAIL_APP_PWD)
        smtp.sendmail(GMAIL_FROM, CRYSTAL_EMAIL, msg.as_string())
    log.info(f"Weekly digest sent to {CRYSTAL_EMAIL}")


def run():
    log.info("Building weekly digest…")
    pending       = fetch_pending_review()
    deadlines     = fetch_approaching_deadlines()
    new_vios      = fetch_new_this_week()
    board_pending = fetch_board_approval_pending()

    log.info(f"  Pending review: {len(pending)}")
    log.info(f"  Approaching deadlines: {len(deadlines)}")
    log.info(f"  New this week: {len(new_vios)}")
    log.info(f"  Board approval needed: {len(board_pending)}")

    html = build_digest_html(pending, deadlines, new_vios, board_pending)
    send_digest(html)
    log.info("Done.")


if __name__ == "__main__":
    run()
