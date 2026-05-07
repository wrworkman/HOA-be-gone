"""
Signal Butte Ranch HOA — Payment Plan Monitor  v1.0
====================================================
Runs monthly alongside the main collections automation.
For every account marked PaymentPlan in Buildium:

  1. Pulls all transactions for the last 35 days
  2. Calculates total paid this cycle
  3. Checks whether the minimum payment was met ($120)
  4. If ON TRACK  → posts a note to Buildium + sends courtesy statement to owner
  5. If FAILING   → alerts Crystal immediately + posts a warning note to Buildium
  6. Sends Crystal a full summary email at the end

STOPPING CONDITIONS:
  - Account not marked PaymentPlan → skip (handled by main automation)
  - Account balance ≤ $0 → flag as PAID OFF, notify Crystal to clear the status

SETUP:
  pip install requests python-dotenv
  Uses same .env / GitHub Secrets as sbr_collections_automation.py
"""

import os
import requests
import smtplib
import logging
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("payment_plan_monitor")

# ─────────────────────────────────────────────────────────────────
#  CONFIG — matches sbr_collections_automation.py exactly
# ─────────────────────────────────────────────────────────────────
CONFIG = {
    "buildium_client_id":      os.environ["BUILDIUM_CLIENT_ID"],
    "buildium_client_secret":  os.environ["BUILDIUM_CLIENT_SECRET"],
    "buildium_association_id": "103158",

    "gl_assessment": 4,    # Income- Homeowner Assessments
    "gl_late_fee":   8,    # Income- Late Fees

    "monthly_assessment":       62.00,
    "payment_plan_min_payment": 120.00,   # minimum qualifying monthly payment

    "email_from":     os.environ["EMAIL_FROM"],
    "email_password": os.environ["EMAIL_PASSWORD"],
    "email_to":       os.environ.get("EMAIL_TO", "sbrneighbors@gmail.com"),

    "hoa_name":     "Signal Butte Ranch HOA",

    "dry_run": False,   # set True to preview without posting notes or sending emails
}

BUILDIUM_BASE = "https://api.buildium.com/v1"


# ─────────────────────────────────────────────────────────────────
#  BUILDIUM HELPERS
# ─────────────────────────────────────────────────────────────────
def bh() -> dict:
    """Buildium auth headers."""
    return {
        "x-buildium-client-id":     CONFIG["buildium_client_id"],
        "x-buildium-client-secret": CONFIG["buildium_client_secret"],
        "Content-Type":             "application/json",
    }


def get_payment_plan_accounts() -> list[dict]:
    """Return all active ownership accounts with DelinquencyStatus = PaymentPlan."""
    resp = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts",
        headers=bh(),
        params={"associationids": CONFIG["buildium_association_id"], "limit": 500},
    )
    resp.raise_for_status()
    results = []
    for acct in resp.json():
        status = (acct.get("DelinquencyStatus") or "").lower()
        if acct.get("Status") == "Active" and "payment" in status and "plan" in status:
            results.append(acct)
    log.info(f"Found {len(results)} PaymentPlan account(s)")
    return results


def get_owner_name(acct_id: int) -> str:
    """Return primary owner display name for an ownership account."""
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}",
        headers=bh(),
    )
    if r.status_code != 200:
        return f"Account {acct_id}"
    data = r.json()
    owners = data.get("Owners") or []
    if owners:
        o = owners[0]
        first = o.get("FirstName", "")
        last  = o.get("LastName", "")
        return f"{first} {last}".strip() or f"Account {acct_id}"
    return f"Account {acct_id}"


def get_unit_address(acct_id: int) -> str:
    """Return the SBR property address for this ownership account."""
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}",
        headers=bh(),
    )
    if r.status_code != 200:
        return ""
    unit_id = r.json().get("UnitId")
    if not unit_id:
        return ""
    r2 = requests.get(f"{BUILDIUM_BASE}/associations/units/{unit_id}", headers=bh())
    if r2.status_code != 200:
        return ""
    addr = r2.json().get("Address") or {}
    line1 = addr.get("AddressLine1", "")
    city  = addr.get("City", "")
    state = addr.get("State", "")
    return f"{line1}, {city}, {state}".strip(", ")


