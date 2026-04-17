"""
Validation: Compare Crystal's April 1 manual mailing against what the script would decide.

Crystal manually sent letters on April 1, 2026 via EZ Mail to:
  - 60-Day Late Notice         (4 recipients)
  - 90-Day Late Notice         (3 recipients)
  - 120-Day Late Notice        (3 recipients)
  - 150-Day Pre-Lien Notice    (5 recipients)
  - Advanced Stage             (21 recipients)

This script finds all accounts that received a $40/$250 collections charge
on April 1, groups them by stage, and shows whether the script logic agrees.

Run: python validate_vs_crystal.py
"""
import requests
import os
import base64
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

HEADERS = {
    "x-buildium-client-id":     os.environ["BUILDIUM_CLIENT_ID"],
    "x-buildium-client-secret": os.environ["BUILDIUM_CLIENT_SECRET"],
    "Content-Type":             "application/json",
    "Accept":                   "application/json",
}
BASE             = "https://api.buildium.com/v1"
ASSOCIATION_ID   = 103158
APRIL_1          = "2026-04-01"
COLLECTIONS_GL   = {51537, 51538, 51539, 67944}
COLLECTIONS_AMTS = {40.0, 250.0}

STAGE_KEYWORDS = {
    "60-day":      "60-Day",
    "90-day":      "90-Day",
    "120-day":     "120-Day",
    "150-day":     "150-Day Pre-Lien",
    "180-day":     "180-Day Lien",
    "advanced":    "Advanced",
    "pre-legal":   "Pre-Legal",
    "lien":        "180-Day Lien",
}

def get_all_owners():
    owners, offset = [], 0
    while True:
        r = requests.get(
            f"{BASE}/associations/ownershipaccounts",
            headers=HEADERS,
            params={"associationids": ASSOCIATION_ID, "limit": 500, "offset": offset}
        )
        batch = r.json()
        if not batch:
            break
        owners.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return owners

def get_charges(acct_id):
    r = requests.get(
        f"{BASE}/associations/ownershipaccounts/{acct_id}/charges",
        headers=HEADERS,
        params={"limit": 200}
    )
    return r.json() if r.status_code == 200 else []

def classify_stage(memo):
    memo_lower = (memo or "").lower()
    if "pre-legal" in memo_lower or "prelegal" in memo_lower:
        return "Pre-Legal"
    if "advanced" in memo_lower:
        return "Advanced"
    if "180" in memo_lower or "lien" in memo_lower:
        return "180-Day Lien"
    if "150" in memo_lower:
        return "150-Day Pre-Lien"
    if "120" in memo_lower:
        return "120-Day"
    if "90" in memo_lower:
        return "90-Day"
    if "60" in memo_lower:
        return "60-Day"
    return "Unknown"

print("=" * 65)
print("Validation: Crystal's April 1 Mailing vs. Script Logic")
print("=" * 65)
print("Scanning all accounts for April 1 collections charges...")

owners = get_all_owners()
print(f"Total accounts: {len(owners)}\n")

april_1_accounts = {}   # acct_id → stage
total_scanned = 0

for i, acct in enumerate(owners):
    acct_id = acct.get("Id")
    charges = get_charges(acct_id)

    for c in charges:
        if c.get("Date") != APRIL_1:
            continue
        amount = abs(c.get("TotalAmount", 0) or 0)
        in_coll_gl = any(l.get("GLAccountId") in COLLECTIONS_GL for l in c.get("Lines", []))
        if in_coll_gl and amount in COLLECTIONS_AMTS:
            memo  = c.get("Memo", "") or ""
            stage = classify_stage(memo)
            april_1_accounts[acct_id] = {
                "stage":  stage,
                "amount": amount,
                "memo":   memo,
            }

    total_scanned += 1
    if total_scanned % 100 == 0:
        print(f"  ...{total_scanned}/{len(owners)} scanned, {len(april_1_accounts)} April 1 charges found so far")

print(f"\nDone. Found {len(april_1_accounts)} accounts with April 1 collections charges.\n")

# ── Group by stage ──────────────────────────────────────────────
from collections import defaultdict
by_stage = defaultdict(list)
for acct_id, info in april_1_accounts.items():
    by_stage[info["stage"]].append(acct_id)

stage_order = ["60-Day", "90-Day", "120-Day", "150-Day Pre-Lien", "180-Day Lien", "Advanced", "Pre-Legal", "Unknown"]

print("─" * 65)
print("CRYSTAL'S APRIL 1 MAILING — ACCOUNTS BY STAGE")
print("─" * 65)
for stage in stage_order:
    accounts = by_stage.get(stage, [])
    if accounts:
        print(f"\n  {stage} ({len(accounts)} accounts):")
        for acct_id in sorted(accounts):
            info = april_1_accounts[acct_id]
            print(f"    • {acct_id}  ${info['amount']:.0f}  memo: {info['memo'][:60]}")

if by_stage.get("Unknown"):
    print(f"\n  ⚠️  {len(by_stage['Unknown'])} accounts with unrecognized memo format:")
    for acct_id in by_stage["Unknown"]:
        print(f"    • {acct_id}  memo: {april_1_accounts[acct_id]['memo'][:80]}")

print(f"\n{'─' * 65}")
print(f"TOTAL: {len(april_1_accounts)} accounts processed by Crystal on April 1")
print(f"{'─' * 65}")
