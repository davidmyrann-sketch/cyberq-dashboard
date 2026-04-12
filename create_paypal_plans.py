#!/usr/bin/env python3
"""
Run once to create PayPal product + subscription plans.
Prints the Plan IDs to paste into Railway env vars.

Usage:
  PAYPAL_CLIENT_ID=xxx PAYPAL_CLIENT_SECRET=yyy python3 create_paypal_plans.py

Add --sandbox to use sandbox environment (default is live).
"""
import json, base64, sys, os, urllib.request, ssl

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

SANDBOX = "--sandbox" in sys.argv
BASE    = "https://api-m.sandbox.paypal.com" if SANDBOX else "https://api-m.paypal.com"
ENV     = "SANDBOX" if SANDBOX else "LIVE"

CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")

if not CLIENT_ID or not CLIENT_SECRET:
    print("ERROR: Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET as env vars first.")
    sys.exit(1)

def get_token():
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req   = urllib.request.Request(
        f"{BASE}/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10, context=_ctx) as r:
        return json.loads(r.read())["access_token"]

def pp_post(path, body, token):
    import uuid as _uuid
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization":    f"Bearer {token}",
            "Content-Type":     "application/json",
            "PayPal-Request-Id": _uuid.uuid4().hex,
            "Prefer":           "return=representation",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ctx) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"\nHTTP {e.code} on {path}")
        print(f"Headers: {dict(e.headers)}")
        print(f"Body: {err_body!r}\n")
        raise

print(f"\n{'='*50}")
print(f"  Creating PayPal plans ({ENV})")
print(f"{'='*50}\n")

token = get_token()
print("✓ Got access token\n")

# 1. Create product
product = pp_post("/v1/catalog/products", {
    "name":        "CyberQ Dashboard",
    "description": "Advanced BBQ temperature monitoring and control dashboard",
    "type":        "SERVICE",
}, token)
prod_id = product["id"]
print(f"✓ Product created: {prod_id}\n")

# 2. Monthly plan — $9/month
monthly = pp_post("/v1/billing/plans", {
    "product_id":  prod_id,
    "name":        "CyberQ Dashboard — Monthly",
    "description": "$9/month subscription",
    "status":      "ACTIVE",
    "billing_cycles": [{
        "frequency":       {"interval_unit": "MONTH", "interval_count": 1},
        "tenure_type":     "REGULAR",
        "sequence":        1,
        "total_cycles":    0,
        "pricing_scheme":  {"fixed_price": {"value": "9.00", "currency_code": "USD"}},
    }],
    "payment_preferences": {
        "auto_bill_outstanding":     True,
        "payment_failure_threshold": 3,
    },
}, token)
monthly_id = monthly["id"]
print(f"✓ Monthly plan created: {monthly_id}")

# 3. Annual plan — $79/year
annual = pp_post("/v1/billing/plans", {
    "product_id":  prod_id,
    "name":        "CyberQ Dashboard — Annual",
    "description": "$79/year subscription (save 27%)",
    "status":      "ACTIVE",
    "billing_cycles": [{
        "frequency":       {"interval_unit": "YEAR", "interval_count": 1},
        "tenure_type":     "REGULAR",
        "sequence":        1,
        "total_cycles":    0,
        "pricing_scheme":  {"fixed_price": {"value": "79.00", "currency_code": "USD"}},
    }],
    "payment_preferences": {
        "auto_bill_outstanding":     True,
        "payment_failure_threshold": 3,
    },
}, token)
annual_id = annual["id"]
print(f"✓ Annual plan created:  {annual_id}\n")

print("="*50)
print("  Paste these into Railway Variables:")
print("="*50)
print(f"\nPAYPAL_PLAN_MONTHLY={monthly_id}")
print(f"PAYPAL_PLAN_ANNUAL={annual_id}\n")
