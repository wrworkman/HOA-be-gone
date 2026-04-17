"""
Signal Butte Ranch HOA — Collections Automation  v2.0
======================================================
Runs monthly. For each active owner account:
  1. Pulls all charge transactions
  2. Checks if account is still delinquent (late fee still applying)
  3. Excludes charges < 30 days old (avoids false positives on new assessments)
  4. Counts the full collections notice history to determine true stage
  5. Posts the next fine to the ledger
  6. Sends the next EZ Mail notice via Buildium
  7. Flags accounts needing certified mail for Crystal's proof approval
  8. Flags accounts approaching attorney threshold (18 months OR $10,000 past due)
  9. Sends Crystal a summary email

STOPPING CONDITIONS:
  - Balance ≤ $0 (paid or prepaid)           → skip
  - No $15 late fee in last 45 days           → flag for manual review (may have paid)
  - DelinquencyStatus = "InCollections"       → skip (attorney is handling)
  - DelinquencyStatus = "PaymentPlan"         → skip (send regular statement only)
  - Last collections notice sent < 25 days ago → skip (already ran this month)

SETUP:
  pip install requests
  Fill in CONFIG below, set dry_run=True first to preview, then False to go live.
"""

import requests
import re
import smtplib
import logging
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sbr_collections")

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────
CONFIG = {
    "buildium_client_id":      "d33da506-8f83-4e5f-a808-04c3cb0842a6",
    "buildium_client_secret":  "jhDyXiXoG1NAWVfxiy1pklL2nobQkleTbexdjAwbt78=",
    "buildium_association_id": "103158",

    # GL Account IDs — confirmed 4/10/2026
    "gl_demand_notices":   51537,   # Income- Collections Demand Notices
    "gl_certified_mail":   51538,   # Income- Collections Certified Notices
    "gl_lien_filing":      67944,   # Income- Collections Lien Filing
    "gl_late_fee":         8,       # Income- Late Fees  (Buildium auto-applies $15)
    "gl_assessment":       4,       # Income- Homeowner Assessments

    # Monthly assessment amount — used to detect prepayment
    "monthly_assessment":  62.00,

    # Attorney thresholds
    "attorney_months":     18,      # months continuously delinquent
    "attorney_balance":    10000.00,# total past-due balance

    # Email summary
    "email_from":          "sbrneighbors@gmail.com",
    "email_password":      "YOUR_GMAIL_APP_PASSWORD",
    "email_to":            "sbrneighbors@gmail.com",

    # Safety
    "dry_run": True,   # ← set False only when ready to go live
}

BUILDIUM_BASE = "https://api.buildium.com/v1"
COLLECTIONS_GL_IDS = {
    CONFIG["gl_demand_notices"],
    CONFIG["gl_certified_mail"],
    CONFIG["gl_lien_filing"],
}

# ─────────────────────────────────────────────────────────────────
#  MEMO NORMALIZATION
#  Crystal has used many slight variations. We normalize to a
#  canonical stage name based on keywords.
# ─────────────────────────────────────────────────────────────────
def normalize_memo(memo: str) -> str:
    """Map any memo variation to a canonical stage name."""
    if not memo:
        return "unknown"
    m = memo.lower().strip()
    if re.search(r"pre.?legal.*final|final.*pre.?legal|30.day.*legal|30 days prior", m):
        return "pre_legal_final"
    if re.search(r"pre.?legal.*60|60.day.*legal|60 days prior", m):
        return "pre_legal_60"
    if re.search(r"lien.*record|180|lien filing", m):
        return "lien_180"
    if re.search(r"pre.?lien|notice of lien|150", m):
        return "prelien_150"
    if re.search(r"advanced|advance|post.lien", m):
        return "advanced"
    if re.search(r"120", m):
        return "day_120"
    if re.search(r"90", m):
        return "day_90"
    if re.search(r"60", m):
        return "day_60"
    return "unknown"


