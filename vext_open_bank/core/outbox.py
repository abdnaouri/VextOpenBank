# vext_open_bank/core/outbox.py
import asyncio
import hashlib
import hmac
import logging
import random
import time
from typing import Any
import httpx
from vext_open_bank.core.db import Database

logger = logging.getLogger("vext.outbox")

MAX_ATTEMPTS = 8
LEASE_SECONDS = 60


def build_hmac_signature(secret: str, timestamp: int, raw_body: str) -> str:
    signed_payload = f"{timestamp}.{raw_body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def compute_backoff_seconds(attempts: int) -> int:
    """Exponential backoff with jitter: ~5s, 20s, 80s, 5m, 20m, 1.3h, 5.4h, 21h."""
    base = min(86400, 5 * (4 ** max(0, attempts - 1)))
    return int(base + random.uniform(0, min(30, base * 0.2)))


class OutboxWorker:
    def __init__(self, db: Database, tenant_secrets: dict[str, str], default_secret: str) -> None:
        self.db = db
        self.tenant_secrets = tenant_secrets
        self.default_secret = default_secret
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()

    def trigger_now(self) -> None:
        """Wakes the worker immediately after `/simulate-transfer` so delivery latency is <50ms."""
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def _claim_batch(self, batch_size: int = 15) -> list[dict[str, Any]]:
        now = int(time.time())
        lease_until = now + LEASE_SECONDS
        with self.db.write_tx() as conn:
            rows = conn.execute(
                """
                SELECT event_id, tenant, motif, webhook_url, payload_json, attempts
                FROM outbox_events
                WHERE status = 'PENDING'
                  AND next_attempt_at <= ?
                  AND locked_until <= ?
                ORDER BY next_attempt_at ASC
                LIMIT ?
                """,
                (now, now, batch_size),
            ).fetchall()

            claimed = [dict(r) for r in rows]
            if claimed:
                conn.executemany(
                    "UPDATE outbox_events SET locked_until = ? WHERE event_id = ?",
                    [(lease_until, r["event_id"]) for r in claimed],
                )
            return claimed

    async def _deliver_one(self, client: httpx.AsyncClient, event: dict[str, Any]) -> None:
        now = int(time.time())
        secret = self.tenant_secrets.get(event["tenant"], self.default_secret)
        body = event["payload_json"]
        signature = build_hmac_signature(secret, now, body)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "VEXT-OpenBank-Bridge/1.0",
            "X-Vext-Event-Id": event["event_id"],
            "X-Vext-Tenant": event["tenant"],
            "X-Vext-Signature": signature,
        }

        try:
            resp = await client.post(event["webhook_url"], content=body, headers=headers)
            if 200 <= resp.status_code < 300:
                with self.db.write_tx() as conn:
                    conn.execute(
                        """
                        UPDATE outbox_events
                        SET status = 'DELIVERED', delivered_at = ?, locked_until = 0, last_error = NULL
                        WHERE event_id = ?
                        """,
                        (int(time.time()), event["event_id"]),
                    )
                return
            err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {str(exc)[:200]}"

        attempts = int(event["attempts"]) + 1
        new_status = "DEAD" if attempts >= MAX_ATTEMPTS else "PENDING"
        next_at = int(time.time()) + compute_backoff_seconds(attempts)

        with self.db.write_tx() as conn:
            conn.execute(
                """
                UPDATE outbox_events
                SET status = ?, attempts = ?, next_attempt_at = ?, locked_until = 0, last_error = ?
                WHERE event_id = ?
                """,
                (new_status, attempts, next_at, err_msg, event["event_id"]),
            )

    async def run_forever(self) -> None:
        last_janitor = 0.0
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        async with httpx.AsyncClient(timeout=10.0, limits=limits, follow_redirects=False) as client:
            while not self._stop_event.is_set():
                try:
                    batch = await asyncio.to_thread(self._claim_batch, 15)
                    if batch:
                        await asyncio.gather(*(self._deliver_one(client, ev) for ev in batch))
                        continue

                    # Run autonomous SQLite maintenance every 300 seconds
                    now_mono = time.monotonic()
                    if now_mono - last_janitor > 300:
                        await asyncio.to_thread(self.db.run_maintenance)
                        last_janitor = now_mono

                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=2.5)
                    except asyncio.TimeoutError:
                        pass
                except Exception as exc:
                    logger.error("Outbox loop error (self-recovering): %s", exc)
                    await asyncio.sleep(3.0)
