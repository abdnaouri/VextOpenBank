# VextOpenBank — Comprehensive API Reference

> **API Prefix:** `/api/v1`  
> **Interactive Documentation:** `/docs` (Swagger UI) & `/redoc` (ReDoc)  
> **Authentication:** `Authorization: Bearer <KEY>` or `X-Vext-Key: <KEY>`

---

## Table of Contents
1. [EnvoiSMS Automated Bank Bridge (`/envoisms`)](#1-envoisms-bridge)
2. [Accounts & Balances (`/accounts`)](#2-accounts--balances)
3. [Cards Lifecycle Management (`/cards`)](#3-cards-management)
4. [Domestic Transfers & Payees (`/transfers`)](#4-transfers--payees)
5. [Personal Finance Management (`/pfm`)](#5-personal-finance-management)
6. [Core Features & Utilities (`/features`)](#6-features--utilities)
7. [Bank Communications & CRM (`/comms`)](#7-bank-communications--crm)
8. [Agency & Market Rates (`/agency`)](#8-agency--market-rates)
9. [Step-Up MFA & Security (`/security`)](#9-step-up-mfa--security)
10. [Open Banking & Automations (`/automation`)](#10-open-banking--automations)
11. [Service Catalog (`/catalog`)](#11-service-catalog)
12. [Customer Profile (`/profile`)](#12-customer-profile)

---

## 1. EnvoiSMS Bridge (`/api/v1/envoisms`)

### `GET /instructions`
Returns canonical CFG Bank account details and unique motif for EnvoiSMS top-up.
* **Auth**: None (Public)
* **Query Params**: `reference` (string, optional), `amount_mad` (float, optional, default: 275.0)
* **Response**: `200 OK`
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
  "mandatory_motif": "ENV-10001",
  "amount_mad": 275.0,
  "notice": "Votre recharge EnvoiSMS est validée et créditée automatiquement dès détection des fonds."
}
```

### `POST /check-topup`
Sub-5ms zero-latency top-up check against the local ledger.
* **Auth**: Required
* **Body**:
```json
{
  "topup_id": "topup_01hx",
  "account_id": "acc_001",
  "amount_mad": 275.0,
  "reference": "ENV-10001",
  "customer_name": "ACME CORP",
  "customer_phone": "0661123456"
}
```
* **Response**: `200 OK` (Status: `CONFIRMED` or `PENDING_CONFIRMATION` or `ALREADY_RECONCILED`)

### `GET /sync-status`
Diagnostic metrics on the bridge, anti-throttling cooldown, and active pending intents.
* **Auth**: Required
* **Response**: `200 OK`
```json
{
  "status": "OPERATIONAL",
  "origin_masking": "ACTIVE (Strict mobile app headers, zero client IP forwarding)",
  "anti_throttling_cooldown_seconds": 25.0,
  "seconds_since_last_cfg_sync": 4.2,
  "cooldown_active": true,
  "active_pending_intents": 0,
  "local_ledger_ready": true
}
```

### `POST /simulate-incoming-transfer`
Simulates an incoming transfer arriving on the CFG Bank ledger (integration tests & demos).
* **Auth**: Required
* **Body**: `{"amount_mad": 275.0, "sender_name": "ACME CORP", "motif": "Recharge ENV-10001"}`
* **Response**: `200 OK`

---

## 2. Accounts & Balances (`/api/v1/accounts`)

### `GET /`
Retrieve active bank account summaries, ledger balances, and IDs.
* **Response**: `200 OK`
```json
{
  "account_name": "Compte courant",
  "account_type": "COURANT",
  "currency": "MAD",
  "balance": 142500.50,
  "balance_display": "142 500,50 MAD",
  "rib": "050810006011833971200189"
}
```

### `GET /rib`
Returns structured Moroccan 24-digit RIB breakdown.
* **Response**: `200 OK` (bank_code: "051", branch_code: "810", account_number: "0060118339712001", rib_key: "89")

### `POST /validate-rib`
Validates any 24-digit Moroccan RIB using the national Modulo-97 checksum formula:
$$\text{Key} = 97 - ((89 \times \text{Bank} + 15 \times \text{Branch} + 3 \times \text{Account}) \pmod{97})$$
* **Query/Body**: `rib="050810006011833971200189"`
* **Response**: `200 OK` (`is_valid: true`, `calculated_key: 89`, `provided_key: 89`)

---

## 3. Cards Management (`/api/v1/cards`)

* `GET /`: Lists all active physical & virtual debit/credit cards, current limits, and statuses.
* `POST /{card_id}/toggle-lock`: Remotely freeze or unfreeze a card.
* `POST /{card_id}/limits`: Adjust daily ATM withdrawal and e-commerce payment limits.
* `POST /{card_id}/ecommerce`: Toggle contactless and international e-commerce flags.

---

## 4. Transfers & Payees (`/api/v1/transfers`)

* `GET /payees`: Lists registered domestic payees from `CFGManagePayees`.
* `POST /payees`: Register a new payee with Modulo-97 validation.
* `POST /execute`: Execute a domestic transfer via `CFGTransfersPaymentsOverview` (Requires elevated step-up token).
* `GET /impayes`: List unpaid direct debits and bounced cheques (`CFGImpayees`) for automated ERP reconciliation.

---

## 5. Personal Finance Management (`/api/v1/pfm`)

* `GET /categories`: Taxonomy of spending categories (Groceries, Utilities, Travel, Telecom, Taxes).
* `GET /donut`: Current month liquidity and category expense distribution (`CFGDataServices/cfgPFMDonut`).
* `GET /transactions`: Categorized transaction ledger with search and date filters.

---

## 6. Features & Utilities (`/api/v1/features`)

* `GET /dotations`: Real-time status of Office des Changes quotas (Dotation Voyage & E-Commerce) via `CFGEndowLanding`.
* `POST /dotations/allocate`: Allocate annual travel allowance to international debit cards.
* `GET /billers`: Discovery of billers (Water, Electricity, Telecom, Tax stamps).
* `POST /billers/inquire`: Inquire bill reference and amount due via `CFGBillers`.
* `POST /billers/pay`: Direct bill settlement via `CFGActionBillPayment`.
* `POST /wallet/transfer`: Interbank Moroccan mobile wallet transfer via GSM phone number or EMVCo QR code (`CFGWalletCreate`).
* `POST /cheques/deposit`: Remote check deposit with CMC7 MICR optical recognition and endorsement image upload.

---

## 7. Bank Communications & CRM (`/api/v1/comms`)

* `GET /inbox`: Secure internal banking messaging inbox (`CFGDetailMessages`).
* `POST /inbox/batch`: Mark read or delete messages in bulk (`CFGMessagesMultiActions`).
* `POST /incident/create`: Open a support incident directly inside CFG Bank's Microsoft Dynamics CRM.
* `POST /push/subscribe`: Register device token for Kony Push Notification Service (KPNS).

---

## 8. Agency & Market Rates (`/api/v1/agency`)

* `GET /locations`: Geolocation lookup for CFG branches and ATMs (`CFGBankLocations`).
* `POST /appointments`: Book or reschedule branch advisory appointments (`CFGMeetingRequestMultipleDel`).
* `GET /rates`: Live interbank exchange rates published by Bank Al-Maghrib (EUR, USD, GBP, SAR).

---

## 9. Step-Up MFA & Security (`/api/v1/security`)

* `POST /mfa/challenge`: Trigger SMS OTP or Entrust push challenge (`MFAOtp`).
* `POST /mfa/verify`: Submit 6-digit OTP code to acquire elevated claims authorization token.
* `GET /token/validate`: Validate status of active claims JWT (`CFGValidateToken`).

---

## 10. Open Banking & Automations (`/api/v1/automation`)

* `GET /open-banking/accounts`: Berlin Group NextGenPSD2 Account Information Services (AIS).
* `GET /open-banking/accounts/{id}/balances`: NextGenPSD2 Account Balances Sub-Resource (`closingBooked`, `expected`).
* `GET /open-banking/accounts/{id}/transactions`: NextGenPSD2 Account Transactions Sub-Resource.
* `GET /export/csv`: Bookkeeping transaction export in standard CSV format.
* `POST /monitor/balance`: Configure automated threshold balance alerts.

---

## 11. Service Catalog (`/api/v1/catalog`)

* `GET /services`: Complete catalog of 42 discovered MobileFabric integration services.
* `GET /billers`: Taxonomy of national Moroccan utility and telecom billers.

---

## 12. Customer Profile (`/api/v1/profile`)

* `GET /`: Customer KYC profile, corporate entity ID, and registration metadata.
* `GET /advisor`: Managing branch contact, relationship manager name, phone, and direct email.
