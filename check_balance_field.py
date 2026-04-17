"""
Quick diagnostic — prints the raw Buildium API response for one account
so we can confirm the exact field name for the account balance.
"""
import requests
import json

CLIENT_ID     = "d33da506-8f83-4e5f-a808-04c3cb0842a6"
CLIENT_SECRET = "jhDyXiXoG1NAWVfxiy1pklL2nobQkleTbexdjAwbt78="

HEADERS = {
    "x-buildium-client-id":     CLIENT_ID,
    "x-buildium-client-secret": CLIENT_SECRET,
    "Content-Type":             "application/json",
}

# Check three accounts:
#   22398 — paid off 2/11, should be near-zero or freshly late (~$77)
#   22546 — paid off 4/1, should be $0
#   22471 — paid down to $12, should be very low
TEST_ACCOUNTS = [22398, 22546, 22471]

for acct_id in TEST_ACCOUNTS:
    r = requests.get(
        f"https://api.buildium.com/v1/associations/ownershipaccounts/{acct_id}",
        headers=HEADERS,
    )
    print(f"\n{'='*55}")
    print(f"Account {acct_id}  |  HTTP {r.status_code}")
    print(f"{'='*55}")
    if r.status_code == 200:
        data = r.json()
        # Print every field that looks balance-related
        balance_fields = {k: v for k, v in data.items()
                         if any(word in k.lower() for word in
                                ("balance", "amount", "due", "paid", "owing"))}
        print("Balance-related fields:")
        for k, v in balance_fields.items():
            print(f"  {k}: {v}")
        print("\nAll fields (keys + values):")
        print(json.dumps(data, indent=2))
    else:
        print(r.text[:300])
