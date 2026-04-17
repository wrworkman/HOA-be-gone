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
import sys
import os
import smtplib
import logging
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# Load credentials from .env file (never committed to GitHub)
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sbr_collections")

# ─────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────
CONFIG = {
    # Credentials loaded from .env — never hardcoded here
    "buildium_client_id":      os.environ["BUILDIUM_CLIENT_ID"],
    "buildium_client_secret":  os.environ["BUILDIUM_CLIENT_SECRET"],
    "buildium_association_id": "103158",

    # GL Account IDs — confirmed 4/10/2026
    "gl_demand_notices":   51537,   # Income- Collections Demand Notices
    "gl_certified_mail":   51538,   # Income- Collections Certified Notices
    "gl_collections_misc": 51539,   # Income- Collections Notices (misc/older charges)
    "gl_lien_filing":      67944,   # Income- Collections Lien Filing
    "gl_late_fee":         8,       # Income- Late Fees  (Buildium auto-applies $15)
    "gl_assessment":       4,       # Income- Homeowner Assessments

    # Monthly assessment amount — used to detect prepayment
    "monthly_assessment":  62.00,

    # Attorney thresholds
    "attorney_months":     17,      # months continuously delinquent → Pre-Legal 60-Day
    "attorney_balance":    10000.00,# total past-due balance

    # Payment plan detection
    # A delinquent account is a payment plan CANDIDATE if:
    #   1. Their past-due balance exceeds payment_plan_min_balance (account still
    #      meaningfully delinquent — not someone who's nearly paid off)
    #   2. They made at least one payment >= payment_plan_min_payment in the last
    #      35 days (one billing cycle)
    # Crystal then sets DelinquencyStatus = PaymentPlan in Buildium → script
    # skips them in future runs unless the plan fails.
    #
    # A PaymentPlan account is FAILING if no payment >= payment_plan_min_payment
    # was received in the last 35 days. Crystal removes the PaymentPlan status
    # and the script resumes collections automatically next month.
    #
    # Balance threshold of $428 ≈ 4 months of assessments ($248) + 4 late fees
    # ($60) + at least one $40 collections fine ($120 total). This ensures we
    # only flag accounts that have genuinely missed 4+ consecutive months — not
    # quarterly payers who are simply behind by one quarter and about to catch up.
    # Minimum payment of $120 is intentionally below the quarterly $186 so that
    # partial catch-up payments on a plan still qualify.
    "payment_plan_min_payment": 120.00,  # min monthly payment to qualify / maintain plan
    "payment_plan_min_balance": 428.00,  # min aged balance to be considered ($428 ≈ 4 months assessments + late fees)

    # Carry-over / high-balance manual review
    # Accounts with NO collections history that exceed EITHER threshold are
    # flagged for Crystal to review before the script touches them.
    # These are likely carry-overs from prior management who were never
    # properly entered into the collections process.
    #   balance_threshold: total aged balance in dollars
    #   months_threshold:  number of $15 late fees (= months continuously late)
    "carryover_balance_threshold": 2000.00,  # $2,000+ with no history → review
    "carryover_months_threshold":  6,        # 6+ consecutive months late with no history → review
    # NOTE: SBR implemented collections in Jan 2026. Accounts with pre-Jan 2026
    # late fees that are genuinely new to the process should NOT be flagged.
    # Raise this threshold if too many false positives appear.

    # Email summary — loaded from .env
    "email_from":          os.environ["EMAIL_FROM"],
    "email_password":      os.environ["EMAIL_PASSWORD"],
    "email_to":            os.environ.get("EMAIL_TO", "sbrneighbors@gmail.com"),

    # Lob.com physical mail — loaded from .env
    # test_ key for test mode (no real mail sent, free), live_ key for production
    "lob_api_key":         os.environ.get("LOB_API_KEY", ""),

    # HOA identity — printed on every letter
    "hoa_name":            "Signal Butte Ranch HOA",
    "hoa_address_line1":   "304 South Jones Boulevard #5432",
    "hoa_address_city":    "Las Vegas",
    "hoa_address_state":   "NV",
    "hoa_address_zip":     "89107",
    # Remittance address shown at the bottom of every letter
    "hoa_remittance":      "P.O. Box 98526, Phoenix, AZ 85038-0526",

    # Safety
    "dry_run": False,  # LIVE — charges and emails are real
}

BUILDIUM_BASE = "https://api.buildium.com/v1"
COLLECTIONS_GL_IDS = {
    CONFIG["gl_demand_notices"],
    CONFIG["gl_certified_mail"],
    CONFIG["gl_collections_misc"],
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
    # "Advanced Stage of Delinquency" — checked explicitly before 120/90/60 to
    # avoid any accidental number-based match if the memo text changes slightly.
    if re.search(r"advanced|advance|adv\b|post.lien", m):
        return "advanced"
    if re.search(r"\b120\b", m):
        return "day_120"
    if re.search(r"\b90\b", m):
        return "day_90"
    if re.search(r"\b60\b", m):
        return "day_60"
    return "unknown"


# ─────────────────────────────────────────────────────────────────
#  STAGE DETERMINATION
#  Based on the FULL history of collections notices, determine
#  the true current stage and what action to take next.
# ─────────────────────────────────────────────────────────────────
def determine_next_action(notice_history: list[str], total_assessment_balance: float,
                          months_delinquent_total: int = 0) -> dict:
    """
    Given the normalized history of notices sent (oldest first),
    the estimated total assessment balance, and total months delinquent,
    return the next action.

    Memo format (standardized for searchability in Buildium):
      SBR | 60-Day Collections Notice
      SBR | 90-Day Collections Notice
      SBR | 120-Day Collections Notice
      SBR | 150-Day Pre-Lien Notice
      SBR | 180-Day Lien Notice
      SBR | Advanced Delinquency | Month 07  (through Month 16)
      SBR | Pre-Legal 60-Day Notice           (Month 17)
      SBR | Pre-Legal Final Notice            (Month 18)
    """
    # Count each stage
    counts = {}
    for n in notice_history:
        counts[n] = counts.get(n, 0) + 1

    # Use the larger of notice history length or months_delinquent_total
    months = max(len(notice_history), months_delinquent_total)

    # ── Board alert: Pre-Legal Final already sent ─────────────────
    if counts.get("pre_legal_final", 0) >= 1:
        return {
            "stage_name":      "BOARD ALERT — Ready for Attorney",
            "memo":            None,
            "fine_amount":     0,
            "gl_account":      None,
            "certified_mail":  False,
            "letter_template": None,
            "all_addresses":   True,
            "board_alert":     "Pre-Legal Final Notice already sent. Turn over to collections attorney.",
            "auto_handle":     False,
        }

    # ── Month 18: Pre-Legal Final Notice ─────────────────────────
    if counts.get("pre_legal_60", 0) >= 1 and counts.get("pre_legal_final", 0) == 0:
        return {
            "stage_name":      "Pre-Legal Final Notice",
            "memo":            "SBR | Pre-Legal Final Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_certified_mail"],
            "certified_mail":  True,
            "letter_template": "30 Days Prior to Legal Collections (Final Letter)",
            "all_addresses":   True,
            "board_alert":     "Pre-Legal Final Notice being sent. Attorney referral in 30 days.",
            "auto_handle":     True,
        }

    # ── Month 17: Pre-Legal 60-Day Notice ────────────────────────
    # Triggered by attorney threshold (17+ months OR $10k+) while in Advanced
    advanced_count = counts.get("advanced", 0)
    attorney_threshold_met = (
        months >= CONFIG["attorney_months"] or
        total_assessment_balance >= CONFIG["attorney_balance"]
    )

    if attorney_threshold_met and advanced_count >= 1:
        return {
            "stage_name":      "Pre-Legal 60-Day Notice",
            "memo":            "SBR | Pre-Legal 60-Day Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_certified_mail"],
            "certified_mail":  True,
            "letter_template": "60 Days Prior to Legal Collections (2nd to Last Letter)",
            "all_addresses":   True,
            "board_alert":     f"Account at {months} months / ${total_assessment_balance:.0f} past due. Pre-legal 60-day notice being sent.",
            "auto_handle":     True,
        }

    # ── Months 07–16: Advanced Delinquency (numbered) ────────────
    if counts.get("advanced", 0) >= 1 or counts.get("lien_180", 0) >= 1:
        # Month number = total months delinquent, clamped to 07–16
        adv_month = max(7, min(16, months))
        return {
            "stage_name":      f"Advanced Delinquency | Month {adv_month:02d}",
            "memo":            f"SBR | Advanced Delinquency | Month {adv_month:02d}",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_demand_notices"],
            "certified_mail":  False,
            "letter_template": "Advanced Stage of Delinquency",
            "all_addresses":   True,
            "board_alert":     None,
            "auto_handle":     True,
        }

    # ── Month 06: 180-Day Lien Notice ────────────────────────────
    if counts.get("prelien_150", 0) >= 1:
        return {
            "stage_name":      "180-Day Lien Notice",
            "memo":            "SBR | 180-Day Lien Notice",
            "fine_amount":     250.0,
            "gl_account":      CONFIG["gl_lien_filing"],
            "certified_mail":  True,
            "letter_template": "180-Day Late & Lien Recorded Notice",
            "all_addresses":   True,
            "board_alert":     "Lien being filed this cycle.",
            "auto_handle":     True,
        }

    # ── Month 05: 150-Day Pre-Lien Notice ────────────────────────
    if counts.get("day_120", 0) >= 1:
        return {
            "stage_name":      "150-Day Pre-Lien Notice",
            "memo":            "SBR | 150-Day Pre-Lien Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_certified_mail"],
            "certified_mail":  True,
            "letter_template": "150-Day Late & Notice of Intent to Record a Lien",
            "all_addresses":   True,
            "board_alert":     None,
            "auto_handle":     True,
        }

    # ── Month 04: 120-Day Collection Notice ──────────────────────
    if counts.get("day_90", 0) >= 1:
        return {
            "stage_name":      "120-Day Collection Notice",
            "memo":            "SBR | 120-Day Collections Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_demand_notices"],
            "certified_mail":  False,
            "letter_template": "120-Day Late Notice",
            "all_addresses":   True,
            "board_alert":     None,
            "auto_handle":     True,
        }

    # ── Month 03: 90-Day Collection Notice ───────────────────────
    if counts.get("day_60", 0) >= 1:
        return {
            "stage_name":      "90-Day Collection Notice",
            "memo":            "SBR | 90-Day Collections Notice",
            "fine_amount":     40.0,
            "gl_account":      CONFIG["gl_demand_notices"],
            "certified_mail":  False,
            "letter_template": "90-Day Late Notice",
            "all_addresses":   False,
            "board_alert":     None,
            "auto_handle":     True,
        }

    # ── Month 02: 60-Day Collection Notice (first notice) ────────
    return {
        "stage_name":      "60-Day Collection Notice",
        "memo":            "SBR | 60-Day Collections Notice",
        "fine_amount":     40.0,
        "gl_account":      CONFIG["gl_demand_notices"],
        "certified_mail":  False,
        "letter_template": "60-Day Late Notice",
        "all_addresses":   False,
        "board_alert":     None,
        "auto_handle":     True,
    }


