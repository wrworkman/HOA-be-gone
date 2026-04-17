#!/usr/bin/env python3
"""
SBR Violation Tracker — Buildium Inbound Sync
==============================================
Runs daily via GitHub Actions.

Some homeowners will respond to violation notices through Buildium's
Resident Center instead of our resolution portal — out of habit or
because they're already logged in. This script catches those responses
so nothing falls through the cracks.

For every open violation in Supabase that has a Buildium account ID, it:
  1. Checks if the violation was marked resolved in Buildium
  2. Checks for new resident messages in the Buildium inbox mentioning
     the property address or violation reference
  3. Checks for new documents uploaded by the homeowner in Buildium
  4. If any activity found: marks violation as pending_resolution in
     Supabase and notifies Crystal via email

This complements our resolution portal (Option A) as a safety net (Option B).
Crystal sees everything in her weekly digest regardless of which path
the homeowner used.
"""

import os, smtplib, logging
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
from supabase import create_client

log = logging.getLogger("buildium_inbound_sync")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUILDIUM_CLIENT_ID   = os.environ["BUILDIUM_CLIENT_ID"]
BUILDIUM_CLIENT_SECRET = os.environ["BUILDIUM_CLIENT_SECRET"]
GMAIL_FROM           = os.environ["GMAIL_FROM"]
GMAIL_APP_PWD        = os.environ["GMAIL_APP_PASSWORD"]
CRYSTAL_EMAIL        = os.environ.get("CRYSTAL_EMAIL", GMAIL_FROM)
PORTAL_URL           = os.environ.get("RESOLUTION_PORTAL_URL",
                                       "https://sbrhoa.github.io/violations/resolve")

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Look back 25 hours so we don't miss anything between daily runs
LOOKBACK_HOURS = 25

# ══════════════════════════════════════════════════════════
# BUILDIUM AUTH (reuse pattern from cascade.py)
# ══════════════════════════════════════════════════════════
_token_cache: dict = {}

def get_token() -> str:
    global _token_cache
    if _token_cache.get("expires_at", datetime.min) > datetime.utcnow():
        return _token_cache["access_token"]
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
    _token_cache = {
        "access_token": d["access_token"],
        "expires_at": datetime.utcnow() + timedelta(seconds=d.get("expires_in", 3600) - 60),
    }
    return d["access_token"]


def buildium_get(path: str, params: dict = None) -> list | dict | None:
    token = get_token()
    r = requests.get(
        f"https://api.buildium.com/v1{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "x-buildium-api-key": BUILDIUM_CLIENT_ID,
        },
        params=params or {},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

# ══════════════════════════════════════════════════════════
# FETCH OPEN VIOLATIONS FROM SUPABASE
# ══════════════════════════════════════════════════════════
def get_open_violations() -> list:
    """Get all open/pending violations that have a Buildium account ID."""
    r = sb.table("violations") \
        .select("id, violation_ref, address, violation_label, buildium_acct_id, status, stage, created_at") \
        .in_("status", ["open", "pending_resolution"]) \
        .not_.is_("buildium_acct_id", "null") \
        .execute()
    return r.data or []

# ══════════════════════════════════════════════════════════
# CHECK BUILDIUM VIOLATION STATUS
# ══════════════════════════════════════════════════════════
def check_violation_resolved_in_buildium(buildium_vio_id: int) -> bool:
    """
    Check if a violation has been marked closed/resolved in Buildium directly.
    Buildium violation statuses: Draft, Submitted, Resolved, Closed
    """
    if not buildium_vio_id:
        return False
    vio = buildium_get(f"/associations/violations/{buildium_vio_id}")
    if not vio:
        return False
    status = (vio.get("Status") or "").lower()
    return status in ("resolved", "closed")

