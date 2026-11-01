import os
import json
import requests
import random
from typing import Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from dotenv import load_dotenv

# 1. Load credentials safely
load_dotenv()

if not os.getenv("GEMINI_API_KEY"):
    print("❌ ERROR: Missing GEMINI_API_KEY inside your .env file!")
    exit(1)

# 2. Initialize Gemini Client
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Strict Vendor Allowlist configuration matching company contracts
APPROVED_VENDOR_DOMAINS = {"aws.com", "github.com", "dell.com", "cisco.com"}
HARDCODED_MAVERICK_KEYWORDS = ["personal card", "personal credit card", "bypass", "reimburse", "reimbursement"]
SPEND_THRESHOLD_LIMIT = 1000.00

# Structured data schemas with Nullable/Optional fields to handle non-procurement text properly
class ProcurementIntent(BaseModel):
    is_purchase_request: bool
    item_name: Optional[str] = None
    quantity: Optional[int] = None
    total_cost: Optional[float] = None  # Renamed from estimated_cost for calculation accuracy
    vendor_email: Optional[str] = None
    is_compliant: bool
    reasoning: str

def get_paypal_access_token():
    """Attempts to fetch a live token, falls back gracefully if sandbox sends HTML"""
    url = "https://paypal.com"
    headers = {"Accept": "application/json", "Accept-Language": "en_US"}
    client_id = os.getenv("PAYPAL_CLIENT_ID", "MOCK_CLIENT_ID").strip()
    secret = os.getenv("PAYPAL_CLIENT_SECRET", "MOCK_SECRET").strip()
    
    try:
        response = requests.post(
            url, 
            headers=headers, 
            data={"grant_type": "client_credentials"}, 
            auth=(client_id, secret),
            timeout=8
        )
        
        if response.status_code == 200 and "<!DOCTYPE html>" in response.text:
            return "MOCK_SANDBOX_TOKEN_FALLBACK_ACTIVE"
            
        if response.status_code != 200:
            return "MOCK_SANDBOX_TOKEN_FALLBACK_ACTIVE"
            
        return response.json()["access_token"]
    except Exception:
        return "MOCK_SANDBOX_TOKEN_FALLBACK_ACTIVE"

def create_paypal_invoice(token, intent: ProcurementIntent, sender_email: str):
    """Mints an invoice record live against the correct PayPal Sandbox REST endpoint"""
    
    # Calculate perfect unit amount splitting to avoid quantity multiplication inflation
    qty = intent.quantity if (intent.quantity and intent.quantity > 0) else 1
    calculated_unit_value = round((intent.total_cost or 0.0) / qty, 2)

    if token == "MOCK_SANDBOX_TOKEN_FALLBACK_ACTIVE":
        mock_id = f"INV2-DSAI-{random.randint(100000, 999999)}"
        return {
            "id": mock_id,
            "href": f"https://paypal.com/{mock_id}",
            "status": "DRAFT_SIMULATED"
        }

    # FIX 1: Explicitly point to the absolute PayPal Sandbox Invoicing Endpoint
    url = "https://api-m.sandbox.paypal.com/v2/invoicing/invoices"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    payload = {
        "detail": {"currency_code": "USD", "note": f"DarkSpend.ai Auto PO for {sender_email}."},
        "invoicer": {"email_address": "finance-ops@yourcompany.com"},
        "primary_recipients": [{"billing_info": {"email_address": intent.vendor_email}}],
        "items": [{
            "name": intent.item_name,
            "quantity": str(qty),
            "unit_amount": {"currency_code": "USD", "value": str(calculated_unit_value)}
        }]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=8)
        if response.status_code not in [200, 201] or "<!DOCTYPE html>" in response.text:
            mock_id = f"INV2-DSAI-{random.randint(100000, 999999)}"
            return {
                "id": mock_id, 
                "href": f"https://paypal.com/{mock_id}",
                "status": "DRAFT_SIMULATED"
            }
        
        data = response.json()
        data["status"] = "LIVE_SANDBOX_MINTED"
        return data
    except Exception:
        mock_id = f"INV2-DSAI-{random.randint(100000, 999999)}"
        return {
            "id": mock_id, 
            "href": f"https://paypal.com/{mock_id}",
            "status": "DRAFT_SIMULATED"
        }

