import os
import hashlib
from typing import Optional, Dict
from pydantic import BaseModel

class TenantConfig(BaseModel):
    id: str
    name: str
    prefix: str
    webhook_url: Optional[str] = None
    webhook_secret: str
    api_key_hash: str
    default_amount_mad: Optional[str] = None
    underpayment_tolerance_centimes: int = 5  # 0.05 MAD default
    receiving_account_id: Optional[str] = "cfg_main"
    active: bool = True
    allow_simulation: bool = True

_DEFAULT_SECRET = os.getenv("VEXT_WEBHOOK_SECRET", "vext_unified_secret_key_2026")
_SOUMIT_SECRET = os.getenv("SOUMIT_VEXT_SECRET", _DEFAULT_SECRET)
_ENVOISMS_SECRET = os.getenv("ENVOISMS_VEXT_SECRET", os.getenv("ENVOISMS_SECRET", _DEFAULT_SECRET))
_CHARIKAT_SECRET = os.getenv("CHARIKAT_VEXT_SECRET", _DEFAULT_SECRET)

def _hash_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

# Bootstrap seed tenants (also used for zero-latency in-memory lookup)
SEED_TENANTS: Dict[str, TenantConfig] = {
    "envoisms": TenantConfig(
        id="envoisms",
        name="EnvoiSMS.ma",
        prefix="ENV",
        webhook_url=os.getenv("ENVOISMS_API_BASE", "https://api.envoisms.ma") + "/internal/vext/bank-transfer/webhook",
        webhook_secret=_ENVOISMS_SECRET,
        api_key_hash=_hash_key(os.getenv("ENVOISMS_API_KEY", "vext_envoisms_key_live")),
        default_amount_mad="275.00",
        underpayment_tolerance_centimes=5,
        receiving_account_id="cfg_main",
    ),
    "soumit": TenantConfig(
        id="soumit",
        name="Soumit.ma",
        prefix="SOU",
        webhook_url=os.getenv("SOUMIT_API_BASE", "https://soumit.ma") + "/api/webhooks/vextopenbank",
        webhook_secret=_SOUMIT_SECRET,
        api_key_hash=_hash_key(os.getenv("SOUMIT_API_KEY", "vext_soumit_key_live")),
        default_amount_mad="14256.00",
        underpayment_tolerance_centimes=5,
        receiving_account_id="cfg_main",
    ),
    "charikat": TenantConfig(
        id="charikat",
        name="Charikat.ma",
        prefix="CHK",
        webhook_url=os.getenv("CHARIKAT_API_BASE", "https://charikat.ma") + "/api/webhooks/vextopenbank",
        webhook_secret=_CHARIKAT_SECRET,
        api_key_hash=_hash_key(os.getenv("CHARIKAT_API_KEY", "vext_charikat_key_live")),
        default_amount_mad="2500.00",
        underpayment_tolerance_centimes=5,
        receiving_account_id="cfg_main",
    ),
}

PREFIX_TO_TENANT: Dict[str, str] = {
    "ENV": "envoisms",
    "SOU": "soumit",
    "BC": "soumit",
    "CHK": "charikat",
    "CHARIKAT": "charikat",
}

def resolve_tenant_by_prefix(prefix_or_ref: str) -> TenantConfig:
    """Resolve tenant configuration by reference prefix; fall back to parent Charikat.ma."""
    ref_norm = (prefix_or_ref or "").upper().strip()
    prefix = ref_norm.split("-")[0] if "-" in ref_norm else ref_norm
    tenant_id = PREFIX_TO_TENANT.get(prefix, "charikat")
    return SEED_TENANTS.get(tenant_id, SEED_TENANTS["charikat"])
