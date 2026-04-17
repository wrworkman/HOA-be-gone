"""
Buildium Mailings API Diagnostic
Probes the communications endpoints to find the correct EZ Mail endpoint.
Run: python check_mailings_api.py
"""
import requests
import json
import base64
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["BUILDIUM_CLIENT_ID"]
CLIENT_SECRET = os.environ["BUILDIUM_CLIENT_SECRET"]

HEADERS = {
    "x-buildium-client-id":     CLIENT_ID,
    "x-buildium-client-secret": CLIENT_SECRET,
    "Content-Type":             "application/json",
    "Accept":                   "application/json",
}

BASE = "https://api.buildium.com/v1"

print("=" * 60)
print("Buildium Mailings API Diagnostic")
print("=" * 60)

# 1. Test the endpoint we're currently using
endpoints_to_test = [
    "/communications/mailings",
    "/communications/announcements",
    "/communications/emailcampaigns",
    "/associations/ownershipaccounts/communications",
    "/mailings",
    "/communications",
]

print("\n--- Testing GET on communication endpoints ---")
for path in endpoints_to_test:
    url = BASE + path
    r = requests.get(url, headers=HEADERS, params={"limit": 1})
    print(f"  GET {path}  →  {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    ✅ EXISTS — sample: {json.dumps(data)[:200]}")

# 2. Try to get mailing templates
print("\n--- Looking for mailing templates ---")
template_endpoints = [
    "/communications/mailings/templates",
    "/communications/templates",
    "/mailingtemplates",
]
for path in template_endpoints:
    url = BASE + path
    r = requests.get(url, headers=HEADERS, params={"limit": 5})
    print(f"  GET {path}  →  {r.status_code}")
    if r.status_code == 200:
        print(f"    ✅ Templates found: {json.dumps(r.json())[:300]}")

# 3. Check what a POST to mailings returns (don't actually send — just check the error)
print("\n--- Testing POST /communications/mailings (minimal payload) ---")
r = requests.post(
    f"{BASE}/communications/mailings",
    headers=HEADERS,
    json={}
)
print(f"  Status: {r.status_code}")
print(f"  Response: {r.text[:500]}")

print("\n--- Testing POST /communications/mailings (our current payload) ---")
r = requests.post(
    f"{BASE}/communications/mailings",
    headers=HEADERS,
    json={
        "AssociationIds":      [103158],
        "OwnershipAccountIds": [22518],
        "TemplateName":        "60-Day Collection Notice",
        "GroupByUnit":         False,
        "SendEzMail":          True,
    }
)
print(f"  Status: {r.status_code}")
print(f"  Response: {r.text[:500]}")

# 4. Check announcements POST structure (what fields does it accept?)
print("\n--- Testing POST /communications/announcements (empty payload to see validation errors) ---")
r = requests.post(f"{BASE}/communications/announcements", headers=HEADERS, json={})
print(f"  Status: {r.status_code}")
print(f"  Response: {r.text[:800]}")

# 5. Check if announcements can target specific ownership accounts
print("\n--- GET /communications/announcements (look at structure of existing ones) ---")
r = requests.get(f"{BASE}/communications/announcements", headers=HEADERS, params={"limit": 2})
if r.status_code == 200:
    print(json.dumps(r.json(), indent=2)[:1000])

# 6. Look for owner-specific communication endpoints
print("\n--- Testing owner-specific communication endpoints ---")
owner_endpoints = [
    "/associations/ownershipaccounts/22518/communications",
    "/associations/owners/communications",
    "/associations/communications",
    "/communications/announcements?ownershipaccountids=22518",
]
for path in owner_endpoints:
    url = BASE + path if not path.startswith("/communications/announcements?") else BASE + path
    r = requests.get(url, headers=HEADERS)
    print(f"  GET {path}  →  {r.status_code}")
    if r.status_code == 200:
        print(f"    ✅ {r.text[:200]}")

