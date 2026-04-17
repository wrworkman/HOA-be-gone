"""
Buildium API Lookup — SBR Setup Helper
=======================================
Run this once to:
  1. Confirm your API key works
  2. Get all GL Account IDs (needed for the collections script)
  3. Confirm your Association ID

HOW TO RUN:
  1. Fill in CLIENT_ID and CLIENT_SECRET below
  2. Open Terminal / Command Prompt
  3. Run:  python buildium_lookup.py
"""

import requests
import json

# ── Fill these in ──────────────────────────────────────────────
CLIENT_ID     = "d33da506-8f83-4e5f-a808-04c3cb0842a6"
CLIENT_SECRET = "jhDyXiXoG1NAWVfxiy1pklL2nobQkleTbexdjAwbt78="        # the secret you copied
ASSOCIATION_ID = "103158"
# ──────────────────────────────────────────────────────────────

BASE    = "https://api.buildium.com/v1"
HEADERS = {
    "x-buildium-client-id":     CLIENT_ID,
    "x-buildium-client-secret": CLIENT_SECRET,
    "Content-Type":             "application/json",
}

def check(label, resp):
    if resp.status_code == 200:
        print(f"\n✅  {label}")
    else:
        print(f"\n❌  {label} — HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


# ── 1. Confirm association exists ──────────────────────────────
print("\n" + "="*55)
print("  Buildium API Connection Test — Signal Butte Ranch")
print("="*55)

resp = requests.get(f"{BASE}/associations/{ASSOCIATION_ID}", headers=HEADERS)
data = check("Association lookup", resp)
if data:
    print(f"    Name : {data.get('Name')}")
    print(f"    ID   : {data.get('Id')}")


# ── 2. List all GL accounts ────────────────────────────────────
resp = requests.get(f"{BASE}/glaccounts", headers=HEADERS, params={"limit": 500})
data = check("GL Accounts", resp)
if data:
    print(f"\n  {'ID':<12}  {'Type':<12}  Name")
    print(f"  {'-'*12}  {'-'*12}  {'-'*35}")
    for acct in sorted(data, key=lambda x: (x.get('AccountType',''), x.get('Name',''))):
        print(f"  {str(acct.get('Id','')):<12}  "
              f"{acct.get('AccountType',''):<12}  "
              f"{acct.get('Name','')}")


# ── 3. List delinquent owners (preview) ───────────────────────
COLLECTIONS_GL_IDS = {51537, 51538, 51539, 67944}

# Stage progression map — what comes AFTER each memo
NEXT_STAGE = {
    "Collections Notice - 60 Day":                    ("90-Day Collection Notice",        40,  51537, False),
    "Collections Notice - 90 Day":                    ("120-Day Collection Notice",       40,  51537, False),
    "Collections Notice - 120 Day":                   ("150-Day Pre-Lien Notice",         40,  51538, True),
    "Collections Notice - Pre-Lien":                  ("180-Day Lien Filing",            250,  67944, True),
    "Collections Notice - Lien":                      ("Advanced Delinquency Notice",     40,  51537, False),
    "Collections Notice - Advanced Stage of Delinquency": ("Advanced Delinquency Notice",40,  51537, False),
    "Collections Notice - Pre-Legal 60 Day":          ("Pre-Legal Final Notice",          40,  51538, True),
    "Collections Notice - Pre-Legal Final":           ("BOARD ALERT — Ready for Attorney",0,  None,  True),
}

# ── Scan ALL active accounts ─────────────────────────────────────
print(f"\n  Scanning all active accounts for collections history...")
print(f"  This will take 1-2 minutes for 442 accounts...\n")

resp2 = requests.get(
    f"{BASE}/associations/ownershipaccounts",
    headers=HEADERS,
    params={"associationids": ASSOCIATION_ID, "limit": 500}
)
all_accounts = resp2.json() if resp2.status_code == 200 else []
active = [a for a in all_accounts if a.get("Status") == "Active"]

delinquent = []
for i, acct in enumerate(active):
    acct_id = acct.get("Id")
    r = requests.get(
        f"{BASE}/associations/ownershipaccounts/{acct_id}/charges",
        headers=HEADERS,
        params={"limit": 100}
    )
    if r.status_code != 200:
        continue
    charges = r.json()

    # Collections charges only
    coll = [c for c in charges
            if any(l.get("GLAccountId") in COLLECTIONS_GL_IDS
                   for l in c.get("Lines", []))]
    if not coll:
        continue

    # Most recent collections charge = current stage
    latest  = sorted(coll, key=lambda x: x["Date"], reverse=True)[0]
    memo    = latest.get("Memo", "")
    date    = latest.get("Date", "")
    total   = sum(c["TotalAmount"] for c in coll)

    # Determine next action
    next_action = NEXT_STAGE.get(memo, ("Unknown stage — review manually", 0, None, False))

    delinquent.append({
        "id":          acct_id,
        "unit_id":     acct.get("UnitId"),
        "current_stage": memo,
        "last_notice": date,
        "total_fines": total,
        "next_stage":  next_action[0],
        "next_fine":   next_action[1],
        "certified":   next_action[3],
        "delinquency_status": acct.get("DelinquencyStatus", ""),
    })

    # Progress indicator
    if (i + 1) % 50 == 0:
        print(f"  ...scanned {i+1}/{len(active)} accounts, {len(delinquent)} delinquent so far")

print(f"\n  ✅ Scan complete. {len(delinquent)} delinquent accounts found out of {len(active)} active.\n")
print(f"  {'ID':<10} {'Last Notice':<14} {'Next Action':<40} {'Fine':>6} {'Cert':>5}")
print(f"  {'-'*10} {'-'*14} {'-'*40} {'-'*6} {'-'*5}")
for d in sorted(delinquent, key=lambda x: x["last_notice"]):
    cert = "YES" if d["certified"] else ""
    print(f"  {str(d['id']):<10} {d['last_notice']:<14} {d['next_stage']:<40} "
          f"${d['next_fine']:>5.0f} {cert:>5}")

print(f"\n  Accounts needing certified mail: "
      f"{sum(1 for d in delinquent if d['certified'])}")
print(f"  Total fines to be posted this cycle: "
      f"${sum(d['next_fine'] for d in delinquent):.2f}")

# ── Show actual memo text for unknown stage accounts ─────────────
unknown = [d for d in delinquent if d["next_stage"] == "Unknown stage — review manually"]
if unknown:
    print(f"\n\n  UNKNOWN STAGE ACCOUNTS — actual memo text ({len(unknown)} accounts)")
    print(f"  (Need to add these to the stage map)\n")
    for u in unknown:
        acct_id = u["id"]
        r = requests.get(
            f"{BASE}/associations/ownershipaccounts/{acct_id}/charges",
            headers=HEADERS,
            params={"limit": 100}
        )
        if r.status_code == 200:
            charges  = r.json()
            coll     = [c for c in charges
                        if any(l.get("GLAccountId") in COLLECTIONS_GL_IDS
                               for l in c.get("Lines", []))]
            if coll:
                latest = sorted(coll, key=lambda x: x["Date"], reverse=True)[0]
                print(f"  ID {acct_id} | {latest['Date']} | MEMO: '{latest['Memo']}'")
                # Show full collections history for this account
                for c in sorted(coll, key=lambda x: x["Date"]):
                    print(f"             {c['Date']}  ${c['TotalAmount']:.0f}  {c['Memo']}")
                print()

# ── Explore transactions endpoint — the key to balance calculation ──
print(f"\n  Exploring transactions for account 22390 (known delinquent)...")
r = requests.get(
    f"{BASE}/associations/ownershipaccounts/22390/transactions",
    headers=HEADERS,
    params={"limit": 50}
)
if r.status_code == 200:
    txns = r.json()
    print(f"  Total transactions: {len(txns)}")

    # Show unique TransactionTypeEnum values
    types = set(t.get("TransactionTypeEnum") for t in txns)
    print(f"  TransactionTypeEnum values: {types}")

    # Show all transactions sorted by date
    print(f"\n  {'Date':<14} {'Type':<25} {'Amount':>10}  Memo/Journal")
    print(f"  {'-'*14} {'-'*25} {'-'*10}  {'-'*30}")
    running_balance = 0
    for t in sorted(txns, key=lambda x: x["Date"]):
        ttype  = t.get("TransactionTypeEnum", "")
        amount = t.get("TotalAmount", 0) or 0
        date   = t.get("Date", "")
        journal = t.get("Journal", {}) or {}
        memo   = ""
        if isinstance(journal, dict):
            lines = journal.get("Lines", []) or []
            if lines and isinstance(lines[0], dict):
                memo = lines[0].get("Memo", "") or ""
        # Payments are negative to the owner's balance
        if "payment" in ttype.lower() or "credit" in ttype.lower():
            running_balance -= amount
        else:
            running_balance += amount
        print(f"  {date:<14} {ttype:<25} ${amount:>9.2f}  {memo[:35]}")
    print(f"\n  Calculated running balance: ${running_balance:.2f}")
else:
    print(f"  HTTP {r.status_code}: {r.text[:200]}")

print("\n" + "="*55)
print("  Copy the GL Account IDs above into sbr_collections_automation.py")