def process_mock_email(subject, body, sender):
    print(f"\n==================================================")
    print(f"📥 NEW INCOMING EMAIL FROM: {sender}")
    print(f"Subject: {subject}")
    print(f"--------------------------------------------------")
    
    print("🤖 Step 1: Querying Google Gemini model...")
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=f"Subject: {subject}\nBody: {body}",
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are an elite corporate procurement compliance auditing agent. "
                    "Analyze the unstructured text request from an employee. "
                    "1. Determine if the email relates to acquiring, spending money on, or claiming expenses for software, hardware, or business tools. If yes, 'is_purchase_request' MUST be True. "
                    "2. Evaluate policy compliance: If the employee explicitly mentions bypassing standard corporate channels, using a personal credit card for company assets, or requesting an after-the-fact reimbursement for off-catalog software tools, mark 'is_compliant' as False and document the precise policy violation context inside the 'reasoning' field."
                ),
                response_mime_type="application/json",
                response_schema=ProcurementIntent,
                temperature=0.0
            ),
        )
        intent = ProcurementIntent.model_validate_json(response.text)
        print("✅ Step 2: Gemini successfully parsed text data.")
    except Exception as e:
        print(f"❌ Gemini Processing Error: {str(e)}")
        return

    if not intent.is_purchase_request:
        print("ℹ️ AI Status: Ignored. Not a procurement request.")
        return
    
    # UPGRADE: Deterministic Code Validation layer (Protects against LLM hallucination)
    body_lower = body.lower()
    has_maverick_keyword = any(kw in body_lower for kw in HARDCODED_MAVERICK_KEYWORDS)
    
    # Extract vendor domain for allowlist verification check
    vendor_domain = ""
    if intent.vendor_email and "@" in intent.vendor_email:
        vendor_domain = intent.vendor_email.split("@")[-1].lower()

    if not intent.is_compliant or has_maverick_keyword:
        print("🚨 AI INTERCEPT: Maverick Spend Detected! Policy violation triggered.")
        print(f"   ↳ Code Audit Flag: {'Keyword Match' if has_maverick_keyword else 'LLM Guardrail'}")
        print(f"   ↳ Reasoning: {intent.reasoning}")
        print("⛔ Action: Aborting transaction pipeline to save corporate funds.")
        return

    # UPGRADE: Vendor Domain Contract Allowlist Verification Check
    if vendor_domain not in APPROVED_VENDOR_DOMAINS:
        print("🚨 COMPLIANCE BLOCK: Unapproved External Supplier Domain Caught.")
        print(f"   ↳ Extracted Domain: '{vendor_domain}' is not in corporate allowlist contracts.")
        print("⛔ Action: Halting automation loops pending supply-chain vetting.")
        return

    # UPGRADE: Financial Transaction Budget Cap Guardrail Check
    if intent.total_cost and intent.total_cost >= SPEND_THRESHOLD_LIMIT:
        print("⚠️ THRESHOLD ROUTE: High-Value Transaction Detected.")
        print(f"   ↳ Amount: ${intent.total_cost} exceeds automated approval cap (${SPEND_THRESHOLD_LIMIT}).")
        print("🤝 Action: Diverting transaction from automation to Manager Review Queue.")
        return

    print("💼 AI Status: Compliant Request Approved.")
    print(f"   ↳ Item: {intent.item_name} (Qty: {intent.quantity})")
    print(f"   ↳ Computed Total Value: ${intent.total_cost} (Unit cost: ${round(intent.total_cost/intent.quantity, 2)})")
    
    try:
        print("💳 Step 3: Authenticating with PayPal Developer Sandbox...")
        token = get_paypal_access_token()
        
        print("📝 Step 4: Structuring B2B invoice parameters...")
        paypal_record = create_paypal_invoice(token, intent, sender)
        
        # FIX 3: Clear Visual Honesty Mode between live network execution vs. gateway bypass triggers
        if paypal_record.get("status") == "DRAFT_SIMULATED":
            print("🎭 SUCCESS: SIMULATED invoice node generated (Sandbox Cloud Gateway bypassed)")
        else:
            print("🎉 SUCCESS: LIVE sandbox invoice created directly on PayPal rails!")
            
        print(f"   ↳ Generated PayPal Invoice ID: {paypal_record.get('id')}")
        print(f"   ↳ API Reference Endpoint Node: {paypal_record.get('href')}")
    except Exception as e:
        print(f"❌ PayPal Fintech Error: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Fully Patched DarkSpend.ai Enterprise Demo Engine...")
    
    # CASE 1: Compliant Request
    process_mock_email(
        subject="Procurement Request: GitHub Enterprise Seats",
        body="Hi Finance Team, our software department needs to add 15 additional team seats to our active GitHub Enterprise contract. The billing point email domain is billing@github.com and the estimated cost is 375.00 USD total. Please log this transaction parameters.",
        sender="engineering-manager@company.com"
    )
    
    # CASE 2: Maverick Spend Attempt
    process_mock_email(
        subject="Expense report claim for executive travel tool",
        body="Hey there, I decided to bypass our slow internal software portal yesterday and bought a premium AI itinerary builder application subscription using my personal corporate card because it was faster. The site was some startup tool. Refilling this via travel expense reimbursements please.",
sender="sales-vp@company.com"
)
# CASE 3: Messy Natural Language & Currency Variation
process_mock_email(
subject="need laptop stuff asap!!",
body="yo, we gotta grab 3 units of standard external computer monitor setups from our pre-cleared vendor Dell Operations. Their billing is monitors@dell.com. It looks like it is going to run about 210 dollars a piece for a total sum of 630.00 bucks. get it into the system tracker code.",
sender="it-support@company.com"
)
# CASE 4: Out-Of-Scope Conversational Text (Noise Immunity Test)
process_mock_email(
subject="Lunch plans for tomorrow afternoon",
body="Hey team, just wanted to check if anyone is free for a quick team lunch tomorrow around 1 PM at the taco place down the street? Let me know by end of day so I can grab a reservation block.",
sender="hr-team@company.com"
)
# CASE 5: High-Value Cap Threshold Exceeded (Triggers Manager Routing)
process_mock_email(
subject="URGENT: Core Datacenter Infrastructure Upgrade",
body="Finance team, we are executing our yearly hardware infrastructure refresh contract with Cisco Systems. We need to finalize 50 units of network router hardware switches immediately. Billing domain is sales@cisco.com. Total contract cost value is 85500.00 dollars.",
sender="cto-office@company.com"
)