# ─────────────────────────────────────────────────────────────────
#  STAGE DETERMINATION
#  Based on the FULL history of collections notices, determine
#  the true current stage and what action to take next.
# ─────────────────────────────────────────────────────────────────
def determine_next_action(notice_history: list[str], total_assessment_balance: float) -> dict:
    """
    Given the normalized history of notices sent (oldest first),
    and the estimated total assessment balance, return the next action.

    Returns dict with:
      stage_name, fine_amount, gl_account, certified_mail,
      letter_template, all_addresses, board_alert
    """
    # Count each stage
    counts = {}
    for n in notice_history:
        counts[n] = counts.get(n, 0) + 1

    # Determine highest stage reached
    if counts.get("pre_legal_final", 0) >= 1:
        return {
            "stage_name":      "BOARD ALERT — Ready for Attorney",
            "fine_amount":     0,
            "gl_account":      None,
            "certified_mail":  False,
            "letter_template": None,
            "all_addresses":   True,
            "board_alert":     "30-day final notice already sent. Turn over to collections attorney.",
            "auto_handle":     False,
        }

    if counts.get("pre_legal_60", 0) >= 1 and counts.get("pre_legal_final", 0) == 0:
        return {
            "stage_name":      "Pre-Legal Final Notice (30-Day)",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_certified_mail"],
            "certified_mail":  True,
            "letter_template": "Collections Notice - Pre-Legal Final",
            "all_addresses":   True,
            "board_alert":     "Final pre-legal notice being sent. Attorney referral in 30 days.",
            "auto_handle":     True,
        }

    # Advanced stage: runs until 18 months OR $10k threshold
    advanced_count = counts.get("advanced", 0)
    months_delinquent = len(notice_history)

    # Check attorney thresholds
    attorney_threshold_met = (
        months_delinquent >= CONFIG["attorney_months"] or
        total_assessment_balance >= CONFIG["attorney_balance"]
    )

    if attorney_threshold_met and advanced_count >= 1:
        return {
            "stage_name":      "Pre-Legal 60-Day Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_certified_mail"],
            "certified_mail":  True,
            "letter_template": "Collections Notice - Pre-Legal 60 Day",
            "all_addresses":   True,
            "board_alert":     f"Account at {months_delinquent} months / ${total_assessment_balance:.0f} past due. Pre-legal 60-day notice being sent.",
            "auto_handle":     True,
        }

    if counts.get("advanced", 0) >= 1:
        return {
            "stage_name":      "Advanced Delinquency Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_demand_notices"],
            "certified_mail":  False,
            "letter_template": "Collections Notice - Advanced Stage of Delinquency",
            "all_addresses":   True,
            "board_alert":     None,
            "auto_handle":     True,
        }

    if counts.get("lien_180", 0) >= 1:
        return {
            "stage_name":      "Advanced Delinquency Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_demand_notices"],
            "certified_mail":  False,
            "letter_template": "Collections Notice - Advanced Stage of Delinquency",
            "all_addresses":   True,
            "board_alert":     None,
            "auto_handle":     True,
        }

    if counts.get("prelien_150", 0) >= 1:
        return {
            "stage_name":      "180-Day Lien Filing",
            "fine_amount":     250.0,
            "gl_account":      CONFIG["gl_lien_filing"],
            "certified_mail":  True,
            "letter_template": "Collections Notice - Lien",
            "all_addresses":   True,
            "board_alert":     "Lien being filed this cycle.",
            "auto_handle":     True,
        }

    if counts.get("day_120", 0) >= 1:
        return {
            "stage_name":      "150-Day Pre-Lien Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_certified_mail"],
            "certified_mail":  True,
            "letter_template": "Collections Notice - Pre-Lien",
            "all_addresses":   True,
            "board_alert":     None,
            "auto_handle":     True,
        }

    if counts.get("day_90", 0) >= 1:
        return {
            "stage_name":      "120-Day Collection Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_demand_notices"],
            "certified_mail":  False,
            "letter_template": "Collections Notice - 120 Day",
            "all_addresses":   True,
            "board_alert":     None,
            "auto_handle":     True,
        }

    if counts.get("day_60", 0) >= 1:
        return {
            "stage_name":      "90-Day Collection Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_demand_notices"],
            "certified_mail":  False,
            "letter_template": "Collections Notice - 90 Day",
            "all_addresses":   False,
            "board_alert":     None,
            "auto_handle":     True,
        }

    # No prior collections notices — this is the first one
    return {
        "stage_name":      "60-Day Collection Notice",
        "fine_amount":     40.0,
        "gl_account":      CONFIG["gl_demand_notices"],
        "certified_mail":  False,
        "letter_template": "Collections Notice - 60 Day",
        "all_addresses":   False,
        "board_alert":     None,
        "auto_handle":     True,
    }