def get_account_balance(acct_id: int) -> float:
    """
    Return the current total balance on the account.
    Pulls all charges and sums them — positive = still owes money.
    """
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/charges",
        headers=bh(),
        params={"limit": 200},
    )
    if r.status_code != 200:
        return 0.0
    return sum(c.get("TotalAmount", 0) or 0 for c in r.json())


def get_recent_payments(acct_id: int, days: int = 35) -> list[dict]:
    """Return payment/credit transactions posted in the last `days` days."""
    cutoff = date.today() - timedelta(days=days)
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/transactions",
        headers=bh(),
        params={"limit": 100},
    )
    if r.status_code != 200:
        return []
    results = []
    for t in r.json():
        ttype    = (t.get("TransactionTypeEnum") or "").lower()
        txn_date = datetime.strptime(t["Date"], "%Y-%m-%d").date()
        if txn_date >= cutoff and ("payment" in ttype or "credit" in ttype):
            results.append(t)
    return results


def get_all_payments(acct_id: int) -> list[dict]:
    """Return all payment/credit transactions ever on this account."""
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/transactions",
        headers=bh(),
        params={"limit": 200},
    )
    if r.status_code != 200:
        return []
    return [
        t for t in r.json()
        if "payment" in (t.get("TransactionTypeEnum") or "").lower()
        or "credit"  in (t.get("TransactionTypeEnum") or "").lower()
    ]


def post_buildium_note(acct_id: int, note: str):
    """Post a note to the ownership account in Buildium."""
    if CONFIG["dry_run"]:
        log.info(f"    [DRY RUN] Would post note to {acct_id}: {note[:80]}...")
        return
    r = requests.post(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/notes",
        headers=bh(),
        json={"Note": note},
    )
    if r.status_code not in (200, 201):
        log.warning(f"    Note post failed for {acct_id}: {r.status_code} {r.text[:200]}")


# ─────────────────────────────────────────────────────────────────
#  ACCOUNT ANALYSIS
# ─────────────────────────────────────────────────────────────────
def analyze_payment_plan(acct: dict) -> dict:
    """
    Full analysis for one PaymentPlan account.
    Returns a result dict with status, amounts, and recommended action.
    """
    acct_id    = acct["Id"]
    owner_name = get_owner_name(acct_id)
    unit_addr  = get_unit_address(acct_id)
    balance    = get_account_balance(acct_id)
    recent_payments = get_recent_payments(acct_id, days=35)

    total_paid_this_cycle = sum(
        abs(t.get("TotalAmount", 0) or 0) for t in recent_payments
    )
    qualifying_payments = [
        t for t in recent_payments
        if abs(t.get("TotalAmount", 0) or 0) >= CONFIG["payment_plan_min_payment"]
    ]
    min_met = len(qualifying_payments) > 0

    # Detect paid-off accounts
    paid_off = balance <= 0

    # Most recent payment date across all time (for context in alerts)
    all_pmts = get_all_payments(acct_id)
    last_payment_date = (
        max(t["Date"] for t in all_pmts) if all_pmts else "never"
    )

    return {
        "acct_id":              acct_id,
        "owner_name":           owner_name,
        "unit_address":         unit_addr,
        "balance":              balance,
        "total_paid_this_cycle": total_paid_this_cycle,
        "qualifying_payments":  qualifying_payments,
        "min_met":              min_met,
        "paid_off":             paid_off,
        "last_payment_date":    last_payment_date,
        "min_required":         CONFIG["payment_plan_min_payment"],
    }


# ─────────────────────────────────────────────────────────────────
#  ACTIONS
# ─────────────────────────────────────────────────────────────────
def handle_paid_off(result: dict):
    """Account balance is $0 or less — plan complete."""
    log.info(f"  ✅ PAID OFF: {result['owner_name']} ({result['unit_address']})")
    note = (
        f"[Payment Plan Monitor — {date.today()}]\n"
        f"Account balance is now ${result['balance']:.2f}. "
        f"Payment plan appears COMPLETE. "
        f"Please remove the PaymentPlan status from this account."
    )
    post_buildium_note(result["acct_id"], note)


