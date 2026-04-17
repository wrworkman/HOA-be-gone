"""
check_template_content.py
Fetches one Buildium EZ Mail template and prints its full structure
so we can see the body HTML and exact merge field format.

Run:
    python check_template_content.py
"""

import requests
import json
from dotenv import load_dotenv
import os

load_dotenv()

BUILDIUM_BASE = "https://api.buildium.com/v1"
HEADERS = {
    "x-buildium-client-id":     os.environ["BUILDIUM_CLIENT_ID"],
    "x-buildium-client-secret": os.environ["BUILDIUM_CLIENT_SECRET"],
    "Content-Type":             "application/json",
}

# Template IDs confirmed from diagnostic run
TEMPLATE_IDS = {
    4210: "60-Day Late Notice",
    4387: "90-Day Late Notice",
    4388: "120-Day Late Notice",
    4389: "150-Day Pre-Lien Notice",
    4390: "180-Day Lien Notice",
    4391: "Advanced Stage of Delinquency",
    4392: "Pre-Legal 60-Day Notice",
    4393: "Pre-Legal Final Notice",
}

print("=" * 70)
print("Fetching Buildium template content")
print("=" * 70)

# ── Try multiple likely endpoint patterns ────────────────────────────────
endpoints_to_try = [
    f"{BUILDIUM_BASE}/communications/emailtemplates/4210",
    f"{BUILDIUM_BASE}/communications/emailtemplates?ids=4210",
    f"{BUILDIUM_BASE}/communications/templates/4210",
    f"{BUILDIUM_BASE}/communications/templates?ids=4210",
    f"{BUILDIUM_BASE}/communications/mailingtemplates/4210",
    f"{BUILDIUM_BASE}/communications/mailingtemplates?ids=4210",
]

working_endpoint = None
for url in endpoints_to_try:
    r = requests.get(url, headers=HEADERS)
    print(f"  {r.status_code}  {url}")
    if r.status_code == 200:
        working_endpoint = url
        print(f"\n✅ Found working endpoint: {url}")
        print("\nFull response:")
        print(json.dumps(r.json(), indent=2))
        break

if not working_endpoint:
    print("\n❌ None of those endpoints returned 200.")
    print("   Trying a broader list of all templates...")
    list_attempts = [
        f"{BUILDIUM_BASE}/communications/emailtemplates",
        f"{BUILDIUM_BASE}/communications/templates",
        f"{BUILDIUM_BASE}/communications/mailingtemplates",
    ]
    for url in list_attempts:
        r = requests.get(url, headers=HEADERS, params={"limit": 5})
        print(f"  {r.status_code}  {url}")
        if r.status_code == 200:
            data = r.json()
            print(f"\n✅ List endpoint works: {url}")
            print(f"   Returned {len(data)} items. First item:")
            if data:
                print(json.dumps(data[0], indent=2))
            break

print("\nDone.")
