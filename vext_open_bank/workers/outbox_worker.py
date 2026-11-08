import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
import httpx

from ..db.session import get_connection
from ..core.tenants import SEED_TENANTS

logger = logging.getLogger("OutboxWorker")

BACKOFF = [1, 5, 15, 60, 300, 900, 3600, 7200, 21600, 43200]
MAX_ATTEMPTS = len(BACKOFF)

def _sign_webhook(secret: str, body: bytes, ts: int) -> str:
    message = f"{ts}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

def dispatch_pending_events_sync(limit: int = 50) -> int:
    """Synchronous worker pass to deliver pending outbox webhooks."""
    now_ts = int(time.time())
    now_str = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM outbox_events
            WHERE delivered_at IS NULL
              AND next_attempt_at <= ?
              AND attempts < ?
            ORDER BY next_attempt_at ASC
            LIMIT ?
        """, (now_ts, MAX_ATTEMPTS, limit)).fetchall()

        delivered_count = 0
        with httpx.Client(timeout=10.0) as client:
            for row in rows:
                tenant = SEED_TENANTS.get(row["tenant_id"])
                if not tenant or not tenant.webhook_url:
                    with conn:
                        conn.execute("""
                            UPDATE outbox_events
                            SET delivered_at = ?, last_error = 'NO_WEBHOOK_URL_CONFIGURED'
                            WHERE id = ?
                        """, (now_ts, row["id"]))
                    continue

                payload_str = row["payload_json"]
                payload_bytes = payload_str.encode("utf-8")
                sig = _sign_webhook(tenant.webhook_secret, payload_bytes, now_ts)
                headers = {
                    "Content-Type": "application/json",
                    "X-Vext-Event-Id": row["id"],
                    "X-Vext-Event-Type": row["event_type"],
                    "X-Vext-Signature": f"t={now_ts},v1={sig}",
                    "X-Vext-Attempt": str(row["attempts"] + 1),
                }

                try:
                    resp = client.post(tenant.webhook_url, content=payload_bytes, headers=headers)
                    if 200 <= resp.status_code < 300:
                        with conn:
                            conn.execute("""
                                UPDATE outbox_events
                                SET delivered_at = ?, last_error = NULL
                                WHERE id = ?
                            """, (now_ts, row["id"]))
                        delivered_count += 1
                    else:
                        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                except Exception as exc:
                    attempts = row["attempts"] + 1
                    backoff_secs = BACKOFF[attempts - 1] if attempts <= len(BACKOFF) else 86400
                    next_attempt = now_ts + backoff_secs
                    with conn:
                        conn.execute("""
                            UPDATE outbox_events
                            SET attempts = ?, next_attempt_at = ?, last_error = ?
                            WHERE id = ?
                        """, (attempts, next_attempt, str(exc)[:500], row["id"]))
        return delivered_count
    finally:
        conn.close()

async def run_outbox_worker(interval_seconds: int = 15) -> None:
    """Async background worker invoked in FastAPI lifespan."""
    while True:
        try:
            dispatch_pending_events_sync()
        except Exception as exc:
            logger.debug("Outbox worker loop error: %s", exc)
        await asyncio.sleep(interval_seconds)
