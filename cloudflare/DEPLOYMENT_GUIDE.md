# Cloudflare Deployment & Architecture Guide for VextOpenBank

## 1. Cloudflare Architecture: Can Python / FastAPI Run on Cloudflare?

**The Technical Reality:**
* **Cloudflare Workers** runs a sandboxed V8 JavaScript/WebAssembly engine. While experimental Python Workers exist via Pyodide/WebAssembly, it lacks standard C-extension compatibility (e.g. standard SQLite driver hooks, full POSIX networking for `httpx` async keep-alive, and low-level socket controls needed for raw MobileFabric TLS handshakes).
* **The Production Solution:** **Cloudflare Tunnel (cloudflared)** connecting an unprivileged Docker container running your FastAPI app to Cloudflare's Edge.
  * **Zero Open Inbound Ports:** The container initiates an outbound encrypted QUIC/HTTP2 tunnel to Cloudflare Edge. Your server has no open ports on the public internet.
  * **Edge Shielding:** Cloudflare handles DDoS mitigation, SSL/TLS termination (TLS 1.3), Web Application Firewall (WAF), IP rate-limiting, and Bot Fight Mode before traffic ever reaches your API.
  * **Zero Trust Access:** Sensitive endpoints like `/docs` and `/redoc` can be restricted behind Cloudflare Access (SSO with Google, GitHub, or OTP).

---

## 2. Monthly Cost Breakdown: How Much Does It Cost to Validate?

### **Validation Tier (Dev / Testing / MVP)**
| Component | Provider / Product | Monthly Cost |
| :--- | :--- | :--- |
| **Edge Network & DNS** | Cloudflare Free Plan (Global CDN, DNS, DDoS protection) | **$0.00** |
| **Cloudflare Tunnel** | Cloudflare Zero Trust (Up to 50 users included free) | **$0.00** |
| **SSL / TLS Certificate** | Cloudflare Universal SSL (Auto-renewing edge cert) | **$0.00** |
| **WAF & Rate Limiting** | Cloudflare Free (5 WAF custom rules, basic rate limit) | **$0.00** |
| **Compute Origin** | Option A: Local Mac / Homelab via cloudflared<br>Option B: Fly.io / Hetzner Cloud / Render free tier<br>Option C: Hetzner CX22 (2 vCPU, 4GB RAM) | **$0.00**<br>**$0.00**<br>~$4.50 / month |
| **TOTAL TO VALIDATE** | | **$0.00 / month** (or ~$4.50/mo on dedicated VPS) |

### **Production Tier (Full High Availability)**
| Component | Provider / Product | Monthly Cost |
| :--- | :--- | :--- |
| **Edge Network** | Cloudflare Pro ($20/mo) or Free | $0.00 – $20.00 |
| **Cloudflare Workers Edge Proxy** | Paid Workers ($5/mo for 10M req/mo) | $5.00 |
| **Compute Origin** | 2x Redundant Cloud Instances (e.g., 2GB RAM each) | ~$10.00 – $15.00 |
| **ESTIMATED PRODUCTION COST** | | **~$15.00 – $35.00 / month** |

---

## 3. Security Hardening & Zero Secret Guarantee

To ensure that **no secret info is available on the app**:

1. **Zero Hardcoded Secrets**:
   - All credentials (MobileFabric App Key, App Secret, Airlock credentials, Device UUID, EnvoiSMS HMAC secret) are read dynamically from OS environment variables (`os.environ.get(...)`).
   - Default fallbacks for secrets have been eliminated; if an environment variable is missing, the service fails securely rather than leaking default test credentials.

2. **Docker Hardening**:
   - Multi-stage build (`builder` + `runner`).
   - Runs under non-root unprivileged user `vextuser` (UID: 10001).
   - Python bytecode disabled (`PYTHONDONTWRITEBYTECODE=1`), unbuffered output (`PYTHONUNBUFFERED=1`).
   - Healthcheck probe verifies internal responsiveness without exposing data.

3. **Cloudflare Edge Scrubbing**:
   - Strips backend headers (`Server`, `x-powered-by`, `x-volterra-location`).
   - Injects HTTP Strict Transport Security (`Strict-Transport-Security: max-age=31536000`).
   - Blocks automated scrapers with Cloudflare Threat Score > 25.

---

## 4. Step-by-Step Deployment Instructions

### Step 1: Install `cloudflared` (CLI)
```bash
# On macOS:
brew install cloudflare/cloudflare/cloudflared

# On Debian/Ubuntu:
curl -fsSL https://pkg.cloudflare.com/cloudflared-ascii.repo | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```

### Step 2: Authenticate and Create Tunnel
```bash
# Login to Cloudflare
cloudflared tunnel login

# Create tunnel named 'vext-open-bank'
cloudflared tunnel create vext-open-bank

# Associate domain routing (e.g., api.bank.yourdomain.com)
cloudflared tunnel route dns vext-open-bank api.bank.yourdomain.com
```

### Step 3: Configure Environment
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
chmod 600 .env
```
Fill in your gateway keys in `.env`, including the tunnel token `CLOUDFLARE_TUNNEL_TOKEN`.

### Step 4: Launch via Docker Compose
```bash
cd cloudflare
docker compose up -d --build
```

### Step 5: Validate Deployment
```bash
# Health check via Cloudflare edge domain
curl -I https://api.bank.yourdomain.com/api/v1/auth/status

# Verify Berlin Group Open Banking endpoint
curl -s https://api.bank.yourdomain.com/api/v1/automation/open-banking/account | jq .
```