def handle_on_track(result: dict):
    """Account made a qualifying payment this cycle — post confirmation note."""
    log.info(f"  ✅ ON TRACK: {result['owner_name']} — paid ${result['total_paid_this_cycle']:.2f} this cycle, balance ${result['balance']:.2f}")
    payment_lines = "\n".join(
        f"  • ${abs(p.get('TotalAmount',0)):.2f} on {p['Date']}"
        for p in sorted(result["qualifying_payments"], key=lambda x: x["Date"])
    )
    note = (
        f"[Payment Plan Monitor — {date.today()}]\n"
        f"Payment plan ON TRACK.\n"
        f"Payments received this cycle:\n{payment_lines}\n"
        f"Total this cycle: ${result['total_paid_this_cycle']:.2f}\n"
        f"Remaining balance: ${result['balance']:.2f}"
    )
    post_buildium_note(result["acct_id"], note)


def handle_failing(result: dict):
    """No qualifying payment received — post warning note."""
    log.warning(f"  ⚠️  FAILING: {result['owner_name']} — no qualifying payment. Last payment: {result['last_payment_date']}")
    note = (
        f"[Payment Plan Monitor — {date.today()}] ⚠️ PAYMENT PLAN FAILING\n"
        f"No payment of ${result['min_required']:.2f}+ received in the last 35 days.\n"
        f"Total received this cycle: ${result['total_paid_this_cycle']:.2f}\n"
        f"Last payment on record: {result['last_payment_date']}\n"
        f"Remaining balance: ${result['balance']:.2f}\n"
        f"ACTION NEEDED: If no payment arrangement is confirmed, "
        f"remove the PaymentPlan status so collections resume next month."
    )
    post_buildium_note(result["acct_id"], note)


