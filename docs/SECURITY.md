# VextOpenBank — Security Architecture & Threat Model

> **Classification:** Confidential / Defense-in-Depth Specification  
> **Standard:** Banking-Grade API Shielding & Zero-Trust Perimeter

---

## 1. Zero-Trust Origin Isolation Model

```
[ Public Internet ]
        │
        ▼ (Strict HTTPS / TLS 1.3)
[ Cloudflare Global Edge Shield ]
   ├── WAF Threat Scoring (Score > 25 Blocked)
   ├── Rate Limiting (60 req/min per IP)
   ├── Authorization Gate (X-Vext-Key / HMAC verification)
   ├── Response Header Scrubbing (Removes Server, rndr-id, etc.)
   └── Cryptographic Token Injection (X-Origin-Shield: cf_shield_origin_auth_...)
        │
        ▼ (HTTPS / Private Forwarding)
[ Render Origin (vext-open-bank.onrender.com) ]
   └── enforce_internal_security Middleware
          ├── X-Origin-Shield == ORIGIN_SHIELD_SECRET? (Allow)
          ├── X-Vext-Key == INTERNAL_API_KEY? (Allow)
          ├── Path == /api/v1/envoisms/instructions? (Allow Public)
          └── Else: 403 Forbidden ("Direct origin access blocked.")
```

Any direct request made to `https://vext-open-bank.onrender.com` that does not carry the cryptographic `X-Origin-Shield` header injected by Cloudflare is immediately dropped with HTTP `403 Forbidden`.

---

## 2. Bank Origin Concealment & Egress Hygiene

When VextOpenBank communicates upstream with CFG Bank's MobileFabric Gateway (`:8443`), it strictly eliminates all downstream traces.

### Header Whitelisting Matrix

| Header Name | Forwarded to CFG? | Reason / Security Policy |
| :--- | :--- | :--- |
| `X-Forwarded-For` | **STRICTLY STRIPPED** | Prevents bank WAF from observing client/Render IP addresses |
| `CF-Connecting-IP` | **STRICTLY STRIPPED** | Prevents bank WAF from observing Cloudflare client IP |
| `Origin` | **STRICTLY STRIPPED** | Prevents leaking domain origins (e.g. `envoisms.ma`) |
| `Referer` | **STRICTLY STRIPPED** | Prevents leaking user referral sources |
| `X-Origin-Shield` | **STRICTLY STRIPPED** | Internal boundary token; never leaked upstream |
| `User-Agent` | **REPLACED** | Fixed Android 14 native app agent: `CFGBankApp/5.5.2 (Linux; U; Android 14; KB2003 Build/UKQ1.230924.001)` |
| `X-Kony-Platform-Type` | **INJECTED** | Set to `android` |
| `X-Kony-SDK-Version` | **INJECTED** | Set to `9.7.52` |
| `X-Kony-DeviceId` | **INJECTED** | Persistent UUID hardware identifier |
| `Accept-Language` | **INJECTED** | Standard Android locale: `fr-FR,fr;q=0.9,en-US;q=0.8` |

---

## 3. Behavioral AI & Anomaly Detection Evasion

Modern banking WAFs (such as F5 Distributed Cloud, Airlock IAM, and Shape Security) flag automated clients through behavioral heuristics:

### A. Cadence & Polling Jitter
* **Robotic Anti-Pattern**: Requests hitting servers on exact millisecond intervals (e.g., exactly every 30.000s) trigger machine learning scrapers alerts.
* **Our Implementation**: The background reconciliation daemon applies randomized Gaussian/uniform micro-jitter ($\pm 15\%$):
  $$\text{Interval} = 30.0 + \text{random.uniform}(-4.0, 6.0) \text{ seconds}$$
  Individual MobileFabric requests introduce a $50\text{ ms} - 180\text{ ms}$ micro-delay before dispatching.

### B. Authentication Churn Reduction
* **Robotic Anti-Pattern**: Re-authenticating (`/login`) before every single operation triggers credential-stuffing and session-churn alerts.
* **Our Implementation**: The client decodes the JWT expiration timestamp (`exp`) and caches the active token. It continues using the valid session until within 45 seconds of expiration. If the gateway ever returns HTTP 401, a single silent re-authentication is performed.

### C. Rate Limit Backoff Compliance
* If CFG Bank returns HTTP `429 Too Many Requests`, the client inspects the `Retry-After` header and halts execution for the requested duration (or 30s default) before resuming, avoiding aggressive retry penalties.

---

## 4. Hardware Profile Persistence

CFG Bank tracks device hardware profiles across sessions. To avoid appearing as a newly registered device on every service restart:
* The system checks `DEVICE_ID`, `CLIENT_ID`, and `PROFILE_ID` from the environment.
* If omitted, it reads or creates `/tmp/vext_persistent_device_profile.json`.
* Across worker restarts, server deploys, and process recycling, the identical hardware UUID is reused.