# ─────────────────────────────────────────────────────────────────
#  CONSECUTIVE LATE FEE STREAK
#  Counts the current unbroken run of monthly $15 late fees.
#  A gap > 35 days breaks the streak (account was brought current).
#  NOTE: No February 2026 exception here — payment detection below
#  handles the "paid off then restarted" case more reliably.
#  Accounts that were genuinely continuous through Feb 2026 still
#  have their full notice_history from prior stage memos, so stage
#  logic works correctly even if streak shows a lower number.
# ─────────────────────────────────────────────────────────────────
def count_consecutive_late_fees(all_late_fees: list) -> int:
    if not all_late_fees:
        return 0

    sorted_fees = sorted(all_late_fees, key=lambda c: c["Date"], reverse=True)
    streak = 1

    for i in range(len(sorted_fees) - 1):
        newer = datetime.strptime(sorted_fees[i    ]["Date"], "%Y-%m-%d").date()
        older = datetime.strptime(sorted_fees[i + 1]["Date"], "%Y-%m-%d").date()
        gap   = (newer - older).days

        if gap <= 35:
            streak += 1
        else:
            break   # Gap found — streak resets here

    return streak


# ─────────────────────────────────────────────────────────────────
#  PAYMENT RESET DETECTION
#  Checks the transactions endpoint (which includes payments) to see
#  if a significant payment was made AFTER the most recent collections
#  notice. If yes, the account paid off its debt and any restart of
#  delinquency should be treated as a fresh delinquency, not a
#  continuation of the prior collections process.
#
#  Only called for accounts with existing notice_history AND a short
#  consecutive streak (≤ 2 months) — the exact scenario where a
#  payoff-and-restart could be confused with ongoing delinquency.
# ─────────────────────────────────────────────────────────────────
def paid_off_after_last_notice(acct_id: int, last_notice_date: str) -> bool:
    """
    Returns True if a payment >= one month's assessment was made after
    last_notice_date, indicating the account paid off following that notice.
    """
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/transactions",
        headers=buildium_headers(),
        params={"limit": 50},
    )
    if r.status_code != 200:
        return False

    notice_dt = datetime.strptime(last_notice_date, "%Y-%m-%d").date()

    for t in r.json():
        ttype    = (t.get("TransactionTypeEnum") or "").lower()
        amount   = abs(t.get("TotalAmount", 0) or 0)
        txn_date = datetime.strptime(t["Date"], "%Y-%m-%d").date()

        if txn_date <= notice_dt:
            continue   # Before the last notice — not relevant

        # A payment or credit >= 3 months of assessment after the last notice
        # is strong evidence the account made a significant payoff.
        # Using 3× monthly assessment ($186 for SBR) to avoid treating small
        # partial payments as a full reset. Accounts like 22398 ($722 payment)
        # and 22546 ($1,318 payment) will correctly trigger; routine partials won't.
        if ("payment" in ttype or "credit" in ttype) and amount >= (CONFIG["monthly_assessment"] * 3):
            return True

    return False


# ─────────────────────────────────────────────────────────────────
#  PAYMENT PLAN HELPERS
#
#  check_payment_plan_candidate()
#    Called for accounts ACTIVELY being processed (not yet on a plan).
#    Flags if they've made even one qualifying payment this cycle,
#    suggesting Crystal has an informal arrangement with them.
#    Requires the account to still carry a meaningful balance so we
#    don't flag someone who's basically paid off.
#
#  check_payment_plan_failing()
#    Called for accounts ALREADY marked PaymentPlan in Buildium.
#    If no qualifying payment has arrived in the last 35 days, the
#    plan is failing and collections should resume.
#
#  Both use the same threshold: payment_plan_min_payment ($120).
# ─────────────────────────────────────────────────────────────────
def _get_recent_transactions(acct_id: int, days: int = 35) -> list:
    """Fetch transactions for an account, returning those within `days` days."""
    cutoff = date.today() - timedelta(days=days)
    r = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/transactions",
        headers=buildium_headers(),
        params={"limit": 50},
    )
    if r.status_code != 200:
        return []
    results = []
    for t in r.json():
        txn_date = datetime.strptime(t["Date"], "%Y-%m-%d").date()
        if txn_date >= cutoff:
            results.append(t)
    return results


def check_payment_plan_candidate(acct_id: int, aged_balance: float) -> dict | None:
    """
    Returns a candidate dict if a delinquent account (not yet formally on a
    payment plan) made at least one payment >= payment_plan_min_payment in the
    last 35 days AND still carries a meaningful balance.

    A single $120 payment on a delinquent account signals Crystal may have
    verbally agreed to a plan. She should formally set PaymentPlan in Buildium
    to pause future collection notices until the balance is cleared or the plan
    fails.

    Ignored if:
      - aged_balance < payment_plan_min_balance (account is nearly clear — let
        it pay off naturally; the late-fee signal will clear it next month)
      - No payment >= min_payment found in the last 35 days
    """
    min_payment = CONFIG["payment_plan_min_payment"]
    min_balance = CONFIG["payment_plan_min_balance"]

    # Don't flag accounts that are close to paid off — they'll clear themselves
    if aged_balance < min_balance:
        return None

    txns = _get_recent_transactions(acct_id, days=35)
    qualifying = [
        t for t in txns
        if ("payment" in (t.get("TransactionTypeEnum") or "").lower() or
            "credit"  in (t.get("TransactionTypeEnum") or "").lower())
        and abs(t.get("TotalAmount", 0) or 0) >= min_payment
    ]

    if not qualifying:
        return None

    total_paid = sum(abs(t.get("TotalAmount", 0) or 0) for t in qualifying)
    last_date  = max(t["Date"] for t in qualifying)
    return {
        "acct_id":           acct_id,
        "payment_count":     len(qualifying),
        "total_paid":        total_paid,
        "last_payment_date": last_date,
        "aged_balance":      aged_balance,
    }


def check_payment_plan_failing(acct_id: int) -> dict | None:
    """
    Called for accounts ALREADY marked PaymentPlan in Buildium.
    If no payment >= payment_plan_min_payment was received in the last 35 days,
    the plan is failing and Crystal should remove the PaymentPlan status so the
    script resumes collections automatically next month.

    Returns a dict with details, or None if the plan is on track.
    """
    min_payment = CONFIG["payment_plan_min_payment"]
    txns = _get_recent_transactions(acct_id, days=35)

    qualifying = [
        t for t in txns
        if ("payment" in (t.get("TransactionTypeEnum") or "").lower() or
            "credit"  in (t.get("TransactionTypeEnum") or "").lower())
        and abs(t.get("TotalAmount", 0) or 0) >= min_payment
    ]

    if qualifying:
        return None  # Plan is on track — skip

    # Find their most recent payment of any size to show Crystal
    any_payment = sorted(
        [t for t in txns
         if "payment" in (t.get("TransactionTypeEnum") or "").lower()
         or "credit"  in (t.get("TransactionTypeEnum") or "").lower()],
        key=lambda t: t["Date"], reverse=True,
    )
    last_any = any_payment[0]["Date"] if any_payment else "none in last 35 days"

    return {
        "acct_id":            acct_id,
        "last_payment_date":  last_any,
        "min_required":       min_payment,
    }