# ══════════════════════════════════════════════════════════
# CHECK RESIDENT MESSAGES IN BUILDIUM INBOX
# ══════════════════════════════════════════════════════════
def check_resident_messages(owner_id: int, violation_ref: str, address: str) -> list:
    """
    Look for any messages from this owner in Buildium's inbox
    that were sent in the last LOOKBACK_HOURS hours.
    Returns list of relevant message summaries.
    """
    if not owner_id:
        return []

    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    activity = []

    try:
        # Check owner's communication history
        messages = buildium_get(
            f"/communications/inbox",
            params={
                "OwnerId": owner_id,
                "CreatedDateTimeFrom": since,
                "PageSize": 50,
            }
        )

        if not messages:
            return []

        items = messages if isinstance(messages, list) else messages.get("Results", [])

        for msg in items:
            subject = (msg.get("Subject") or "").lower()
            body    = (msg.get("Body") or "").lower()
            ref_lower    = violation_ref.lower() if violation_ref else ""
            addr_keyword = address.split(",")[0].lower() if address else ""

            # Look for messages that reference this violation or address
            if (ref_lower and ref_lower in (subject + body)) or \
               (addr_keyword and addr_keyword in (subject + body)) or \
               "violation" in (subject + body) or \
               "fixed" in body or "resolved" in body or "complied" in body:
                activity.append({
                    "type": "message",
                    "subject": msg.get("Subject", "(no subject)"),
                    "from": msg.get("SenderName", "Homeowner"),
                    "date": msg.get("CreatedDateTime", ""),
                    "preview": body[:200],
                })

    except Exception as e:
        log.warning(f"  Message check failed for owner {owner_id}: {e}")

    return activity

# ══════════════════════════════════════════════════════════
# CHECK FOR HOMEOWNER DOCUMENT UPLOADS
# ══════════════════════════════════════════════════════════
def check_document_uploads(owner_id: int) -> list:
    """
    Check if the homeowner uploaded any documents to Buildium
    in the last LOOKBACK_HOURS hours (e.g., proof of compliance photo).
    """
    if not owner_id:
        return []

    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    activity = []

    try:
        docs = buildium_get(
            f"/files/sharingentities",
            params={
                "EntityType": "AssociationOwner",
                "EntityId": owner_id,
                "UpdatedSince": since,
                "PageSize": 20,
            }
        )

        if not docs:
            return []

        items = docs if isinstance(docs, list) else docs.get("Results", [])

        for doc in items:
            # Only flag documents uploaded BY the owner (not by management)
            uploaded_by = (doc.get("CreatedByUser") or {}).get("UserRole", "")
            if "owner" in uploaded_by.lower() or "resident" in uploaded_by.lower():
                activity.append({
                    "type": "document",
                    "name": doc.get("FileName", "Unknown file"),
                    "date": doc.get("CreatedDateTime", ""),
                    "url":  doc.get("DownloadUrl", ""),
                })

    except Exception as e:
        log.warning(f"  Document check failed for owner {owner_id}: {e}")

    return activity

# ══════════════════════════════════════════════════════════
# UPDATE SUPABASE + NOTIFY CRYSTAL
# ══════════════════════════════════════════════════════════
def flag_buildium_response(violation: dict, activity: list, resolved_in_buildium: bool) -> None:
    """Mark the violation as pending review and notify Crystal."""

    new_status = "resolved" if resolved_in_buildium else "pending_resolution"
    notes_addon = "\n[BUILDIUM ACTIVITY: Homeowner responded through Buildium Resident Center — see Crystal's alert]"

    sb.table("violations").update({
        "status": new_status,
        "ai_verdict": "pending",
        "ai_notes": "Homeowner responded via Buildium — manual review needed.",
        "notes": (violation.get("notes") or "") + notes_addon,
        "resolved_at": datetime.now(timezone.utc).isoformat() if resolved_in_buildium else None,
    }).eq("id", violation["id"]).execute()

    log.info(f"  Flagged {violation['violation_ref']} — Buildium activity detected")

    # Email Crystal
    portal_link = f"{PORTAL_URL}?id={violation.get('violation_ref', violation['id'])}"

    activity_html = ""
    for a in activity:
        if a["type"] == "message":
            activity_html += f"""
            <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin-bottom:8px">
              <div style="font-size:12px;font-weight:700;color:#1e40af">💬 Message from homeowner</div>
              <div style="font-size:13px;margin-top:4px"><strong>Subject:</strong> {a['subject']}</div>
              <div style="font-size:13px;color:#6b7280;margin-top:2px">{a.get('preview','')}</div>
            </div>"""
        elif a["type"] == "document":
            doc_link = f"<a href='{a['url']}'>View document</a>" if a.get("url") else ""
            activity_html += f"""
            <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin-bottom:8px">
              <div style="font-size:12px;font-weight:700;color:#059669">📎 Document uploaded by homeowner</div>
              <div style="font-size:13px;margin-top:4px">{a['name']} {doc_link}</div>
            </div>"""

    resolved_banner = ""
    if resolved_in_buildium:
        resolved_banner = """
        <div style="background:#d1fae5;border:1px solid #34d399;border-radius:8px;padding:12px;margin-bottom:16px;font-weight:700;color:#065f46">
          ✅ This violation was marked as Resolved directly in Buildium.
        </div>"""

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;color:#1f2937">
<div style="background:#1e40af;padding:18px 22px;border-radius:8px 8px 0 0">
  <h2 style="color:white;margin:0;font-size:17px">Homeowner Responded via Buildium</h2>
  <p style="color:rgba(255,255,255,0.75);margin:4px 0 0;font-size:12px">Action required — {violation['violation_ref']}</p>
