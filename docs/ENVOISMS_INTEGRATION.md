# EnvoiSMS.ma Automated Bank Bridge — Integration Guide

> **Partner:** EnvoiSMS.ma  
> **Protocol:** HTTPS REST + HMAC-SHA256 Webhook Callbacks  
> **Gateway Base URL:** `https://vext-open-bank-edge.a-naouri-10d.workers.dev/api/v1/envoisms`  
> **Direct Origin (Protected):** `https://vext-open-bank.onrender.com/api/v1/envoisms`

---

## 1. Overview & Key Capabilities

The EnvoiSMS Bank Bridge powers instantaneous top-ups for EnvoiSMS accounts via Moroccan domestic interbank transfers (Virement Instantané BAM & Virement Ordinaire).

* **Sub-5ms Edge Latency**: Initial top-up checks resolve from local SQLite ledgers with zero synchronous round-trips to CFG Bank.
* **Double-Spend & Collision Protection**: Reconciled bank transactions are locked atomically with SQLite uniqueness constraints.
* **Cryptographic Proof**: Confirmed top-ups return an HMAC-SHA256 digital signature guaranteeing authenticity.
* **Anti-Throttling Shield**: Outbound synchronization with CFG Bank enforces a 25-second cooldown with randomized jitter ($\pm 15\%$), insulating the bank connection from high customer polling volumes.

---

## 2. API Endpoints Specification

### A. Banking Instructions
Retrieve canonical bank RIB and transfer motif instructions to display to the customer during checkout.

* **Endpoint**: `GET /api/v1/envoisms/instructions`
* **Access**: Public (No authentication headers required)
* **Query Parameters**:
  * `reference` (optional): Unique order reference code (e.g. `ENV-98421`).
  * `amount_mad` (optional, default: `275.0`): Expected top-up amount in Moroccan Dirhams.

#### Response (`200 OK`)
```json
{
  "bank_name": "CFG Bank",
  "bank_code": "051",
  "agency": "Agence Rabat Zaer / Casablanca",
  "beneficiary_name": "VEXT AI",
  "rib": "050810006011833971200189",
  "rib_formatted": "050 810 0060118339712001 89",
  "swift_bic": "CFGBMAMC",
  "transfer_type": "Virement Instantané (BAM) ou Virement Ordinaire",
  "mandatory_motif": "ENV-98421",
  "amount_mad": 275.0,
  "notice": "Votre recharge EnvoiSMS est validée et créditée automatiquement dès détection des fonds."
}
```

---

### B. Check & Reconcile Top-Up
Check if funds have arrived on CFG Bank. If matched, immediately locks the credit and signs the confirmation.

* **Endpoint**: `POST /api/v1/envoisms/check-topup`
* **Access**: Authenticated via `X-Vext-Key` or `X-EnvoiSMS-Secret`
* **Request Headers**:
  * `Content-Type: application/json`
  * `X-Vext-Key: <INTERNAL_API_KEY>` or `X-EnvoiSMS-Secret: <SHARED_SECRET>`
* **Request Body**:
```json
{
  "topup_id": "topup_01hx892ka710",
  "account_id": "acc_client_9941",
  "amount_mad": 275.0,
  "reference": "ENV-98421",
  "customer_name": "ACME CORP SARL",
  "customer_phone": "0661123456"
}
```

#### Response Cases

##### Case 1: Transfer Not Yet Observed (`PENDING_CONFIRMATION`)
```json
{
  "status": "PENDING_CONFIRMATION",
  "message": "Aucun virement de 275.00 MAD correspondant à la référence 'ENV-98421' n'a encore été détecté sur le compte CFG Bank VEXT AI.",
  "topup_id": "topup_01hx892ka710",
  "amount_mad": 275.0,
  "checked_at": "2026-11-05T03:17:16.202405"
}
```

##### Case 2: Transfer Matched & Reconciled (`CONFIRMED`)
```json
{
  "status": "CONFIRMED",
  "message": "Virement bancaire de 275.00 MAD confirmé avec succès sur CFG Bank !",
  "details": {
    "event": "bank_transfer.confirmed",
    "topup_id": "topup_01hx892ka710",
    "account_id": "acc_client_9941",
    "amount_mad": 275.0,
    "amount_eur": 25.46,
    "bank_tx_id": "CFG-20261105031200-9842",
    "bank_name": "CFG Bank",
    "beneficiary_name": "VEXT AI",
    "beneficiary_rib": "050810006011833971200189",
    "sender_name": "ACME CORP SARL",
    "transfer_motif": "Recharge EnvoiSMS ENV-98421",
    "timestamp": "2026-11-05T03:17:16.205112",
    "signature": "3f98a72b0c41097ef14890d7ab8720..."
  }
}
```

##### Case 3: Already Reconciled (`ALREADY_RECONCILED`)
```json
{
  "status": "ALREADY_RECONCILED",
  "topup_id": "topup_01hx892ka710",
  "bank_tx_id": "CFG-20261105031200-9842",
  "amount_mad": 275.0,
  "reconciled_at": "2026-11-05T03:17:16.205112"
}
```

---

### C. Bridge Sync Diagnostics
Inspect operational status, last sync timestamp, and cooldown health.

* **Endpoint**: `GET /api/v1/envoisms/sync-status`
* **Access**: Authenticated via `X-Vext-Key`
* **Response (`200 OK`)**:
```json
{
  "status": "OPERATIONAL",
  "origin_masking": "ACTIVE (Strict mobile app headers, zero client IP forwarding)",
  "anti_throttling_cooldown_seconds": 25.0,
  "seconds_since_last_cfg_sync": 4.1,
  "cooldown_active": true,
  "active_pending_intents": 2,
  "local_ledger_ready": true
}
```

---

## 3. Cryptographic Signature Verification

Every `CONFIRMED` response includes an HMAC-SHA256 signature calculated over the lexicographically sorted JSON payload using the shared secret (`ENVOISMS_VEXT_SECRET`).

### Python Verification
```python
import hmac
import hashlib
import json

def verify_envoisms_signature(details: dict, secret: str) -> bool:
    received_sig = details.get("signature", "")
    # Exclude the signature field itself when reconstructing payload
    payload_to_verify = {k: v for k, v in details.items() if k != "signature"}
    
    # Canonical JSON serialization (sorted keys, no extra whitespace)
    serialized = json.dumps(payload_to_verify, sort_keys=True).encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), serialized, hashlib.sha256).hexdigest()
    
    return hmac.compare_digest(received_sig, expected_sig)
```

### Node.js / JavaScript Verification
```javascript
const crypto = require("crypto");

function verifyEnvoiSmsSignature(details, secret) {
  const { signature, ...payload } = details;
  
  // Sort keys alphabetically
  const sortedKeys = Object.keys(payload).sort();
  const sortedPayload = {};
  sortedKeys.forEach(key => { sortedPayload[key] = payload[key]; });
  
  const serialized = JSON.stringify(sortedPayload);
  const expectedSignature = crypto
    .createHmac("sha256", secret)
    .update(serialized)
    .digest("hex");
    
  return crypto.timingSafeEqual(
    Buffer.from(signature, "hex"),
    Buffer.from(expectedSignature, "hex")
  );
}
```

### PHP Verification
```php
function verifyEnvoiSmsSignature(array $details, string $secret): bool {
    $receivedSig = $details['signature'] ?? '';
    unset($details['signature']);
    
    ksort($details);
    $serialized = json_encode($details, JSON_UNESCAPED_SLASHES);
    $expectedSig = hash_hmac('sha256', $serialized, $secret);
    
    return hash_equals($receivedSig, $expectedSig);
}
```