# ─────────────────────────────────────────────────────────────────
#  DELINQUENCY DETECTION
# ─────────────────────────────────────────────────────────────────
def analyze_account(acct_id: int, debug: bool = False) -> dict | None:
    """
    Pull charge history for one account.
    Returns analysis dict if delinquent, None if current/skip.
    Pass debug=True to log every decision point for a specific account.
    """
    def dbg(msg):
        if debug:
            log.info(f"    [DEBUG {acct_id}] {msg}")

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
    # Collect ALL $15 late fees so we can detect the current streak.
    all_late_fees = [
        c for c in charges
        if c.get("TotalAmount") == 15.0
        and any(l.get("GLAccountId") == CONFIG["gl_late_fee"]
                for l in c.get("Lines", []))
    ]

    # Consecutive streak = unbroken run of monthly late fees.
    # Resets to zero if the account was ever brought current.
    # This is what we use for stage logic — lifetime total is irrelevant
    # because paying off resets the delinquency clock.
    consecutive_months = count_consecutive_late_fees(all_late_fees)

    # "Recent" = a late fee posted in the last 45 days = still actively delinquent.
    recent_late_fee = any(
        datetime.strptime(c["Date"], "%Y-%m-%d").date() >= cutoff_45
        for c in all_late_fees
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

    dbg(f"late_fees={len(all_late_fees)}  consecutive={consecutive_months}  recent={recent_late_fee}  aged_assess=${aged_assessment_total:.2f}  total_aged=${total_aged_balance:.2f}")
    if all_late_fees:
        dates = [c["Date"] for c in sorted(all_late_fees, key=lambda x: x["Date"], reverse=True)[:3]]
        dbg(f"most recent late fee dates: {dates}  |  cutoff_45={cutoff_45}")

    # ── Early exit if clearly current ────────────────────────────
    if not recent_late_fee and aged_assessment_total <= 0:
        dbg("EXIT: no recent late fee + no aged assessment balance → current")
        return None

    # Collections fine amounts — used to distinguish assessment collection
    # charges from unrelated violation fines that may share the same GL account.
    COLLECTIONS_AMOUNTS = {40.0, 250.0}

    # ── Collections notice history ────────────────────────────────
    # Pass 1: charges with a readable stage memo (most reliable — works
    #   regardless of which GL account the charge landed on).
    # Pass 2: charges on a known collections GL account whose amount
    #   matches $40 or $250 — these are likely old assessment collection
    #   charges with blank/generic memos. Violation fines ($25, $75, $100
    #   etc.) are intentionally excluded here so they don't pollute the
    #   assessment collections history or trigger false manual-review flags.
    charge_ids_seen = set()
    all_coll = []
    for c in charges:
        memo = c.get("Memo", "") or ""
        if normalize_memo(memo) != "unknown":
            all_coll.append(c)
            charge_ids_seen.add(id(c))
    for c in charges:
        if id(c) not in charge_ids_seen:
            amount = abs(c.get("TotalAmount", 0) or 0)
            in_coll_gl = any(l.get("GLAccountId") in COLLECTIONS_GL_IDS
                             for l in c.get("Lines", []))
            if in_coll_gl and amount in COLLECTIONS_AMOUNTS:
                all_coll.append(c)
    all_coll = sorted(all_coll, key=lambda x: x["Date"])

    # ── Build notice history ──────────────────────────────────────
    notice_history    = []
    last_notice_date  = None
    ambiguous_charges = []   # readable-amount charges with unreadable memos

    for c in all_coll:
        memo  = c.get("Memo", "") or ""
        stage = normalize_memo(memo)
        if stage != "unknown":
            notice_history.append(stage)
            last_notice_date = c["Date"]
        else:
            # Amount-matched but memo unreadable — record for potential flag
            ambiguous_charges.append(c)

    # Only flag for manual review if we have ZERO readable stage history
    # AND there are ambiguous charges we can't interpret.
    # If we have at least one readable stage, we can still proceed —
    # the ambiguous charges are noise (old imports, etc.).
    has_old_charge_memos = (len(notice_history) == 0 and len(ambiguous_charges) > 0)

    # ── Payment reset detection ───────────────────────────────────────
    # If the account has prior notice history but the current consecutive
    # streak is short (≤ 2 months), check whether a significant payment
    # was made after the last notice. If so, the prior debt was paid off
    # and any new delinquency is a fresh start — wipe notice_history so
    # the 60-day gate and stage logic treat this as a new delinquency.
    #
    # This handles cases like:
    #   22398: had Advanced notices, paid $722 on 2/11, now late again
    #          for the first time in March → should start fresh at 60-day
    #   22546: had Advanced notices, paid $1,318 on 4/1 → balance $0,
    #          should be skipped entirely (caught by balance check above)
    if notice_history and last_notice_date:
        if paid_off_after_last_notice(acct_id, last_notice_date):
            log.info(f"    Account {acct_id}: payment detected after last notice "
                     f"({last_notice_date}) — resetting collections history")
            notice_history   = []
            last_notice_date = None

    # ── 60-day gate: don't start collections until 2nd consecutive late fee ─
    # Buildium posts the first $15 at 30 days late.
    # Collections don't begin until 60 days (2nd consecutive late fee).
    # Accounts already in the collections process (notice_history not empty)
    # are never gated — they stay in the process regardless.
    # Using consecutive_months (not lifetime total) so that accounts which
    # paid off and restarted are treated as new delinquencies, not continuations.
    dbg(f"notice_history={notice_history}  consecutive={consecutive_months}")
    if len(notice_history) == 0 and consecutive_months < 2:
        dbg(f"EXIT: 60-day gate — only {consecutive_months} consecutive month(s), no prior history")
        return None   # Only 30 days late (or restarted) — too early for collections

    # ── Guard: already sent a notice in last 25 days? ────────────
    if last_notice_date:
        last_dt = datetime.strptime(last_notice_date, "%Y-%m-%d").date()
        if (today - last_dt).days < 25:
            dbg(f"EXIT: recent notice guard — last notice {last_notice_date}")
            return None

    # Skip if no late fee AND no collections history (truly current)
    if not recent_late_fee and not all_coll:
        dbg("EXIT: no recent late fee and no collections history → current")
        return None

    if aged_assessment_total <= 0 and not recent_late_fee:
        dbg("EXIT: no aged assessment balance and no recent late fee → current")
        return None

    return {
        "acct_id":            acct_id,
        "notice_history":     notice_history,
        "last_notice_date":   last_notice_date,
        "months_delinquent":  max(consecutive_months, len(all_coll), 1),
        "aged_balance":       aged_assessment_total,
        "total_aged_balance": total_aged_balance,
        "has_old_charge_memos": has_old_charge_memos,
        "consecutive_months": consecutive_months,
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


def send_ez_mail(acct_id: int, template_name: str, all_addresses: bool) -> bool:
    """DEPRECATED — Buildium EZ Mail API does not exist (404). Kept as dead stub."""
    log.warning(f"    ⚠️  send_ez_mail() called but Buildium EZ Mail has no API endpoint. "
                f"Use send_lob_letter() instead.")
    return False


# ─────────────────────────────────────────────────────────────────
#  LOB.COM — PHYSICAL LETTER SENDING
#  Replaces Buildium EZ Mail (which has no API endpoint).
#  Sends USPS First Class or Certified letters programmatically.
#  Test API key: no mail actually sent, free, returns a preview PDF.
#  Live API key: real mail, ~$1/letter first class, ~$6 certified.
# ─────────────────────────────────────────────────────────────────

def get_owner_addresses(acct_id: int, all_addresses: bool) -> list[dict]:
    """
    Returns address dicts for owner(s) of an ownership account.
    Each dict: {name, first_name, last_name, line1, line2, city, state, zip, unit_address}

    unit_address is always the SBR property address — used in the letter body
    where the original Buildium template has [[Unit_Address_Line_1]].
    It is fetched from the unit record separately from the owner's mailing address.

    all_addresses=False → primary owner only (60/90/120-day notices)
    all_addresses=True  → all owners on the account (150-day+, certified stages)
    """
    # ── Step 1: always fetch the property/unit address (goes in letter body) ──
    unit_line1 = unit_city = unit_state = unit_zip = ""
    r_acct = requests.get(
        f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}",
        headers=buildium_headers(),
    )
    if r_acct.status_code == 200:
        unit_id = r_acct.json().get("UnitId")
        if unit_id:
            r_unit = requests.get(
                f"{BUILDIUM_BASE}/associations/units/{unit_id}",
                headers=buildium_headers(),
            )
            if r_unit.status_code == 200:
                ua         = r_unit.json().get("Address") or {}
                unit_line1 = (ua.get("AddressLine1") or "").strip()
                unit_city  = (ua.get("City")         or "").strip()
                unit_state = (ua.get("State")        or "").strip()
                unit_zip   = (ua.get("PostalCode")   or "").strip()
    unit_address = (f"{unit_line1}, {unit_city}, {unit_state} {unit_zip}".strip(", ")
                    if unit_line1 else "")

    # ── Step 2: get owner(s) for mailing / envelope address ────────
    r_owners = requests.get(
        f"{BUILDIUM_BASE}/associations/owners",
        headers=buildium_headers(),
        params={"ownershipaccountids": acct_id, "limit": 10},
    )
    owner_list = r_owners.json() if r_owners.status_code == 200 else []
    if not all_addresses:
        owner_list = owner_list[:1]

    addresses = []
    for owner in owner_list:
        first = (owner.get("FirstName") or "").strip()
        last  = (owner.get("LastName")  or "").strip()
        name  = f"{first} {last}".strip() or "Homeowner"
        addr  = (owner.get("MailingAddress") or
                 owner.get("PrimaryAddress") or
                 owner.get("Address") or {})
        line1 = (addr.get("AddressLine1") or "").strip()
        line2 = (addr.get("AddressLine2") or "").strip()
        city  = (addr.get("City")         or "").strip()
        state = (addr.get("State")        or "").strip()
        zip_  = (addr.get("PostalCode")   or "").strip()
        if line1 and city and state and zip_:
            addresses.append({
                "name":         name,
                "first_name":   first or "Homeowner",
                "last_name":    last,
                "line1":        line1,
                "line2":        line2,
                "city":         city,
                "state":        state,
                "zip":          zip_,
                "unit_address": unit_address,
            })

    if addresses:
        return addresses

    # ── Fallback: use the unit/property address as mailing address ─
    if unit_line1 and unit_city and unit_state and unit_zip:
        return [{
            "name":         "Homeowner",
            "first_name":   "Homeowner",
            "last_name":    "",
            "line1":        unit_line1,
            "line2":        "",
            "city":         unit_city,
            "state":        unit_state,
            "zip":          unit_zip,
            "unit_address": unit_address,
        }]
    return []


# ── Progress table helper ─────────────────────────────────────────
# Each letter (60-180 day) includes a "Where you are in the Collection
# Process" table with the current stage bolded. Pre-legal letters don't
# use this table — they have their own HOW TO RESOLVE section.

_EARLY_TABLE_ROWS = [
    ("30 Days",   "Late Notice",                            "$15"),
    ("60 Days",   "1st Enforcement Attempt",                "$55"),
    ("90 Days",   "2nd Enforcement Attempt",                "$55"),
    ("120 Days",  "3rd Enforcement Attempt",                "$55"),
    ("150 Days",  "Notice of Lien",                         "$55"),
    ("180+ Days", "Lien Recorded Plus 12% Interest Begins", "$295"),
]

# Maps template name → row index that shows "Present Stage" (0 = 30-day row)
_PRESENT_ROW = {
    "60-Day Late Notice":                               1,
    "90-Day Late Notice":                               2,
    "120-Day Late Notice":                              3,
    "150-Day Late & Notice of Intent to Record a Lien": 4,
    "180-Day Late & Lien Recorded Notice":              5,
}


def _progress_table(template_name: str) -> str:
    """Returns the correct HTML progress table for a given stage, or '' for pre-legal."""
    if template_name in _PRESENT_ROW:
        present   = _PRESENT_ROW[template_name]
        rows_html = ""
        for i, (day, action, fee) in enumerate(_EARLY_TABLE_ROWS):
            if i == 0 or i < present:
                status = "Completed"
                bold   = ""
            elif i == present:
                status = "<strong>Present Stage</strong>"
                bold   = " style='font-weight:bold'"
            else:
                status = ""
                bold   = ""
            rows_html += (f"<tr{bold}><td>{day}</td><td>{action}</td>"
                          f"<td>{fee}</td><td>{status}</td></tr>\n")
        return (
            "<h3 style='text-align:center;margin-top:20pt;margin-bottom:6pt;'>"
            "Where you are in the Collection Process</h3>"
            "<table border='1' cellpadding='5' cellspacing='0' "
            "style='border-collapse:collapse;width:100%;font-size:10pt;'>"
            "<tr style='background:#f0f0f0;'>"
            "<td><strong>Day Late</strong></td>"
            "<td><strong>Action Taken</strong></td>"
            "<td><strong>Fee Applied</strong></td>"
            "<td><strong>Your Status</strong></td></tr>"
            f"{rows_html}</table>"
        )

    if template_name == "Advanced Stage of Delinquency":
        return (
            "<h3 style='text-align:center;margin-top:20pt;margin-bottom:6pt;'>"
            "Where you are in the Collection Process</h3>"
            "<table border='1' cellpadding='5' cellspacing='0' "
            "style='border-collapse:collapse;width:100%;font-size:10pt;'>"
            "<tr style='background:#f0f0f0;'>"
            "<td><strong>Days Late</strong></td>"
            "<td><strong>Action Taken</strong></td>"
            "<td><strong>Fee Applied</strong></td>"
            "<td><strong>Your Status</strong></td></tr>"
            "<tr><td>180+ days delinquent</td>"
            "<td>Lien Recorded Plus 12% Interest accrues as permitted by law</td>"
            "<td>$295</td><td>Completed</td></tr>"
            "<tr style='font-weight:bold'><td>6 &#8211; 17 months delinquent</td>"
            "<td>Advanced Stage of Delinquency Notices</td>"
            "<td>$55 Per Month</td><td>Present Stage</td></tr>"
            "<tr><td>18 months delinquent</td>"
            "<td>Account referred to collection attorney proceedings, which may include "
            "foreclosure, subject to applicable law and attorney review.</td>"
            "<td>Estimated attorney expenses $2,000&#8211;$5,000+</td><td></td></tr>"
            "</table>"
        )

    return ""   # Pre-legal letters have their own resolution section, no table


# ── Letter body text (real content from Buildium templates) ──────────
# Placeholders filled by build_letter_html() via .format():
#   {first_name}      — owner first name          [[Homeowner_First_Name]]
#   {last_name}       — owner last name           [[Homeowner_Last_Name]]
#   {unit_address}    — SBR property address      [[Unit_Address_Line_1]]
#   {acct_id}         — Buildium account number   [[HOA_Owner_Account_Number]]
#   {balance_str}     — formatted balance, $0.00  [[Statement_Balance]]
#   {progress_table}  — auto-generated HTML table ('' for pre-legal letters)
LETTER_BODIES: dict[str, str] = {

    # ── Early-stage notices (60–180 day) ─────────────────────────────────
    "60-Day Late Notice": """
<p>Dear {first_name} {last_name},</p>

<p>We are reaching out regarding your HOA dues account at {unit_address}, which is now over 60 days
past due. We want to make sure you are aware of the status in order to avoid further fees. In accordance with
A.R.S. &sect;33-1807 and Section 14 of our Covenants, the Association is sending this formal dues enforcement
notice as required by law.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>To avoid additional fees, please submit your payment as soon as possible. You can review your account and
<strong>pay online at SignalButteRanch.com</strong> or by <strong>mailing a check with your account number
to &#8211;Signal Butte Ranch Community Association P.O. Box 98526 Phoenix, AZ 85038-0526</strong>.</p>

<p>If you have further questions, you may <strong>create a request at SignalButteRanch.com</strong>. Please
allow a few business days for a response.</p>

{progress_table}

<p>Please take this opportunity to bring your account current. If you've already made payment, disregard
this notice.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed HomeOwners Association<br>
480-648-4861</p>
""",

    "90-Day Late Notice": """
<p>Dear {first_name} {last_name},</p>

<p>We are reaching out regarding your HOA dues account at {unit_address}, which is now
<strong>over 90 days past due</strong>. We want to make sure you are aware of the status in order to avoid
further fees. In accordance with A.R.S. &sect;33-1807 and Section 14 of our Covenants, the Association is
sending this formal dues enforcement notice as required by law.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>To avoid additional fees, please submit your payment as soon as possible. You can review your account and
<strong>pay online at SignalButteRanch.com</strong> or by <strong>mailing a check with your account number
to &#8211;Signal Butte Ranch Community Association P.O. Box 98526 Phoenix, AZ 85038-0526</strong>.</p>

<p>If you have further questions, you may <strong>create a request at SignalButteRanch.com</strong>. Please
allow a few business days for a response.</p>

{progress_table}

<p>Please take this opportunity to bring your account current. If you've already made payment, disregard
this notice.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed HomeOwners Association<br>
480-648-4861</p>
""",

    "120-Day Late Notice": """
<p>Dear {first_name} {last_name},</p>

<p>We are reaching out regarding your HOA dues account at {unit_address}, which is now
<strong>over 120 days past due</strong>. We want to make sure you are aware of the status in order to avoid
further fees. In accordance with A.R.S. &sect;33-1807 and Section 14 of our Covenants, the Association is
sending this formal dues enforcement notice as required by law.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>To avoid additional fees, please submit your payment as soon as possible. You can review your account and
<strong>pay online at SignalButteRanch.com</strong> or by <strong>mailing a check with your account number
to &#8211;Signal Butte Ranch Community Association P.O. Box 98526 Phoenix, AZ 85038-0526</strong>.</p>

<p>If you have further questions or would like to <strong>arrange a board approved payment plan</strong>,
you may <strong>create a request at SignalButteRanch.com</strong>. Please allow a few business days for a
response.</p>

{progress_table}

<p>Please take this opportunity to bring your account current. If you've already made payment, disregard
this notice.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed HomeOwners Association<br>
480-648-4861</p>
""",

    "150-Day Late & Notice of Intent to Record a Lien": """
<h2 style="text-align:center; margin-bottom:0.2in;">Notice of Intent to Record a Lien</h2>

<p>Dear {first_name} {last_name},</p>

<p>Your HOA dues account at {unit_address} is now <strong>over 150 days past due</strong>. We want to make
sure you are aware of the status in order to <strong>avoid a lien placed on your property after 180 days
delinquent.</strong> In accordance with A.R.S. &sect;33-1807 and Section 14 of our Covenants, the Association
is sending this formal dues enforcement notice as required by law.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>To avoid additional fees, please submit your payment as soon as possible. You can review your account and
<strong>pay online at SignalButteRanch.com</strong> or by <strong>mailing a check with your account number
to &#8211;Signal Butte Ranch Community Association P.O. Box 98526 Phoenix, AZ 85038-0526</strong>.</p>

<p>If you have further questions or would like to <strong>arrange a board approved payment plan</strong>,
you may <strong>create a request at SignalButteRanch.com</strong>. Please allow a few business days for a
response.</p>

{progress_table}

<p>Please take this opportunity to bring your account current. If you've already made payment, disregard
this notice.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed HomeOwners Association<br>
480-648-4861</p>
""",

    "180-Day Late & Lien Recorded Notice": """
<p>Dear {first_name} {last_name},</p>

<p>We are reaching out regarding your HOA dues account at {unit_address}, which is now
<strong>over 180 days past due</strong>. We want to make sure you are aware of the status in order to avoid
further fees. In accordance with A.R.S. &sect;33-1807 and Section 14 of our Covenants, the Association is
sending this formal dues enforcement notice as required by law.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>To avoid additional fees, please submit your payment as soon as possible. You can review your account and
<strong>pay online at SignalButteRanch.com</strong> or by <strong>mailing a check with your account number
to &#8211;Signal Butte Ranch Community Association P.O. Box 98526 Phoenix, AZ 85038-0526</strong>.</p>

<p>If you have further questions or would like to <strong>arrange a board approved payment plan</strong>,
you may <strong>create a request at SignalButteRanch.com</strong>. Please allow a few business days for a
response.</p>

{progress_table}

<p>Please take this opportunity to bring your account current so that the recorded lien may be released.
If you've already made payment, disregard this notice.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed HomeOwners Association<br>
480-648-4861</p>
""",

    # ── Advanced stage (months 7–16) ──────────────────────────────────────
    "Advanced Stage of Delinquency": """
<p>Dear {first_name} {last_name},</p>

<p>Your HOA dues account for {unit_address} remains delinquent and is currently classified by the Association
as being in an <strong>ADVANCED STAGE OF DELINQUENCY</strong>. This notice is provided to ensure you are
aware of the account status and to provide an opportunity to resolve the balance before additional steps are
taken. This notice is sent pursuant to A.R.S. &sect;33-1807 and Section 14 of our Covenants, which govern the
collection of unpaid assessments.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>To avoid additional fees and the possible referral of your account for further collection action, please
submit your payment as soon as possible. Review your account payment history at
<strong>SignalButteRanch.com</strong>. Payments may be made online or by <strong>mailing a check with your
account number to &#8211;Signal Butte Ranch Community Association P.O. Box 98526 Phoenix,
AZ 85038-0526</strong>.</p>

<p>If you have further questions or wish to request a <strong>board approved payment plan</strong>, you may
<strong>create a request at SignalButteRanch.com</strong>. Please allow a few business days for a
response.</p>

{progress_table}

<p>Please take this opportunity to bring your account current. If you have already made payment, disregard
this notice.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed Home Owners Association<br>
480-648-4861</p>
""",

    # ── Pre-legal notices (months 17–18) — no progress table ─────────────
    "60 Days Prior to Legal Collections (2nd to Last Letter)": """
<p style="text-align:center; font-weight:bold; font-size:10pt;">NOTICE OF INTENT TO REFER ACCOUNT TO LEGAL
COLLECTIONS (60-DAY NOTICE) &#8211; OPPORTUNITY TO RESOLVE DELINQUENT ACCOUNT</p>

<p style="font-size:14pt; font-weight:bold; line-height:1.4; margin-bottom:0.2in;">Your account is
delinquent. Failure to bring your account current or enter into an approved payment arrangement within
60 days of this notice will result in referral for further collection proceedings, which could include
bringing a foreclosure action against your property.</p>

<p>Dear {first_name} {last_name},</p>

<p>The hoa assessment account at {unit_address} is more than seventeen (17) months delinquent. Despite prior
notices, the outstanding balance has not been resolved. Unless full payment is received or a payment plan is
approved within sixty (60) days from the date of this letter, the Association will proceed with further
collection actions as permitted under Arizona law and the Association&#8217;s governing documents.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>&#8209;Arizona law permits homeowners associations to pursue lien enforcement and foreclosure once an
account reaches 18 months delinquent <u>or</u> exceeds $10,000 in unpaid assessments, fees, and charges.<br>
<strong>&#8209;Referral to legal counsel may increase the total balance owed by approximately $2,000 to
$5,000 or more due to attorney fees, court costs, and additional enforcement expenses.</strong></p>

<p><strong>HOW TO RESOLVE THIS MATTER</strong><br>
<strong>1. Pay in full.</strong> Online at SignalButteRanch.com or by mail to: Signal Butte Ranch Community
Association P.O. Box 98526 Phoenix, AZ 85038-0526<br>
<strong>2. Or Submit a written request for a payment plan at SignalButteRanch.com.</strong></p>

<p>If you believe this notice is in error, submit a written request within 60-days to avoid further
escalation. Failure to act will be an additional expensive burden to you.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed HomeOwners Association<br>
480-648-4861</p>
""",

    "30 Days Prior to Legal Collections (Final Letter)": """
<p style="text-align:center; font-weight:bold; font-size:10pt;">FINAL 30-DAY NOTICE PRIOR TO LEGAL
COLLECTIONS &#8211; OPPORTUNITY TO RESOLVE DELINQUENT ACCOUNT</p>

<p style="font-size:14pt; font-weight:bold; line-height:1.4; margin-bottom:0.2in;">Your account is
delinquent. Failure to bring your account current or enter into an approved payment arrangement within
30 days of this notice will result in referral for further collection proceedings, which could include
bringing a foreclosure action against your property.</p>

<p>Dear {first_name} {last_name},</p>

<p>The hoa assessment account at {unit_address} is more than eighteen (18) months delinquent. Despite prior
notices, the outstanding balance has not been resolved. Unless full payment is received or a payment plan is
approved <strong>within thirty (30) days</strong> from the date of this letter, the Association will proceed
with further collection actions as permitted under Arizona law and the Association&#8217;s governing
documents.</p>

<p><strong>Account Number: {acct_id} &nbsp;&nbsp;&nbsp;&nbsp; Past Due Amount: {balance_str}</strong></p>

<p>&#8209;Arizona law permits homeowners associations to pursue lien enforcement and foreclosure once an
account reaches 18 months delinquent <u>or</u> exceeds $10,000 in unpaid assessments, fees, and charges.<br>
<strong>&#8209;Referral to legal counsel may increase the total balance owed by approximately $2,000 to
$5,000 or more due to attorney fees, court costs, and additional enforcement expenses.</strong></p>

<p><strong>HOW TO RESOLVE THIS MATTER</strong><br>
<strong>1. Pay in full.</strong> Online at SignalButteRanch.com or by mail to: Signal Butte Ranch Community
Association P.O. Box 98526 Phoenix, AZ 85038-0526<br>
<strong>2. Or Submit a written request for a payment plan at SignalButteRanch.com.</strong></p>

<p>If you believe this notice is in error, submit a written request within 30-days to avoid further
escalation. Failure to act will be an additional expensive burden to you.</p>

<p>Respectfully,<br>
Signal Butte Ranch Community Association<br>
Self-Managed HomeOwners Association<br>
480-648-4861</p>
""",
}

# ── Signal Butte Ranch logo (base64 PNG, extracted from Buildium templates) ──
_LOGO_B64 = "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"

def build_letter_html(template_name: str, addr: dict,
                      acct_id: int, balance: float) -> str:
    """Build a full HTML letter for the given stage. Lob renders this as a PDF.

    addr keys: name, first_name, last_name, line1, line2 (opt),
               city, state, zip, unit_address
    """
    today_str    = date.today().strftime("%B %d, %Y")
    first_name   = addr.get("first_name") or addr["name"].split()[0]
    last_name    = addr.get("last_name", "")
    unit_address = addr.get("unit_address", "")
    balance_str  = f"${balance:,.2f}"
    prog_table   = _progress_table(template_name)

    raw_body = LETTER_BODIES.get(
        template_name,
        "<p>[Letter body not configured for template: '{template_name}']</p>"
    )
    body = raw_body.format(
        first_name=first_name,
        last_name=last_name,
        unit_address=unit_address,
        acct_id=acct_id,
        balance_str=balance_str,
        progress_table=prog_table,
    )

    addr_lines = [addr["line1"], addr.get("line2", ""),
                  f"{addr['city']}, {addr['state']} {addr['zip']}"]
    addr_html  = "<br>".join(line for line in addr_lines if line)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11pt;
    line-height: 1.6;
    color: #000;
    width: 8.5in;
    padding: 0.75in;
  }}
  .letterhead  {{ display: table; width: 100%; margin-bottom: 0.35in; }}
  .from-block  {{ display: table-cell; vertical-align: top;
                  font-size: 10pt; color: #333; }}
  .logo-block  {{ display: table-cell; vertical-align: top;
                  text-align: right; width: 175px; padding-right: 0.25in; }}
  .logo-block img {{ width: 155px; }}
  .date-block  {{ margin-bottom: 0.3in; }}
  .to-block    {{ margin-bottom: 0.4in; }}
  .body        {{ margin-bottom: 0.3in; }}
  .body p      {{ margin-bottom: 0.15in; }}
  .body h2     {{ margin-bottom: 0.15in; }}
</style>
</head>
<body>

<div class="letterhead">
  <div class="from-block">
    <strong>{CONFIG['hoa_name']}</strong><br>
    {CONFIG['hoa_address_line1']}<br>
    {CONFIG['hoa_address_city']}, {CONFIG['hoa_address_state']} {CONFIG['hoa_address_zip']}
  </div>
  <div class="logo-block">
    <img src="data:image/png;base64,{_LOGO_B64}" alt="Signal Butte Ranch">
  </div>
</div>

<div class="date-block">{today_str}</div>

<div class="to-block">
  {addr["name"]}<br>
  {addr_html}
</div>

<div class="body">
{body}
</div>

</body>
</html>"""


def send_lob_letter(acct_id: int, template_name: str, all_addresses: bool,
                    balance: float = 0.0, certified: bool = False) -> bool:
    """
    Send a physical letter via Lob.com. Returns True if all sends succeeded.

    certified=True  → USPS Certified Mail (tracked, ~$6, signature optional)
    certified=False → USPS First Class (~$1)

    In dry_run mode: logs intent, no API call made.
    With test API key (test_...): Lob accepts the call and returns a PDF
      preview but does NOT actually mail anything — free and safe for testing.
    """
    lob_key = CONFIG.get("lob_api_key", "")
    if not lob_key:
        log.warning(f"    ⚠️  LOB_API_KEY not configured — letter skipped for {acct_id}")
        return False

    if CONFIG["dry_run"]:
        mail_type = "usps_certified" if certified else "usps_first_class"
        log.info(f"    [DRY RUN] Lob '{template_name}' ({mail_type}) → acct {acct_id}")
        return True

    addresses = get_owner_addresses(acct_id, all_addresses)
    if not addresses:
        log.warning(f"    ⚠️  No mailing address found for account {acct_id} — letter skipped")
        return False

    mail_type = "usps_certified" if certified else "usps_first_class"
    all_ok    = True

    for addr in addresses:
        html = build_letter_html(template_name, addr, acct_id, balance)

        data: dict = {
            "to[name]":              addr["name"],
            "to[address_line1]":     addr["line1"],
            "to[address_city]":      addr["city"],
            "to[address_state]":     addr["state"],
            "to[address_zip]":       addr["zip"],
            "to[address_country]":   "US",
            "from[name]":            CONFIG["hoa_name"],
            "from[address_line1]":   CONFIG["hoa_address_line1"],
            "from[address_city]":    CONFIG["hoa_address_city"],
            "from[address_state]":   CONFIG["hoa_address_state"],
            "from[address_zip]":     CONFIG["hoa_address_zip"],
            "from[address_country]": "US",
            "file":                  html,
            "color":                 "false",
            "double_sided":          "false",
            "mail_type":             mail_type,
            "use_type":              "operational",
        }
        if addr.get("line2"):
            data["to[address_line2]"] = addr["line2"]

        try:
            resp = requests.post(
                "https://api.lob.com/v1/letters",
                auth=(lob_key, ""),
                data=data,
                timeout=30,
            )
            resp.raise_for_status()
            result    = resp.json()
            letter_id = result.get("id", "unknown")
            url       = result.get("url", "")
            log.info(f"    ✅ Lob letter sent → {addr['name']} | {letter_id} | {mail_type}")
            if url:
                log.info(f"       Preview: {url}")
        except Exception as e:
            log.warning(f"    ⚠️  Lob FAILED for {acct_id} → {addr['name']}: {e}")
            try:
                log.warning(f"       Lob response: {e.response.text}")   # type: ignore
            except Exception:
                pass
            all_ok = False

    return all_ok


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
    certified_queue         = []
    board_alerts            = []
    mail_failures           = []  # charge posted but EZ Mail failed — Crystal must send manually
    payment_plan_candidates = []  # informal payments detected — Crystal should set PaymentPlan
    failing_plans           = []  # formally on PaymentPlan but missed this month's $120

    for i, acct in enumerate(owners):
        acct_id = acct.get("Id")

        # ── DelinquencyStatus skip ────────────────────────────────
        skip_reason = should_skip(acct)
        if skip_reason:
            # Even though we skip PaymentPlan accounts for collections, we still
            # need to check whether they're keeping up with their plan. If no
            # qualifying payment arrived this cycle, flag Crystal to remove the
            # PaymentPlan status so collections resume next month.
            status = (acct.get("DelinquencyStatus") or "").lower()
            if "payment" in status and "plan" in status:
                failing = check_payment_plan_failing(acct_id)
                if failing:
                    failing_plans.append(failing)
                    log.info(f"    ⚠️  Payment plan FAILING: {acct_id} — "
                             f"no ${failing['min_required']:.0f}+ payment in last 35 days "
                             f"(last seen: {failing['last_payment_date']})")
            skipped.append({"id": acct_id, "reason": skip_reason})
            continue

        # ── Analyze charge history ────────────────────────────────
        analysis = analyze_account(acct_id)
        if analysis is None:
            continue   # current or prepaid

        # ── Accounts with ONLY ambiguous charge history need manual review ──
        # (has $40/$250 charges in collections GL but zero readable stage memos —
        # cannot determine current stage; violation fines are excluded from this check)
        if analysis["has_old_charge_memos"]:
            flagged.append({
                "id":      acct_id,
                "reason":  "Only legacy 'Charge' memos found — cannot determine collections stage. Review in Buildium and post the correct next stage notice manually.",
                "balance": analysis["aged_balance"],
            })
            continue

        # ── Carry-over / high-balance check ──────────────────────────
        # If an account has NO prior collections history but is already
        # significantly delinquent (high balance OR many months late),
        # it was likely never processed by prior management and needs
        # Crystal to manually determine the correct starting stage before
        # the automation touches it. Sending a 60-Day notice to someone
        # 7 months late would be wrong.
        if len(analysis["notice_history"]) == 0:
            over_balance = analysis["total_aged_balance"] >= CONFIG["carryover_balance_threshold"]
            over_months  = analysis["months_delinquent"] >= CONFIG["carryover_months_threshold"]
            if over_balance or over_months:
                reason_parts = []
                if over_balance:
                    reason_parts.append(f"balance ${analysis['total_aged_balance']:.0f} exceeds ${CONFIG['carryover_balance_threshold']:.0f} threshold")
                if over_months:
                    reason_parts.append(f"{analysis['months_delinquent']} months late exceeds {CONFIG['carryover_months_threshold']}-month threshold")
                flagged.append({
                    "id":      acct_id,
                    "reason":  f"No collections history but {' and '.join(reason_parts)}. Likely carry-over from prior management — determine correct stage in Buildium and post it manually.",
                    "balance": analysis["aged_balance"],
                })
                continue

        # ── Determine next action ─────────────────────────────────
        action = determine_next_action(
            analysis["notice_history"],
            analysis["total_aged_balance"],
            months_delinquent_total=analysis["months_delinquent"],
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
                action["memo"],
                action["gl_account"],
            )

        # ── Physical letter via Lob.com ───────────────────────────
        if action["letter_template"]:
            mail_ok = send_lob_letter(
                acct_id,
                action["letter_template"],
                action["all_addresses"],
                balance=analysis["total_aged_balance"],
                certified=action["certified_mail"],
            )
            if not mail_ok:
                mail_failures.append({"id": acct_id, "stage": action["stage_name"]})

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

        # ── Check for informal payment plan ───────────────────────
        # Only flag if their balance is still meaningful — don't bother if
        # they're nearly paid off and will clear naturally next month.
        pp = check_payment_plan_candidate(acct_id, analysis["total_aged_balance"])
        if pp:
            payment_plan_candidates.append(pp)
            log.info(f"    💳 Payment plan candidate: {acct_id} — "
                     f"{pp['payment_count']} payment(s) / ${pp['total_paid']:.0f} in last 35 days")

        if (i + 1) % 50 == 0:
            log.info(f"  ...{i+1}/{len(owners)} scanned")

    log.info(f"\n  Done. Processed {len(results)} | "
             f"Skipped {len(skipped)} | "
             f"Flagged {len(flagged)} | "
             f"Board alerts {len(board_alerts)}")

    # ── Print flagged and board alert details so we can review them ──
    if flagged:
        log.info("\n  MANUAL REVIEW ACCOUNTS:")
        for f in flagged:
            log.info(f"    Account {f['id']} | Balance ${f['balance']:.0f} | {f['reason']}")
    if board_alerts:
        log.info("\n  BOARD ALERT ACCOUNTS:")
        for a in board_alerts:
            log.info(f"    Account {a['id']} | {a['months']} months | Balance ${a['balance']:.0f} | {a['alert']}")

    send_summary_email(results, skipped, flagged, certified_queue, board_alerts,
                       mail_failures, payment_plan_candidates, failing_plans)
    return results


# ─────────────────────────────────────────────────────────────────
#  SUMMARY EMAIL
# ─────────────────────────────────────────────────────────────────
def send_summary_email(processed, skipped, flagged, certified_queue, board_alerts,
                       mail_failures=None, payment_plan_candidates=None, failing_plans=None):
    today   = date.today().strftime("%B %d, %Y")
    subject = f"SBR Collections Run — {today}"
    dry     = CONFIG["dry_run"]
    mode    = "⚠️ DRY RUN — nothing posted" if dry else "✅ LIVE RUN"

    lines = [
        f"<h2>Signal Butte Ranch — Monthly Collections</h2>",
        f"<p><b>{today}</b> &nbsp;|&nbsp; <b>{mode}</b></p><hr>",
    ]

    # Mail failures — charge posted but letter not sent — urgent
    if mail_failures:
        lines.append(f"<h3 style='color:red'>⚠️ MAIL FAILURES — MANUAL SEND REQUIRED ({len(mail_failures)})</h3>")
        lines.append("<p><b>The charge was posted to Buildium but the EZ Mail letter failed to send.<br>"
                     "Crystal must send these letters manually in Buildium or Page Per Page:</b></p><ul>")
        for m in mail_failures:
            lines.append(f"<li>Account {m['id']} — {m['stage']}</li>")
        lines.append("</ul>")

    # Failing payment plans — must remove status so collections resume next month
    if failing_plans:
        lines.append(f"<h3 style='color:darkorange'>⚠️ Payment Plans Failing — Remove Status in Buildium ({len(failing_plans)})</h3>")
        lines.append(
            "<p>These accounts are marked <b>Payment Plan</b> in Buildium but have "
            f"<b>not made a ${CONFIG['payment_plan_min_payment']:.0f}+ payment in the last 35 days.</b><br>"
            "Open each account in Buildium, remove the Payment Plan status, and the script "
            "will automatically resume collections notices next month.</p><ul>"
        )
        for fp in failing_plans:
            lines.append(
                f"<li>Account {fp['acct_id']} — "
                f"last payment seen: {fp['last_payment_date']}</li>"
            )
        lines.append("</ul>")

    # Payment plan candidates — one-click action in Buildium
    if payment_plan_candidates:
        lines.append(f"<h3 style='color:steelblue'>💳 Possible Payment Plans — Set in Buildium ({len(payment_plan_candidates)})</h3>")
        lines.append(
            f"<p>These accounts are actively delinquent and have made at least one "
            f"${CONFIG['payment_plan_min_payment']:.0f}+ payment this cycle — suggesting an "
            f"informal arrangement may be in place.<br>"
            f"If you've agreed to a payment plan, open each account in Buildium and set "
            f"<b>Delinquency Status → Payment Plan</b>. The script will skip them each month "
            f"and alert you here if they miss a payment.</p><ul>"
        )
        for pp in payment_plan_candidates:
            lines.append(
                f"<li>Account {pp['acct_id']} — "
                f"${pp['aged_balance']:.0f} balance — "
                f"{pp['payment_count']} payment(s) / ${pp['total_paid']:.0f} paid "
                f"(last: {pp['last_payment_date']})</li>"
            )
        lines.append("</ul>")

    # Board alerts first — highest priority
    if board_alerts:
        lines.append(f"<h3 style='color:red'>🚨 Board Action Required ({len(board_alerts)})</h3><ul>")
        for a in board_alerts:
            lines.append(f"<li>Account {a['id']} — {a['months']} months, "
                         f"${a['balance']:.0f} balance<br><i>{a['alert']}</i></li>")
        lines.append("</ul>")

    # Certified mail log — sent automatically via Lob, no approval needed
    if certified_queue:
        lines.append(f"<h3 style='color:darkorange'>📬 Certified Mail Sent via Lob ({len(certified_queue)})</h3>")
        lines.append("<p>The following certified letters were sent automatically via Lob.com. "
                     "USPS tracking numbers will appear in your "
                     "<a href='https://dashboard.lob.com'>Lob dashboard</a> "
                     "within 1 business day:</p><ul>")
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


# ─────────────────────────────────────────────────────────────────
#  AUDIT MODE  (python sbr_collections_automation.py --audit)
#  Scans every account with a balance > $77 (one assessment + one late fee)
#  and shows exactly why each was processed, skipped, gated, reset, or
#  flagged. Use this before go-live to spot any accounts falling
#  through the cracks.
#
#  Filter: total aged charges > monthly_assessment + $15 late fee ($77 for SBR).
#  This catches anyone with meaningful unpaid charges regardless of late-fee
#  pattern — more reliable than filtering on late-fee count alone.
# ─────────────────────────────────────────────────────────────────
def run_audit():
    today         = date.today()
    cutoff_30     = today - timedelta(days=30)
    cutoff_45     = today - timedelta(days=45)
    COLLECTIONS_AMOUNTS  = {40.0, 250.0}
    AUDIT_BALANCE_FLOOR  = CONFIG["monthly_assessment"] + 15   # $77 for SBR

    log.info("=" * 70)
    log.info(f"SBR Collections AUDIT — {today.strftime('%B %d, %Y')}")
    log.info(f"Showing all accounts with a late fee or collections charge (balance > ${AUDIT_BALANCE_FLOOR:.0f})")
    log.info("=" * 70)

    owners = get_active_owners()
    rows   = []

    for i, acct in enumerate(owners):
        acct_id = acct.get("Id")

        # ── DelinquencyStatus check ───────────────────────────────
        skip_reason = should_skip(acct)
        if skip_reason:
            rows.append({
                "id": acct_id, "cons": "-", "total_fees": "-",
                "history": "-", "balance": 0,
                "disposition": f"SKIPPED — {skip_reason}",
            })
            continue

        r = requests.get(
            f"{BUILDIUM_BASE}/associations/ownershipaccounts/{acct_id}/charges",
            headers=buildium_headers(),
            params={"limit": 200},
        )
        if r.status_code != 200:
            continue
        charges = r.json()

        # ── Delinquency filter ────────────────────────────────────────
        # The ONLY reliable signals of active delinquency are:
        #   1. A $15 late fee within the last 45 days — Buildium only
        #      auto-applies this when the account is past due right now.
        #   2. Existing collections history — account is already in the process.
        #
        # Gross charge balance is NOT used as a filter. The charges endpoint
        # has no payment data, so paid accounts appear to carry large balances.
        # If Buildium stopped charging late fees, it means the account paid
        # (or was placed in an arrangement). Trust Buildium's fee signal.

        cutoff_45_audit = today - timedelta(days=45)
        has_recent_late_fee = any(
            c.get("TotalAmount") == 15.0
            and any(l.get("GLAccountId") == CONFIG["gl_late_fee"]
                    for l in c.get("Lines", []))
            and datetime.strptime(c["Date"], "%Y-%m-%d").date() >= cutoff_45_audit
            for c in charges
        )
        has_any_collections_charge = any(
            any(l.get("GLAccountId") in COLLECTIONS_GL_IDS
                for l in c.get("Lines", []))
            for c in charges
        )

        if not has_recent_late_fee and not has_any_collections_charge:
            continue   # Not actively delinquent — skip

        total_aged_balance_check = sum(
            c.get("TotalAmount", 0)
            for c in charges
            if datetime.strptime(c["Date"], "%Y-%m-%d").date() <= cutoff_30
        )

        all_late_fees = [
            c for c in charges
            if c.get("TotalAmount") == 15.0
            and any(l.get("GLAccountId") == CONFIG["gl_late_fee"]
                    for l in c.get("Lines", []))
        ]

        total_late_fees    = len(all_late_fees)
        consecutive_months = count_consecutive_late_fees(all_late_fees)
        recent_late_fee    = any(
            datetime.strptime(c["Date"], "%Y-%m-%d").date() >= cutoff_45
            for c in all_late_fees
        )
        aged_assessment_total = sum(
            c.get("TotalAmount", 0)
            for c in charges
            if datetime.strptime(c["Date"], "%Y-%m-%d").date() <= cutoff_30
            and any(l.get("GLAccountId") == CONFIG["gl_assessment"]
                    for l in c.get("Lines", []))
        )
        total_aged_balance = total_aged_balance_check   # already computed above

        # ── Build collections history (same logic as main script) ─
        charge_ids_seen = set()
        all_coll = []
        for c in charges:
            memo = c.get("Memo", "") or ""
            if normalize_memo(memo) != "unknown":
                all_coll.append(c)
                charge_ids_seen.add(id(c))
        for c in charges:
            if id(c) not in charge_ids_seen:
                amount     = abs(c.get("TotalAmount", 0) or 0)
                in_coll_gl = any(l.get("GLAccountId") in COLLECTIONS_GL_IDS
                                 for l in c.get("Lines", []))
                if in_coll_gl and amount in COLLECTIONS_AMOUNTS:
                    all_coll.append(c)
        all_coll = sorted(all_coll, key=lambda x: x["Date"])

        notice_history    = []
        last_notice_date  = None
        ambiguous_charges = []
        for c in all_coll:
            memo  = c.get("Memo", "") or ""
            stage = normalize_memo(memo)
            if stage != "unknown":
                notice_history.append(stage)
                last_notice_date = c["Date"]
            else:
                ambiguous_charges.append(c)

        has_old_charge_memos = (len(notice_history) == 0 and len(ambiguous_charges) > 0)

        # History summary for display (show last known stage)
        history_display = "none"
        if notice_history:
            history_display = notice_history[-1]
        elif ambiguous_charges:
            history_display = f"ambiguous({len(ambiguous_charges)})"

        # ── Payment reset (mirrors main script exactly) ───────────
        payment_reset = False
        if notice_history and last_notice_date:
            if paid_off_after_last_notice(acct_id, last_notice_date):
                payment_reset    = True
                notice_history   = []
                last_notice_date = None

        months_delinquent = max(consecutive_months, len(all_coll), 1)

        # ── Determine disposition ─────────────────────────────────
        # KEY RULE: recent_late_fee is the ONLY reliable delinquency signal.
        # aged_assessment_total reflects gross charges with no payments subtracted,
        # so it is always positive for any account with charge history — it cannot
        # distinguish a paid-off or prepaid account from an unpaid one.
        # If Buildium is not issuing late fees, the account is not delinquent.
        if not recent_late_fee:
            disp = "CURRENT — no active late fee (paid up or prepaid)"

        elif has_old_charge_memos:
            disp = "MANUAL REVIEW — ambiguous legacy charges, stage unknown"

        elif payment_reset:
            if consecutive_months < 2:
                disp = (f"RESET → GATE NOT MET "
                        f"({consecutive_months} mo since reset, need 2+)")
            else:
                action = determine_next_action([], total_aged_balance,
                                               months_delinquent_total=months_delinquent)
                disp = f"RESET → WOULD PROCESS → {action['stage_name']}"

        elif len(notice_history) == 0 and consecutive_months < 2:
            disp = (f"GATE NOT MET — {consecutive_months} consecutive mo "
                    f"(need 2+), no prior history")

        elif len(notice_history) == 0:
            over_balance = total_aged_balance >= CONFIG["carryover_balance_threshold"]
            over_months  = months_delinquent   >= CONFIG["carryover_months_threshold"]
            if over_balance or over_months:
                why = []
                if over_balance: why.append(f"${total_aged_balance:.0f} balance")
                if over_months:  why.append(f"{months_delinquent} months")
                disp = f"CARRYOVER FLAG — {' + '.join(why)}, no history"
            else:
                action = determine_next_action([], total_aged_balance,
                                               months_delinquent_total=months_delinquent)
                disp = f"WOULD PROCESS → {action['stage_name']}"

        elif last_notice_date and (today - datetime.strptime(last_notice_date, "%Y-%m-%d").date()).days < 25:
            disp = f"RECENT NOTICE — sent {last_notice_date}, within 25-day guard"

        elif not recent_late_fee and aged_assessment_total <= 0:
            disp = "CURRENT — balance cleared after collections"

        else:
            action = determine_next_action(notice_history, total_aged_balance,
                                           months_delinquent_total=months_delinquent)
            if action["auto_handle"]:
                disp = f"WOULD PROCESS → {action['stage_name']}"
            else:
                disp = f"BOARD ALERT → {action['stage_name']}"

        rows.append({
            "id":          acct_id,
            "cons":        consecutive_months,
            "total_fees":  total_late_fees,
            "history":     history_display,
            "balance":     total_aged_balance,
            "disposition": disp,
        })

        if (i + 1) % 50 == 0:
            log.info(f"  ...{i+1}/{len(owners)} scanned")

    # ── Sort by priority (most actionable first) ──────────────────
    def _sort_key(r):
        d = r["disposition"]
        if "BOARD ALERT"       in d: return 0
        if "WOULD PROCESS"     in d: return 1
        if "CARRYOVER"         in d: return 2
        if "MANUAL REVIEW"     in d: return 3
        if "RESET → WOULD"     in d: return 4
        if "RESET → GATE"      in d: return 5
        if "RECENT NOTICE"     in d: return 6
        if "GATE NOT MET"      in d: return 7
        if "CURRENT"           in d: return 8
        if "SKIPPED"           in d: return 9
        return 10

    rows.sort(key=_sort_key)

    # ── Only surface accounts that need a human decision ─────────
    # Everything the script handles automatically is excluded.
    # The goal: Crystal reads this, does the listed actions, done.

    board_alert  = [r for r in rows if "BOARD ALERT"        in r["disposition"]]
    carryover    = [r for r in rows if "CARRYOVER"          in r["disposition"]]
    manual       = [r for r in rows if "MANUAL REVIEW"      in r["disposition"]]
    fee_stopped  = [r for r in rows if "LATE FEE STOPPED"   in r["disposition"]]
    reset_watch  = [r for r in rows if "RESET → GATE"       in r["disposition"]]

    # Counts for the auto-handled buckets (no detail needed)
    auto_process = sum(1 for r in rows if "WOULD PROCESS" in r["disposition"])
    recent_guard = sum(1 for r in rows if "RECENT NOTICE" in r["disposition"])
    gate_wait    = sum(1 for r in rows if "GATE NOT MET"  in r["disposition"])

    log.info(f"\n{'='*60}")
    log.info("ACTIONS REQUIRED BEFORE NEXT RUN")
    log.info(f"{'='*60}")

    if board_alert:
        log.info(f"\n🚨 BOARD DECISION NEEDED ({len(board_alert)} accounts)")
        log.info("   These accounts are past the attorney threshold.")
        log.info("   Board must decide: refer to attorney or hold.")
        for r in board_alert:
            log.info(f"   • Account {r['id']} — last stage: {r['history']} — {r['disposition']}")

    if carryover:
        log.info(f"\n📋 SET STAGE IN BUILDIUM ({len(carryover)} accounts)")
        log.info("   These accounts have no collections history but are")
        log.info("   significantly delinquent. Open each in Buildium,")
        log.info("   determine correct stage, and post the notice manually.")
        log.info("   Script will pick them up automatically next month.")
        for r in carryover:
            log.info(f"   • Account {r['id']} — {r['disposition'].replace('CARRYOVER FLAG — ', '')}")

    if manual:
        log.info(f"\n🔍 AMBIGUOUS HISTORY — NEEDS REVIEW ({len(manual)} accounts)")
        log.info("   Old charges exist but stage cannot be determined from memo.")
        log.info("   Open in Buildium, confirm current stage, post correct notice.")
        for r in manual:
            log.info(f"   • Account {r['id']}")

    if reset_watch:
        log.info(f"\n👀 WATCH LIST — PAID UP, ONE FRESH LATE FEE ({len(reset_watch)} accounts)")
        log.info("   These made a significant payment after their last notice.")
        log.info("   History was reset. If they stay late next month the script")
        log.info("   will automatically send a new 60-Day notice. No action needed")
        log.info("   unless you believe the payment was only partial.")
        for r in reset_watch:
            log.info(f"   • Account {r['id']} — was at: {r['history']}")

    if fee_stopped:
        log.info(f"\n⚠️  LATE FEE STOPPED — CHECK IN BUILDIUM ({len(fee_stopped)} accounts)")
        log.info("   Buildium stopped applying the $15 late fee to these accounts.")
        log.info("   They may have been accidentally marked exempt, or paid an")
        log.info("   arrangement that wasn't formally entered. Check each in Buildium.")
        log.info("   If still delinquent, re-enable late fees so the script can track them.")
        for r in fee_stopped:
            disp_clean = r["disposition"].replace("⚠️  LATE FEE STOPPED — ", "")
            log.info(f"   • Account {r['id']} — {disp_clean}")

    if not any([board_alert, carryover, manual, fee_stopped, reset_watch]):
        log.info("\n   ✅ No manual actions required.")

    log.info(f"\n{'─'*60}")
    log.info("SCRIPT WILL HANDLE AUTOMATICALLY")
    log.info(f"  Ready to process next run:    {auto_process} accounts")
    log.info(f"  Protected (recent notice):    {recent_guard} accounts (notice < 25 days ago)")
    log.info(f"  Waiting one more month:       {gate_wait} accounts (only 1 consecutive late fee)")


if __name__ == "__main__":
    if "--audit" in sys.argv:
        run_audit()
    elif "--debug" in sys.argv:
        # Usage: python sbr_collections_automation.py --debug 22426 22443 22506
        ids = [int(a) for a in sys.argv if a.isdigit()]
        if not ids:
            print("Usage: python sbr_collections_automation.py --debug 22426 22443 ...")
        else:
            for acct_id in ids:
                log.info(f"\n── Debugging account {acct_id} ──")
                result = analyze_account(acct_id, debug=True)
                log.info(f"  analyze_account returned: {'dict (would process)' if result else 'None (skipped)'}")
                if result:
                    log.info(f"  notice_history: {result['notice_history']}")
                    log.info(f"  consecutive_months: {result['consecutive_months']}")
    else:
        run_monthly_collections()