</div>
<div style="padding:18px 22px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px">
  {resolved_banner}
  <p style="font-size:14px">A homeowner responded to a violation notice through <strong>Buildium's Resident Center</strong> instead of the resolution portal. Please review and close the violation if appropriate.</p>

  <table style="font-size:13px;margin:12px 0">
    <tr><td style="padding:3px 12px 3px 0;color:#6b7280">Reference:</td><td><strong>{violation['violation_ref']}</strong></td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#6b7280">Address:</td><td>{violation['address']}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#6b7280">Violation:</td><td>{violation['violation_label']}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#6b7280">Stage:</td><td>{violation.get('stage','?')}</td></tr>
  </table>

  <div style="margin:16px 0">{activity_html}</div>

  <p>
    <a href="{portal_link}" style="background:#1e40af;color:white;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px">Review Violation →</a>
  </p>
  <p style="font-size:12px;color:#9ca3af;margin-top:16px">
    To close this violation, log into Supabase or use the resolution portal link above.
    The cascade engine has been paused for this violation pending your review.
  </p>
</div>
</body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Homeowner Responded in Buildium — {violation['violation_ref']} ({violation['address']})"
        msg["From"]    = f"SBR Violations <{GMAIL_FROM}>"
        msg["To"]      = CRYSTAL_EMAIL
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_FROM, GMAIL_APP_PWD)
            smtp.sendmail(GMAIL_FROM, CRYSTAL_EMAIL, msg.as_string())
        log.info(f"  Crystal notified: {violation['violation_ref']}")
    except Exception as e:
        log.warning(f"  Crystal notification failed: {e}")

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def run():
    log.info(f"Buildium inbound sync starting (looking back {LOOKBACK_HOURS}h)…")
    violations = get_open_violations()
    log.info(f"Checking {len(violations)} open violation(s) with Buildium accounts")

    flagged = 0

    for v in violations:
        owner_id = v.get("buildium_acct_id")
        if not owner_id:
            continue

        log.info(f"  Checking {v.get('violation_ref','?')} — {v['address']}")

        # 1. Check if resolved directly in Buildium
        # Note: this requires knowing the Buildium violation ID.
        # We store the owner/account ID; if you also store the Buildium
        # violation ID in a future update, pass it here.
        resolved = False  # check_violation_resolved_in_buildium(buildium_vio_id)

        # 2. Check for resident messages
        messages = check_resident_messages(owner_id, v.get("violation_ref",""), v["address"])

        # 3. Check for document uploads
        documents = check_document_uploads(owner_id)

        activity = messages + documents

        if activity or resolved:
            log.info(f"  ⚡ Activity found: {len(messages)} message(s), {len(documents)} doc(s), resolved={resolved}")
            flag_buildium_response(v, activity, resolved)
            flagged += 1
        else:
            log.info(f"  No activity.")

    log.info(f"Sync complete. {flagged} violation(s) flagged.")


if __name__ == "__main__":
    run()