# 7. Check templates more carefully - filter by owner/mailing type
print("\n--- GET /communications/templates (look for owner/mailing templates) ---")
r = requests.get(f"{BASE}/communications/templates", headers=HEADERS, params={"limit": 50})
if r.status_code == 200:
    templates = r.json()
    print(f"  Total templates: {len(templates)}")
    for t in templates:
        print(f"  ID {t['Id']:4d} | Type: {t.get('RecipientType','?'):20s} | {t['Name']}")

# 8. Fetch content of the 60-Day template
print("\n--- GET /communications/templates/4210 (60-Day Late Notice content) ---")
r = requests.get(f"{BASE}/communications/templates/4210", headers=HEADERS)
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    print(json.dumps(r.json(), indent=2)[:1000])

# 9. Get owner contact info for account 22518
print("\n--- GET ownership account 22518 details (looking for email) ---")
r = requests.get(f"{BASE}/associations/ownershipaccounts/22518", headers=HEADERS)
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    print(json.dumps(r.json(), indent=2)[:500])

# 10. Get owners linked to account 22518
print("\n--- GET owners for account 22518 ---")
r = requests.get(f"{BASE}/associations/owners", headers=HEADERS,
                 params={"ownershipaccountids": "22518", "limit": 5})
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    for owner in data:
        print(f"  Name: {owner.get('FirstName')} {owner.get('LastName')}")
        print(f"  Email: {owner.get('Email')}")
        print(f"  PrimaryAddress: {owner.get('PrimaryAddress')}")

# 11. Test ownership-account-level mailing endpoints (different path than communications/mailings)
print("\n--- Testing account-level and association-level mailing endpoints ---")
mailing_endpoints = [
    f"/associations/ownershipaccounts/22518/mailings",
    f"/associations/103158/mailings",
    f"/associations/mailings",
    f"/communications/letters",
    f"/communications/mailings/letters",
]
for path in mailing_endpoints:
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params={"limit": 1})
    print(f"  GET {path}  →  {r.status_code}")
    if r.status_code == 200:
        print(f"    ✅ {r.text[:200]}")

# Try POST to account-level mailing with template
print("\n--- TEST: POST mailing at account level ---")
r = requests.post(
    f"{BASE}/associations/ownershipaccounts/22518/mailings",
    headers=HEADERS,
    json={"TemplateId": 4210, "SendEzMail": False}  # SendEzMail=False = safe, just creates draft
)
print(f"  Status: {r.status_code}")
print(f"  Response: {r.text[:400]}")

# 12 (renumbered). Test if announcements accepts TemplateId + OwnershipAccountIds
# This would let us use Buildium's own templates targeted to specific owners
print("\n--- TEST: POST announcement with TemplateId targeting specific owner ---")
print("  (Empty body/subject since template provides them — testing if Buildium accepts this)")
r = requests.post(
    f"{BASE}/communications/announcements",
    headers=HEADERS,
    json={
        "TemplateId":           4210,         # 60-Day Late Notice
        "PropertyIds":          [103158],     # SBR association
        "OwnershipAccountIds":  [22518],      # specific delinquent account
        "IncludeAlternateEmail": False,
        "NotifyAssociationTenants": False,
        "Subject":              "DO NOT SEND - API TEST",
        "Body":                 "DO NOT SEND - API TEST",
    }
)
print(f"  Status: {r.status_code}")
print(f"  Response: {r.text[:600]}")

# 12. Test if announcements accepts OwnershipAccountIds without TemplateId
print("\n--- TEST: Does POST announcements accept OwnershipAccountIds field at all? ---")
r = requests.post(
    f"{BASE}/communications/announcements",
    headers=HEADERS,
    json={
        "Subject":              "DO NOT SEND - API TEST",
        "Body":                 "DO NOT SEND - API TEST",
        "PropertyIds":          [103158],
        "OwnershipAccountIds":  [22518],
        "IncludeAlternateEmail": False,
        "NotifyAssociationTenants": False,
    }
)
print(f"  Status: {r.status_code}")
print(f"  Response: {r.text[:600]}")

print("\n" + "=" * 60)
print("Done.")
