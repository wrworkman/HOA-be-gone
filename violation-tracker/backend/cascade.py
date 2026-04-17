#!/usr/bin/env python3
"""
SBR Violation Tracker — Cascade Engine
=======================================
Runs every 30 minutes via GitHub Actions cron.
For each unprocessed violation in Supabase:
  1. Looks up owner in Buildium by address
  2. Determines stage (prior violation history)
  3. Enforces 20-day minimum between notices
  4. Posts note + fine to Buildium ledger
  5. Sends Twilio SMS to homeowner
  6. Sends Gmail email with violation photo
  7. Sends Lob physical letter (stage 2+)
  8. Marks processed in Supabase
  Stages 6–7: alerts Crystal for board approval instead of sending directly.

Required environment variables (set in GitHub Actions secrets):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  BUILDIUM_CLIENT_ID, BUILDIUM_CLIENT_SECRET
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
  GMAIL_FROM, GMAIL_APP_PASSWORD
  LOB_API_KEY
  CRYSTAL_EMAIL, CRYSTAL_PHONE (for board approval alerts)
  RESOLUTION_PORTAL_URL (e.g. https://sbrhoa.github.io/violations/resolve)
"""

import os, json, base64, requests, smtplib, logging
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cascade")

# ══════════════════════════════════════════════════════════
# CONFIG FROM ENVIRONMENT
# ══════════════════════════════════════════════════════════
SUPABASE_URL          = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]
BUILDIUM_CLIENT_ID    = os.environ["BUILDIUM_CLIENT_ID"]
BUILDIUM_CLIENT_SECRET= os.environ["BUILDIUM_CLIENT_SECRET"]
TWILIO_SID            = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN          = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM           = os.environ["TWILIO_FROM_NUMBER"]     # e.g. +14809990000
GMAIL_FROM            = os.environ["GMAIL_FROM"]
GMAIL_APP_PWD         = os.environ["GMAIL_APP_PASSWORD"]
LOB_KEY               = os.environ["LOB_API_KEY"]
CRYSTAL_EMAIL         = os.environ.get("CRYSTAL_EMAIL", GMAIL_FROM)
CRYSTAL_PHONE         = os.environ.get("CRYSTAL_PHONE", "")
PORTAL_URL            = os.environ.get("RESOLUTION_PORTAL_URL",
                                       "https://sbrhoa.github.io/violations/resolve")

HOA_NAME   = "Signal Butte Ranch Community Association"
HOA_PHONE  = "480-648-4861"
HOA_ADDR   = "P.O. Box 98526, Phoenix, AZ 85038-0526"
HOA_WEBSITE= "SignalButteRanch.com"
HOA_EMAIL  = "sbrneighbors@gmail.com"

MIN_DAYS_BETWEEN = 20   # DOC: minimum days between notices for same violation type

# Fine schedule keyed by stage number
FINE_SCHEDULE = {
    1: {"label":"1st Observation — Courtesy Notice",   "fine":0,   "letter":False, "certified":False, "board_approval":False},
    2: {"label":"2nd Observation — Formal Notice",     "fine":0,   "letter":True,  "certified":False, "board_approval":False},
    3: {"label":"3rd Observation — 1st Fine",          "fine":50,  "letter":True,  "certified":False, "board_approval":False},
    4: {"label":"4th Observation — 2nd Fine",          "fine":100, "letter":True,  "certified":False, "board_approval":False},
    5: {"label":"5th Observation — 3rd Fine",          "fine":150, "letter":True,  "certified":False, "board_approval":False},
    6: {"label":"6th Observation — Right to Cure",     "fine":55,  "letter":True,  "certified":True,  "board_approval":True},
    7: {"label":"7th Observation — HOA Cures at Owner Expense", "fine":200, "letter":True, "certified":True, "board_approval":True},
}

# ══════════════════════════════════════════════════════════
# SUPABASE CLIENT
# ══════════════════════════════════════════════════════════
sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ══════════════════════════════════════════════════════════
# BUILDIUM API
# ══════════════════════════════════════════════════════════
_buildium_token: dict = {}

def get_buildium_token() -> str:
    """Fetch or return cached Buildium OAuth2 access token."""
    global _buildium_token
    if _buildium_token.get("expires_at", datetime.min) > datetime.utcnow():
        return _buildium_token["access_token"]

    r = requests.post(
        "https://auth.buildium.com/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": BUILDIUM_CLIENT_ID,
            "client_secret": BUILDIUM_CLIENT_SECRET,
            "scope": "openid",
        },
    )
    r.raise_for_status()
    d = r.json()
    _buildium_token = {
        "access_token": d["access_token"],
        "expires_at": datetime.utcnow() + timedelta(seconds=d.get("expires_in", 3600) - 60),
    }
    return d["access_token"]


