"""
Test whether Buildium API allows writing DelinquencyStatus on ownership accounts.
Run: python check_delinquency_write.py
"""
import requests, os
from dotenv import load_dotenv
load_dotenv()

HEADERS = {
    "x-buildium-client-id":     os.environ["BUILDIUM_CLIENT_ID"],
    "x-buildium-client-secret": os.environ["BUILDIUM_CLIENT_SECRET"],
    "Content-Type": "application/json",
    "Accept":       "application/json",
}
BASE = "https://api.buildium.com/v1"

# First read the current value for 22518
r = requests.get(f"{BASE}/associations/ownershipaccounts/22518", headers=HEADERS)
print(f"Current 22518: DelinquencyStatus = {r.json().get('DelinquencyStatus')}")

# Try PATCH to set PaymentPlan
r = requests.patch(
    f"{BASE}/associations/ownershipaccounts/22518",
    headers=HEADERS,
    json={"DelinquencyStatus": "PaymentPlan"}
)
print(f"PATCH status: {r.status_code}")
print(f"Response: {r.text[:400]}")

# Try PUT
r = requests.put(
    f"{BASE}/associations/ownershipaccounts/22518",
    headers=HEADERS,
    json={"DelinquencyStatus": "PaymentPlan"}
)
print(f"PUT status: {r.status_code}")
print(f"Response: {r.text[:400]}")