# ─────────────────────────────────────────────────────────────────
#  DELINQUENCY DETECTION
# ─────────────────────────────────────────────────────────────────
def analyze_account(acct_id: int) -> dict | None:
    """
    Pull charge history for one account.
    Returns analysis dict if delinquent, None if current/skip.
    """
    today         = date.today()
    cutoff_30     = today - timedelta(days=30)   # ignore charges newer than this
    cutoff_45     = today - timedelta(days=45)   # late fee window

    # ── Use charges endpoint — has Memo + GL account data we need ──
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/charges",
        headers=buildium_headers(),
        params={"limit": 200}
    )
    if r.status_code != 200:
        return None
    charges = r.json()

    # ── Late fee check — proxy for ongoing delinquency ────────────
    # Buildium auto-applies $15 (GL 8) each month to unpaid accounts.
    # If we see one in the last 45 days the account is still delinquent.
    recent_late_fee = any(
        c.get("TotalAmount") == 15.0 and
        datetime.strptime(c["Date"], "%Y-%m-%d").date() >= cutoff_45
        for c in charges
    )

    # ── Aged assessment balance ───────────────────────────────────
    # Only GL 4 (Homeowner Assessments) charges older than 30 days.
    # Excludes newly posted upcoming assessments and all fines/fees.
    aged_assessment_total = sum(
        c.get("TotalAmount", 0)
        for c in charges
        if datetime.strptime(c["Date"], "%Y-%m-%d").date() <= cutoff_30
        and any(l.get("GLAccountId") == CONFIG["gl_assessment"]
                for l in c.get("Lines", []))
    )

    # ── Total aged balance (for attorney $10k threshold) ──────────
    total_aged_balance = sum(
        c.get("TotalAmount", 0)
        for c in charges
        if datetime.strptime(c["Date"], "%Y-%m-%d").date() <= cutoff_30
    )

    # ── Early exit if clearly current ────────────────────────────
    if not recent_late_fee and aged_assessment_total <= 0:
        return None

    # ── Collections notice history (charges endpoint has Memo) ───
    coll_by_gl = [
        c for c in charges
        if any(l.get("GLAccountId") in COLLECTIONS_GL_IDS
               for l in c.get("Lines", []))
    ]
    coll_by_memo = [
        c for c in charges
        if normalize_memo(c.get("Memo", "")) != "unknown"
        and c not in coll_by_gl
    ]
    all_coll = sorted(coll_by_gl + coll_by_memo, key=lambda x: x["Date"])

    # ── Build notice history ──────────────────────────────────────
    notice_history    = []
    last_notice_date  = None
    has_old_charge_memos = False

    for c in all_coll:
        memo  = c.get("Memo", "") or ""
        stage = normalize_memo(memo)
        if stage != "unknown":
            notice_history.append(stage)
            last_notice_date = c["Date"]
        elif memo.strip().lower() in ("charge", ""):
            has_old_charge_memos = True

    # ── Guard: already sent a notice in last 25 days? ────────────
    if last_notice_date:
        last_dt = datetime.strptime(last_notice_date, "%Y-%m-%d").date()
        if (today - last_dt).days < 25:
            return None

    # Skip if no late fee AND no collections history (truly current)
    if not recent_late_fee and not all_coll:
        return None

    if aged_assessment_total <= 0:
        return None

    return {
        "acct_id":            acct_id,
        "notice_history":     notice_history,
        "last_notice_date":   last_notice_date,
        "months_delinquent":  max(len(all_coll), 1),
        "aged_balance":       aged_assessment_total,
        "total_aged_balance": total_aged_balance,
        "has_old_charge_memos": has_old_charge_memos,
    }


# ─────────────────────────────────────────────────────────────────
#  BUILDIUM API HELPERS
# ─────────────────────────────────────────────────────────────────
def buildium_headers() -> dict:
    return {
        "x-buildium-client-id":     CONFIG["buildium_client_id"],
        "x-buildium-client-secret": CONFIG["buildium_client_secret"],
        "Content-Type":             "application/json",
    }


def get_active_owners() -> list[dict]:
    resp = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts",
        headers=buildium_headers(),
        params={"associationids": CONFIG["buildium_association_id"], "limit": 500}
    )
    resp.raise_for_status()
    return [a for a in resp.json() if a.get("Status") == "Active"]