# ─────────────────────────────────────────────────────────────────
#  SUMMARY EMAIL TO CRYSTAL
# ─────────────────────────────────────────────────────────────────
def send_summary_email(on_track: list, failing: list, paid_off: list):
    today_str = date.today().strftime("%B %d, %Y")

    def fmt_row(r):
        return (
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{r['owner_name']}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{r['unit_address']}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>${r['total_paid_this_cycle']:.2f}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>${r['balance']:.2f}</td>"
            f"</tr>"
        )

    def fmt_failing_row(r):
        return (
            f"<tr style='background:#fff3cd'>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{r['owner_name']}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{r['unit_address']}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>${r['total_paid_this_cycle']:.2f}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>${r['balance']:.2f}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;color:#b45309'>{r['last_payment_date']}</td>"
            f"</tr>"
        )

    def fmt_paid_row(r):
        return (
            f"<tr style='background:#d1fae5'>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{r['owner_name']}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{r['unit_address']}</td>"
            f"<td colspan='2' style='padding:6px 12px;border-bottom:1px solid #eee;color:#065f46'>"
            f"PAID OFF — Please remove PaymentPlan status</td>"
            f"</tr>"
        )

    table_header = (
        "<table style='border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px'>"
        "<thead><tr style='background:#f3f4f6'>"
        "<th style='padding:8px 12px;text-align:left'>Owner</th>"
        "<th style='padding:8px 12px;text-align:left'>Address</th>"
        "<th style='padding:8px 12px;text-align:right'>Paid This Cycle</th>"
        "<th style='padding:8px 12px;text-align:right'>Balance</th>"
        "</tr></thead><tbody>"
    )

    failing_header = (
        "<table style='border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px'>"
        "<thead><tr style='background:#fef3c7'>"
        "<th style='padding:8px 12px;text-align:left'>Owner</th>"
        "<th style='padding:8px 12px;text-align:left'>Address</th>"
        "<th style='padding:8px 12px;text-align:right'>Paid This Cycle</th>"
        "<th style='padding:8px 12px;text-align:right'>Balance</th>"
        "<th style='padding:8px 12px;text-align:left'>Last Payment</th>"
        "</tr></thead><tbody>"
    )

    # Build sections
    failing_section = ""
    if failing:
        failing_section = f"""
        <div style='background:#fffbeb;border-left:4px solid #f59e0b;padding:16px;margin:24px 0;border-radius:4px'>
          <h3 style='color:#92400e;margin:0 0 12px'>⚠️ Payment Plans Failing ({len(failing)})</h3>
          <p style='color:#78350f;margin:0 0 12px'>
            These homeowners did not make a qualifying payment of ${CONFIG['payment_plan_min_payment']:.0f}+
            in the last 35 days. If no arrangement is confirmed, remove their PaymentPlan
            status in Buildium so collections resume automatically next month.
          </p>
          {failing_header}
          {''.join(fmt_failing_row(r) for r in failing)}
          </tbody></table>
        </div>
        """

    on_track_section = ""
    if on_track:
        on_track_section = f"""
        <div style='margin:24px 0'>
          <h3 style='color:#065f46;margin:0 0 12px'>✅ Payment Plans On Track ({len(on_track)})</h3>
          {table_header}
          {''.join(fmt_row(r) for r in on_track)}
          </tbody></table>
        </div>
        """

    paid_off_section = ""
    if paid_off:
        paid_off_section = f"""
        <div style='background:#ecfdf5;border-left:4px solid #10b981;padding:16px;margin:24px 0;border-radius:4px'>
          <h3 style='color:#065f46;margin:0 0 12px'>🎉 Payment Plans Complete ({len(paid_off)})</h3>
          <p style='color:#065f46;margin:0 0 12px'>
            These accounts have a $0 balance. Remove their PaymentPlan status in Buildium.
          </p>
          {table_header}
          {''.join(fmt_paid_row(r) for r in paid_off)}
          </tbody></table>
        </div>
        """

    total_accounts = len(on_track) + len(failing) + len(paid_off)
    subject_flag = " ⚠️ ACTION NEEDED" if failing or paid_off else ""

    html = f"""
    <html><body style='font-family:Arial,sans-serif;color:#1f2937;max-width:800px;margin:0 auto;padding:20px'>
      <h2 style='color:#1e3a5f;border-bottom:2px solid #1e3a5f;padding-bottom:8px'>
        {CONFIG['hoa_name']} — Payment Plan Monitor
      </h2>
      <p style='color:#6b7280'>Run date: {today_str} &nbsp;|&nbsp; Accounts reviewed: {total_accounts}</p>

      {failing_section}
      {paid_off_section}
      {on_track_section}

      <p style='color:#9ca3af;font-size:12px;margin-top:32px;border-top:1px solid #e5e7eb;padding-top:12px'>
        Notes have been posted to each account in Buildium. This report is generated automatically
        each month alongside the collections automation.
      </p>
    </body></html>
    """

    if CONFIG["dry_run"]:
        log.info("[DRY RUN] Would send summary email")
        log.info(f"  On track: {len(on_track)}  |  Failing: {len(failing)}  |  Paid off: {len(paid_off)}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"SBR Payment Plan Monitor — {today_str}{subject_flag}"
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = CONFIG["email_to"]
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(CONFIG["email_from"], CONFIG["email_password"])
            server.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
        log.info("Summary email sent to Crystal")
    except Exception as e:
        log.error(f"Failed to send summary email: {e}")


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("SBR Payment Plan Monitor starting")
    log.info(f"Date: {date.today()}  |  Dry run: {CONFIG['dry_run']}")
    log.info("=" * 60)

    accounts = get_payment_plan_accounts()
    if not accounts:
        log.info("No PaymentPlan accounts found — nothing to do.")
        return

    on_track = []
    failing  = []
    paid_off = []

    for acct in accounts:
        acct_id = acct["Id"]
        log.info(f"Processing account {acct_id}...")

        try:
            result = analyze_payment_plan(acct)

            if result["paid_off"]:
                handle_paid_off(result)
                paid_off.append(result)
            elif result["min_met"]:
                handle_on_track(result)
                on_track.append(result)
            else:
                handle_failing(result)
                failing.append(result)

        except Exception as e:
            log.error(f"  Error processing account {acct_id}: {e}")
            continue

    log.info("-" * 60)
    log.info(f"Complete — On track: {len(on_track)} | Failing: {len(failing)} | Paid off: {len(paid_off)}")

    send_summary_email(on_track, failing, paid_off)


if __name__ == "__main__":
    main()
