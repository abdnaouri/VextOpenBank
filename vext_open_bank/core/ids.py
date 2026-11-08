import base64
import hashlib
import secrets

def new_event_id() -> str:
    """Generate collision-resistant event id."""
    return "evt_" + base64.b32encode(secrets.token_bytes(12)).decode().rstrip("=").lower()

def idem_hash(method: str, path: str, body: bytes) -> str:
    """Compute deterministic cryptographic hash of an idempotent HTTP request."""
    return hashlib.sha256(method.encode() + b"\x00" + path.encode() + b"\x00" + body).hexdigest()