def should_skip(acct: dict) -> str | None:
    """Return a skip reason string, or None if account should be processed."""
    status = (acct.get("DelinquencyStatus") or "").lower()
    if "collection" in status:
        return "InCollections (attorney handling)"
    if "payment" in status and "plan" in status:
        return "PaymentPlan (send regular statement)"
    return None


def post_charge(acct_id: int, amount: float, memo: str, gl_account_id: int):
    if CONFIG["dry_run"]:
        log.info(f"    [DRY RUN] Post ${amount:.2f} → '{memo}'")
        return
    resp = requests.post(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/charges",
        headers=buildium_headers(),
        json={
            "Date":  date.today().isoformat(),
            "Memo":  memo,
            "Lines": [{"Amount": amount, "GlAccountId": gl_account_id}],
        }
    )
    resp.raise_for_status()


def send_ez_mail(acct_id: int, template_name: str, all_addresses: bool):
    if CONFIG["dry_run"]:
        log.info(f"    [DRY RUN] EZ Mail '{template_name}' (all_addresses={all_addresses})")
        return
    resp = requests.post(
        f"{BUILDIUM_BASE}/communications/mailings",
        headers=buildium_headers(),
        json={
            "AssociationIds":      [CONFIG["buildium_association_id"]],
            "OwnershipAccountIds": [acct_id],
            "TemplateName":        template_name,
            "GroupByUnit":         not all_addresses,
            "SendEzMail":          True,
        }
    )
    resp.raise_for_status()


# ─────────────────────────────────────────────────────────────────
#  MAIN MONTHLY RUN
# ─────────────────────────────────────────────────────────────────
def run_monthly_collections():
    today = date.today()
    log.info("=" * 60)
    log.info(f"SBR Collections Run — {today.strftime('%B %d, %Y')}")
    log.info(f"DRY RUN: {CONFIG['dry_run']}")
    log.info("=" * 60)

    owners   = get_active_owners()
    results  = []
    skipped  = []
    flagged  = []   # need manual review
    certified_queue = []
    board_alerts    = []

    for i, acct in enumerate(owners):
        acct_id = acct.get("Id")

        # ── DelinquencyStatus skip ────────────────────────────────
        skip_reason = should_skip(acct)
        if skip_reason:
            skipped.append({"id": acct_id, "reason": skip_reason})
            continue

        # ── Analyze charge history ────────────────────────────────
        analysis = analyze_account(acct_id)
        if analysis is None:
            continue   # current or prepaid

        # ── Accounts with old "Charge" memos need manual review ───
        if analysis["has_old_charge_memos"]:
            flagged.append({
                "id":      acct_id,
                "reason":  "Has legacy 'Charge' memos from prior management — stage unclear",
                "balance": analysis["aged_balance"],
            })
            continue

        # ── Determine next action ─────────────────────────────────
        action = determine_next_action(
            analysis["notice_history"],
            analysis["total_aged_balance"],
        )

        if not action["auto_handle"]:
            board_alerts.append({
                "id":    acct_id,
                "alert": action["board_alert"],
                "months": analysis["months_delinquent"],
                "balance": analysis["aged_balance"],
            })
            continue

        log.info(f"  Account {acct_id} | {analysis['months_delinquent']} months | "
                 f"→ {action['stage_name']} | ${action['fine_amount']:.0f}")

        # ── Post fine ─────────────────────────────────────────────
        if action["fine_amount"] > 0:
            post_charge(
                acct_id,
                action["fine_amount"],
                f"Collections Notice - {action['stage_name']}",
                action["gl_account"],
            )

        # ── EZ Mail ───────────────────────────────────────────────
        if action["letter_template"]:
            send_ez_mail(acct_id, action["letter_template"], action["all_addresses"])

        # ── Queue certified mail ──────────────────────────────────
        if action["certified_mail"]:
            certified_queue.append({
                "id":    acct_id,
                "stage": action["stage_name"],
            })

        if action["board_alert"]:
            board_alerts.append({
                "id":     acct_id,
                "alert":  action["board_alert"],
                "months": analysis["months_delinquent"],
                "balance": analysis["aged_balance"],
            })

        results.append({
            "id":      acct_id,
            "months":  analysis["months_delinquent"],
            "balance": analysis["aged_balance"],
            "stage":   action["stage_name"],
            "fine":    action["fine_amount"],
            "certified": action["certified_mail"],
        })

        if (i + 1) % 50 == 0:
            log.info(f"  ...{i+1}/{len(owners)} scanned")

    log.info(f"\n  Done. Processed {len(results)} | "
             f"Skipped {len(skipped)} | "
             f"Flagged {len(flagged)} | "
             f"Board alerts {len(board_alerts)}")

    send_summary_email(results, skipped, flagged, certified_queue, board_alerts)
    return results


