"""
Calculates the real current balance for test accounts using the
transactions endpoint (which includes both charges AND payments).
Also tries any Buildium balance-specific endpoints that may exist.
"""
import requests

CLIENT_ID     = "d33da506-8f83-4e5f-a808-04c3cb0842a6"
CLIENT_SECRET = "jhDyXiXoG1NAWVfxiy1pklL2nobQkleTbexdjAwbt78="
HEADERS = {
    "x-buildium-client-id":     CLIENT_ID,
    "x-buildium-client-secret": CLIENT_SECRET,
    "Content-Type":             "application/json",
}
BASE = "https://api.buildium.com/v1"

# Accounts we care about
TEST_ACCOUNTS = {
    22398: "paid off 2/11, 1 new late fee 3/30",
    22546: "paid off 4/1",
    22471: "paid down to ~$12",
    22652: "known delinquent — 90-day stage",   # control: should show a balance
}

# ── 1. Try any balance-specific endpoints Buildium may offer ──────
print("\n── Trying balance-specific endpoints ──────────────────────")
for acct_id in TEST_ACCOUNTS:
    for path in (
        f"/associations/ownershipaccounts/{acct_id}/balance",
        f"/associations/ownershipaccounts/{acct_id}/ledger",
        f"/associations/ownershipaccounts/{acct_id}/accountsummary",
    ):
        r = requests.get(f"{BASE}{path}", headers=HEADERS)
        if r.status_code == 200:
            print(f"  ✅ Account {acct_id} | {path}  →  {r.text[:200]}")
        else:
            print(f"  ❌ Account {acct_id} | {path}  →  HTTP {r.status_code}")

# ── 2. Calculate balance from transactions ────────────────────────
print("\n── Calculated balance from transactions ────────────────────")
for acct_id, note in TEST_ACCOUNTS.items():
    r = requests.get(
        f"{BASE}/associations/ownershipaccounts/{acct_id}/transactions",
        headers=HEADERS,
        params={"limit": 300},
    )
    if r.status_code != 200:
        print(f"  Account {acct_id} | HTTP {r.status_code}")
        continue

    txns = sorted(r.json(), key=lambda x: x.get("Date", ""))
    balance = 0.0
    last_payment_date = None
    last_payment_amount = None

    for t in txns:
        ttype  = (t.get("TransactionTypeEnum") or "").lower()
        amount = t.get("TotalAmount", 0) or 0
        if "payment" in ttype or "credit" in ttype:
            balance -= amount
            last_payment_date   = t.get("Date")
            last_payment_amount = amount
        else:
            balance += amount

    print(f"\n  Account {acct_id} ({note})")
    print(f"    Calculated balance : ${balance:.2f}")
    print(f"    Last payment       : ${last_payment_amount:.2f} on {last_payment_date}"
          if last_payment_date else "    Last payment       : none found")
    print(f"    Total transactions : {len(txns)}")

    # Also show the raw TransactionTypeEnum values seen (so we know what strings to match)
    types_seen = sorted(set(t.get("TransactionTypeEnum","") for t in txns))
    print(f"    Transaction types  : {types_seen}")