def buildium_get(path: str, params: dict = None) -> dict:
    token = get_buildium_token()
    r = requests.get(
        f"https://api.buildium.com/v1{path}",
        headers={"Authorization": f"Bearer {token}", "x-buildium-api-key": BUILDIUM_CLIENT_ID},
        params=params or {},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def buildium_post(path: str, payload: dict) -> dict:
    token = get_buildium_token()
    r = requests.post(
        f"https://api.buildium.com/v1{path}",
        headers={"Authorization": f"Bearer {token}", "x-buildium-api-key": BUILDIUM_CLIENT_ID,
                 "Content-Type": "application/json"},
        json=payload,
    )
    r.raise_for_status()
    return r.json()


def find_buildium_unit(address: str) -> dict | None:
    """Search Buildium for a rental/association unit by address. Returns unit info or None."""
    # Try association units first (HOA context)
    results = buildium_get("/associations/units", {"Address": address.split(",")[0].strip()})
    if results and len(results) > 0:
        return results[0]
    # Fallback: rental units
    results = buildium_get("/rentals/units", {"Address": address.split(",")[0].strip()})
    if results and len(results) > 0:
        return results[0]
    return None


def find_buildium_owner(unit: dict) -> dict | None:
    """Find the current owner/resident of a unit."""
    unit_id = unit.get("Id")
    if not unit_id:
        return None
    # Get ownership history / current occupant
    tenants = buildium_get(f"/associations/units/{unit_id}/ownership")
    if tenants:
        return tenants[0] if isinstance(tenants, list) else tenants
    return None


def get_owner_contact(owner_data: dict) -> dict:
    """Extract contact info from Buildium owner record."""
    if not owner_data:
        return {}
    person = owner_data.get("PrimaryContact") or owner_data
    return {
        "name":     f"{person.get('FirstName','')} {person.get('LastName','')}".strip(),
        "first":    person.get("FirstName", "Homeowner"),
        "last":     person.get("LastName", ""),
        "email":    person.get("Email", ""),
        "phone":    person.get("PhoneNumbers", [{}])[0].get("PhoneNumber", "") if person.get("PhoneNumbers") else "",
        "line1":    (person.get("Address") or {}).get("AddressLine1", ""),
        "line2":    (person.get("Address") or {}).get("AddressLine2", ""),
        "city":     (person.get("Address") or {}).get("City", ""),
        "state":    (person.get("Address") or {}).get("StateRegion", "AZ"),
        "zip":      (person.get("Address") or {}).get("PostalCode", ""),
    }


def post_buildium_note(owner_id: int, violation: dict, stage_info: dict, photo_url: str) -> None:
    """Post a note to the Buildium owner account documenting the violation."""
    body = (
        f"VIOLATION NOTICE — {violation['violation_label']}\n"
        f"Reference: {violation['violation_ref']}\n"
        f"Address: {violation['address']}\n"
        f"Stage: {stage_info['label']}\n"
        f"Fine: ${stage_info['fine']:.2f}\n"
        f"Officer: {violation['officer']}\n"
        f"GPS: {violation.get('lat','')}, {violation.get('lng','')}\n"
        f"Photo: {photo_url or '(no photo)'}\n"
        f"Notes: {violation.get('notes') or '—'}\n"
        f"Deadline: {violation.get('deadline_date','')}"
    )
    try:
        buildium_post(f"/associations/owners/{owner_id}/notes", {"Note": body})
        log.info(f"  Buildium note posted for owner {owner_id}")
    except Exception as e:
        log.warning(f"  Buildium note failed: {e}")


def post_buildium_fine(owner_id: int, violation: dict, stage_info: dict) -> None:
    """Post a charge to the Buildium ledger (stages 3+)."""
    if stage_info["fine"] <= 0:
        return
    try:
        buildium_post(f"/associations/owners/{owner_id}/transactions", {
            "Type": "Charge",
            "Date": date.today().isoformat(),
            "Amount": stage_info["fine"],
            "Description": f"HOA Violation Fine — {violation['violation_label']} ({violation['violation_ref']})",
            "Reference": violation["violation_ref"],
        })
        log.info(f"  Fine ${stage_info['fine']} posted to Buildium ledger")
    except Exception as e:
        log.warning(f"  Buildium fine post failed: {e}")

# ══════════════════════════════════════════════════════════
# STAGE DETERMINATION
# ══════════════════════════════════════════════════════════
def determine_stage(address: str, category_id: str, violation_id: str,
                    current_violation_created_at: str) -> tuple[int, str | None]:
    """
    Count prior PROCESSED violations at this address with the same violation_id.
    Returns (stage_number, skip_reason_or_None).
    stage = number of prior processed violations + 1
    Skip if the most recent processed notice was < MIN_DAYS_BETWEEN days ago.
    """
    street = address.split(",")[0].strip()

    # All prior processed violations for this address + violation type
    result = sb.table("violations") \
        .select("id, stage, created_at, cascade_processed") \
        .ilike("address", f"%{street}%") \
        .eq("violation_id", violation_id) \
        .eq("cascade_processed", True) \
        .order("created_at", desc=True) \
        .execute()

    prior = result.data or []

    # Check 20-day rule
    if prior:
        most_recent_dt = datetime.fromisoformat(prior[0]["created_at"].replace("Z", "+00:00"))
        current_dt     = datetime.fromisoformat(current_violation_created_at.replace("Z", "+00:00"))
        days_diff      = (current_dt - most_recent_dt).days
        if days_diff < MIN_DAYS_BETWEEN:
            return None, f"20-day rule: last notice was {days_diff} days ago (need {MIN_DAYS_BETWEEN})"

    stage = min(len(prior) + 1, 7)
    return stage, None

# ══════════════════════════════════════════════════════════
# TWILIO SMS
# ══════════════════════════════════════════════════════════
def send_sms(to_phone: str, violation: dict, stage_info: dict, portal_link: str) -> str | None:
    """Send a brief SMS notice. Returns Twilio message SID or None."""
    if not to_phone:
        log.info("  SMS skipped — no phone on file")
        return None
    try:
        # Normalize phone
        phone = "".join(c for c in to_phone if c.isdigit() or c == "+")
        if not phone.startswith("+"):
            phone = "+1" + phone

        body = (
            f"SBR HOA Notice: A violation has been logged at {violation['address']}.\n"
            f"Violation: {violation['violation_label']}\n"
            f"Stage: {stage_info['label']}\n"
        )
        if stage_info["fine"] > 0:
            body += f"Fine: ${stage_info['fine']:.2f}\n"
        body += (
            f"Deadline: {violation.get('deadline_date','20 days')}\n"
            f"To resolve: {portal_link}\n"
            f"(Use the link above — do not reply to this text or log into the HOA portal to respond)\n"
            f"Questions? {HOA_PHONE}"
        )

        tc = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        msg = tc.messages.create(body=body, from_=TWILIO_FROM, to=phone)
        log.info(f"  SMS sent → {phone} (SID: {msg.sid})")
        return msg.sid
    except Exception as e:
        log.warning(f"  SMS failed: {e}")
        return None

# ══════════════════════════════════════════════════════════
# GMAIL EMAIL
# ══════════════════════════════════════════════════════════
def build_email_html(violation: dict, owner: dict, stage_info: dict, portal_link: str) -> str:
    fine_row = (
        f"<tr><td><strong>Fine:</strong></td><td><strong style='color:#dc2626'>${stage_info['fine']:.2f}</strong></td></tr>"
        if stage_info["fine"] > 0 else ""
    )
    photo_block = (
        f"<p><img src='{violation['photo_url']}' style='max-width:100%;border-radius:8px;margin:8px 0'></p>"
        if violation.get("photo_url") else ""
    )
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;color:#1f2937">
<div style="background:#1e40af;padding:20px 24px;border-radius:8px 8px 0 0">
  <h1 style="color:white;margin:0;font-size:20px">Signal Butte Ranch HOA</h1>
  <p style="color:rgba(255,255,255,0.8);margin:4px 0 0;font-size:13px">Violation Notice — {violation['violation_ref']}</p>
</div>
<div style="background:#f9fafb;padding:20px 24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px">
  <p>Dear {owner.get('first','Homeowner')},</p>
  <p>A violation has been observed at the above property and is being brought to your attention.</p>
  <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:6px;padding:10px 14px;margin:12px 0;font-size:13px;color:#92400e">
    <strong>How to respond:</strong> Please use the button below to submit your resolution photo.
    Do not reply to this email or use the HOA Resident Center portal to respond to violations —
    those paths may cause delays. The link below is the fastest way to get this resolved.
  </div>
  {photo_block}
  <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
    <tr><td style="padding:4px 16px 4px 0;color:#6b7280">Property:</td><td><strong>{violation['address']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#6b7280">Violation:</td><td>{violation['violation_label']}</td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#6b7280">Stage:</td><td>{stage_info['label']}</td></tr>
    {fine_row}
    <tr><td style="padding:4px 16px 4px 0;color:#6b7280">Deadline:</td><td>{violation.get('deadline_date','within 20 days')}</td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#6b7280">Reference:</td><td style="font-family:monospace">{violation['violation_ref']}</td></tr>
  </table>
  <div style="background:white;border:1px solid #d1d5db;border-radius:8px;padding:16px;margin:16px 0">
    <h3 style="margin:0 0 8px;font-size:15px">How to resolve this violation:</h3>
    <p style="margin:0;font-size:14px;line-height:1.6">
      Once the issue has been corrected, you may upload a photo of the resolved violation at the link below.
      Our system will review it and close the notice if resolved.
    </p>
    <p style="margin:12px 0 0"><a href="{portal_link}" style="background:#1e40af;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px">Resolve This Violation →</a></p>
  </div>
  <p style="font-size:13px;color:#6b7280;line-height:1.6;margin-top:16px">
    If you have questions or believe this notice was sent in error, please contact us at
    <a href="mailto:{HOA_EMAIL}">{HOA_EMAIL}</a> or {HOA_PHONE}.
  </p>
  <p style="font-size:13px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:12px;margin-top:12px">
    {HOA_NAME} · {HOA_ADDR} · {HOA_PHONE}
  </p>
</div>
</body></html>"""


def send_email(to_email: str, violation: dict, owner: dict, stage_info: dict, portal_link: str) -> bool:
    """Send violation notice email via Gmail SMTP. Returns True on success."""
    if not to_email:
        log.info("  Email skipped — no email on file")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"HOA Violation Notice — {violation['address']} ({violation['violation_ref']})"
        msg["From"]    = f"{HOA_NAME} <{GMAIL_FROM}>"
        msg["To"]      = to_email

        html = build_email_html(violation, owner, stage_info, portal_link)
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_FROM, GMAIL_APP_PWD)
            smtp.sendmail(GMAIL_FROM, to_email, msg.as_string())

        log.info(f"  Email sent → {to_email}")
        return True
    except Exception as e:
        log.warning(f"  Email failed: {e}")
        return False

# ══════════════════════════════════════════════════════════
# LOB PHYSICAL LETTER
# ══════════════════════════════════════════════════════════
def build_letter_html(violation: dict, owner: dict, stage_info: dict, portal_link: str) -> str:
    """Build HTML for Lob physical letter."""
    fine_line = (
        f"<p><strong>Fine: ${stage_info['fine']:.2f}</strong> — This charge has been posted to your account.</p>"
        if stage_info["fine"] > 0 else ""
    )
    certified_notice = (
        "<p style='font-weight:700;color:#dc2626'>This is a certified notice requiring your signature upon receipt.</p>"
        if stage_info["certified"] else ""
    )
    return f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;font-size:11pt;color:#000;max-width:680px;margin:0 auto">
<table style="width:100%;margin-bottom:24pt">
  <tr>
    <td>
      <strong>{HOA_NAME}</strong><br>
      {HOA_ADDR}<br>
      {HOA_PHONE} | {HOA_WEBSITE}
    </td>
    <td style="text-align:right;vertical-align:top;font-size:10pt;color:#555">
      {date.today().strftime('%B %d, %Y')}<br>
      Ref: {violation['violation_ref']}
    </td>
  </tr>
</table>

<p>
  {owner.get('name','Homeowner')}<br>
  {owner.get('line1','')}<br>
  {(owner.get('line2','') + '<br>') if owner.get('line2') else ''}
  {owner.get('city','')}, {owner.get('state','AZ')} {owner.get('zip','')}
</p>

<p><strong>Re: Violation Notice — {violation['violation_label']}</strong><br>
Property: {violation['address']}</p>

{certified_notice}

<p>Dear {owner.get('first','Homeowner')},</p>

<p>This letter is to inform you that the following violation has been observed at your property
and is in violation of the Signal Butte Ranch CC&amp;Rs.</p>

<table style="border-collapse:collapse;margin:12pt 0;font-size:10.5pt">
  <tr><td style="padding:3pt 12pt 3pt 0;font-weight:bold">Violation:</td><td>{violation['violation_label']}</td></tr>
  <tr><td style="padding:3pt 12pt 3pt 0;font-weight:bold">Stage:</td><td>{stage_info['label']}</td></tr>
  <tr><td style="padding:3pt 12pt 3pt 0;font-weight:bold">Deadline:</td><td>{violation.get('deadline_date', '20 days from receipt')}</td></tr>
</table>

{fine_line}

<p>Please correct this violation within the deadline noted above to avoid further action.
Once corrected, you may confirm resolution online at:</p>

<p style="font-family:monospace;font-size:10pt">{portal_link}</p>

<p>If you have questions or believe this notice was issued in error, please contact the Association
at {HOA_EMAIL} or {HOA_PHONE}.</p>

<p>Respectfully,<br><br>
<strong>{HOA_NAME}</strong><br>
Self-Managed Community Association<br>
{HOA_PHONE}</p>
</body></html>"""


def send_lob_letter(violation: dict, owner: dict, stage_info: dict, portal_link: str) -> str | None:
    """Mail a physical letter via Lob.com. Returns letter ID or None."""
    try:
        to_addr = {
            "name":    owner.get("name", "Homeowner"),
            "address_line1": owner.get("line1") or violation["address"].split(",")[0],
            "address_city":  owner.get("city", "Mesa"),
            "address_state": owner.get("state", "AZ"),
            "address_zip":   owner.get("zip", "85212"),
            "address_country": "US",
        }
        if owner.get("line2"):
            to_addr["address_line2"] = owner["line2"]

        from_addr = {
            "name":    HOA_NAME,
            "address_line1": "PO Box 98526",
            "address_city":  "Phoenix",
            "address_state": "AZ",
            "address_zip":   "85038",
            "address_country": "US",
        }

        html_body = build_letter_html(violation, owner, stage_info, portal_link)
        mail_type = "usps_certified" if stage_info["certified"] else "usps_first_class"

        resp = requests.post(
            "https://api.lob.com/v1/letters",
            auth=(LOB_KEY, ""),
            data={
                "description": f"Violation {violation['violation_ref']} — {violation['violation_label']}",
                "to":          json.dumps(to_addr),
                "from":        json.dumps(from_addr),
                "file":        html_body,
                "color":       "false",
                "double_sided": "false",
                "mail_type":   mail_type,
                "use_type":    "operational",
            },
        )
        resp.raise_for_status()
        letter_id = resp.json().get("id")
        log.info(f"  Lob letter queued: {letter_id} (mail_type={mail_type})")
        return letter_id
    except Exception as e:
        log.warning(f"  Lob letter failed: {e}")
        return None

# ══════════════════════════════════════════════════════════
# BOARD APPROVAL ALERT (Stages 6–7)
# ══════════════════════════════════════════════════════════
def send_board_approval_alert(violation: dict, owner: dict, stage_info: dict) -> None:
    """Email Crystal with approve/deny links instead of sending automatically."""
    vio_id = violation.get("id")
    subject = f"⚠️ Board Approval Required — {violation['violation_ref']} (Stage {violation.get('stage')})"
    # Simple approval URL (you'd build a proper endpoint for this)
    approve_url = f"{PORTAL_URL}/approve?id={vio_id}&token=CRYSTAL"
    body = f"""
<p>A violation has reached Stage {violation.get('stage')} and requires board approval before notices are sent.</p>
<table>
  <tr><td><b>Reference:</b></td><td>{violation['violation_ref']}</td></tr>
  <tr><td><b>Address:</b></td><td>{violation['address']}</td></tr>
  <tr><td><b>Homeowner:</b></td><td>{owner.get('name','—')}</td></tr>
  <tr><td><b>Violation:</b></td><td>{violation['violation_label']}</td></tr>
  <tr><td><b>Stage:</b></td><td>{stage_info['label']}</td></tr>
  <tr><td><b>Fine:</b></td><td>${stage_info['fine']:.2f}</td></tr>
</table>
<p>
  <a href="{approve_url}&action=approve" style="background:#16a34a;color:white;padding:10px 20px;text-decoration:none;border-radius:6px;margin-right:10px">✅ Approve — Send Notices</a>
  <a href="{approve_url}&action=deny"    style="background:#dc2626;color:white;padding:10px 20px;text-decoration:none;border-radius:6px">❌ Deny — Hold</a>
</p>
<p>Photo: {violation.get('photo_url','(none)')}</p>
"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{HOA_NAME} <{GMAIL_FROM}>"
        msg["To"]      = CRYSTAL_EMAIL
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_FROM, GMAIL_APP_PWD)
            smtp.sendmail(GMAIL_FROM, CRYSTAL_EMAIL, msg.as_string())
        log.info(f"  Board approval alert sent to Crystal")
    except Exception as e:
        log.warning(f"  Board approval alert failed: {e}")

# ══════════════════════════════════════════════════════════
# MAIN CASCADE LOOP
# ══════════════════════════════════════════════════════════
def process_violation(row: dict) -> None:
    vio_ref = row.get("violation_ref", row["id"])
    log.info(f"\n{'='*60}\nProcessing {vio_ref} — {row['violation_label']}\nAddress: {row['address']}")

    # ── Stage determination ──────────────────────────────────
    stage, skip_reason = determine_stage(
        row["address"], row["category_id"], row["violation_id"], row["created_at"]
    )
    if skip_reason:
        log.info(f"  SKIPPED: {skip_reason}")
        sb.table("violations").update({
            "cascade_processed": True,
            "notes": (row.get("notes") or "") + f"\n[CASCADE SKIP: {skip_reason}]",
        }).eq("id", row["id"]).execute()
        return

    stage_info = FINE_SCHEDULE[stage]
    log.info(f"  Stage {stage}: {stage_info['label']} | Fine: ${stage_info['fine']}")

    # ── Buildium lookup ──────────────────────────────────────
    unit  = find_buildium_unit(row["address"])
    owner_raw   = find_buildium_owner(unit) if unit else None
    owner       = get_owner_contact(owner_raw) if owner_raw else {}
    buildium_id = owner_raw.get("Id") if owner_raw else None
    log.info(f"  Owner: {owner.get('name','NOT FOUND')} | Email: {owner.get('email','—')} | Phone: {owner.get('phone','—')}")

    # Portal link for this specific violation
    portal_link = f"{PORTAL_URL}?id={row.get('violation_ref',row['id'])}"

    # ── Board approval gate (stages 6–7) ─────────────────────
    if stage_info["board_approval"]:
        log.info(f"  Stage {stage} requires board approval — alerting Crystal")
        # Update stage/fine in Supabase and set board_approved = False (pending)
        sb.table("violations").update({
            "stage":          stage,
            "fine_amount":    stage_info["fine"],
            "buildium_acct_id": buildium_id,
            "board_approved": False,
            "cascade_processed": True,
        }).eq("id", row["id"]).execute()
        send_board_approval_alert(dict(row, stage=stage), owner, stage_info)
        return

    # ── Buildium: post note + fine ────────────────────────────
    if buildium_id:
        post_buildium_note(buildium_id, row, stage_info, row.get("photo_url"))
        post_buildium_fine(buildium_id, row, stage_info)

    # ── Twilio SMS ────────────────────────────────────────────
    sms_sid = send_sms(owner.get("phone", ""), row, stage_info, portal_link)

    # ── Gmail email ───────────────────────────────────────────
    send_email(owner.get("email", ""), row, owner, stage_info, portal_link)

    # ── Lob physical letter (stage 2+) ───────────────────────
    lob_id = None
    if stage_info["letter"] and owner.get("line1"):
        lob_id = send_lob_letter(row, owner, stage_info, portal_link)

    # ── Mark processed in Supabase ───────────────────────────
    sb.table("violations").update({
        "stage":             stage,
        "fine_amount":       stage_info["fine"],
        "buildium_acct_id":  buildium_id,
        "twilio_sms_id":     sms_sid,
        "lob_letter_id":     lob_id,
        "cascade_processed": True,
    }).eq("id", row["id"]).execute()

    log.info(f"  ✓ {vio_ref} processed successfully")


def run_cascade() -> None:
    log.info("Cascade engine starting…")
    # Fetch all unprocessed violations
    result = sb.table("violations") \
        .select("*") \
        .eq("cascade_processed", False) \
        .eq("status", "open") \
        .is_("board_approved", "null") \
        .order("created_at") \
        .execute()

    rows = result.data or []
    log.info(f"Found {len(rows)} unprocessed violation(s)")

    for row in rows:
        try:
            process_violation(row)
        except Exception as e:
            log.error(f"  ERROR processing {row.get('violation_ref', row['id'])}: {e}")

    log.info("Cascade engine complete.")


if __name__ == "__main__":
    run_cascade()