# ─────────────────────────────────────────────────────────────────
#  SUMMARY EMAIL
# ─────────────────────────────────────────────────────────────────
def send_summary_email(processed, skipped, flagged, certified_queue, board_alerts):
    today   = date.today().strftime("%B %d, %Y")
    subject = f"SBR Collections Run — {today}"
    dry     = CONFIG["dry_run"]
    mode    = "⚠️ DRY RUN — nothing posted" if dry else "✅ LIVE RUN"

    lines = [
        f"<h2>Signal Butte Ranch — Monthly Collections</h2>",
        f"<p><b>{today}</b> &nbsp;|&nbsp; <b>{mode}</b></p><hr>",
    ]

    # Board alerts first — highest priority
    if board_alerts:
        lines.append(f"<h3 style='color:red'>🚨 Board Action Required ({len(board_alerts)})</h3><ul>")
        for a in board_alerts:
            lines.append(f"<li>Account {a['id']} — {a['months']} months, "
                         f"${a['balance']:.0f} balance<br><i>{a['alert']}</i></li>")
        lines.append("</ul>")

    # Certified mail approvals
    if certified_queue:
        lines.append(f"<h3 style='color:darkorange'>⚡ Approve Certified Mail Proofs ({len(certified_queue)})</h3>")
        lines.append("<p>Log into <a href='https://app.pageperpage.com'>Page Per Page</a> "
                     "and approve these proofs:</p><ul>")
        for c in certified_queue:
            lines.append(f"<li>Account {c['id']} — {c['stage']}</li>")
        lines.append("</ul>")

    # Manual review needed
    if flagged:
        lines.append(f"<h3>🔍 Manual Review Required ({len(flagged)})</h3>")
        lines.append("<p>These accounts have unclear stage history (legacy charges) "
                     "and need Crystal to determine the correct next notice:</p><ul>")
        for f in flagged:
            lines.append(f"<li>Account {f['id']} — ${f['balance']:.0f} balance — {f['reason']}</li>")
        lines.append("</ul>")

    # Processed
    if processed:
        total_fines = sum(r["fine"] for r in processed)
        lines.append(f"<h3>✅ Processed Automatically ({len(processed)} accounts — ${total_fines:.0f} in fines)</h3>")
        lines.append("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse'>")
        lines.append("<tr><th>Account</th><th>Months</th><th>Balance</th>"
                     "<th>Stage</th><th>Fine</th><th>Cert Mail</th></tr>")
        for r in sorted(processed, key=lambda x: x["months"], reverse=True):
            cert = "YES" if r["certified"] else ""
            lines.append(
                f"<tr><td>{r['id']}</td><td>{r['months']}</td>"
                f"<td>${r['balance']:.0f}</td><td>{r['stage']}</td>"
                f"<td>${r['fine']:.0f}</td><td>{cert}</td></tr>"
            )
        lines.append("</table>")

    # Skipped
    if skipped:
        lines.append(f"<h3>⏭️ Skipped ({len(skipped)})</h3><ul>")
        for s in skipped:
            lines.append(f"<li>Account {s['id']} — {s['reason']}</li>")
        lines.append("</ul>")

    lines.append("<hr><p style='color:gray;font-size:11px'>SBR Collections Automation v2.0</p>")
    body = "\n".join(lines)

    if dry:
        log.info(f"\n── EMAIL PREVIEW ──────────────────────────────")
        log.info(f"To: {CONFIG['email_to']}  |  Subject: {subject}")
        log.info(f"Board alerts: {len(board_alerts)} | "
                 f"Certified mail: {len(certified_queue)} | "
                 f"Manual review: {len(flagged)} | "
                 f"Processed: {len(processed)}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = CONFIG["email_to"]
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(CONFIG["email_from"], CONFIG["email_password"])
        smtp.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
    log.info(f"Summary email sent to {CONFIG['email_to']}")


if __name__ == "__main__":
    run_monthly_collections()