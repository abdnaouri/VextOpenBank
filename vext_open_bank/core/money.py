# vext_open_bank/core/money.py
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_HALF_EVEN
import re
from typing import Final

MAD_DECIMALS: Final[Decimal] = Decimal("100")
CENT: Final[Decimal] = Decimal("0.01")
MAX_CENTIMES: Final[int] = 100_000_000_000  # 1B MAD (10,000,000.00 MAD)

RIB_CLEAN_RE: Final[re.Pattern] = re.compile(r"[\s\-]+")
MOTIF_EXTRACT_RE: Final[re.Pattern] = re.compile(
    r"\b(ENV|SOU|BC|CHK)[\s\-_]*([0-9A-Z]{3,16})\b", re.IGNORECASE
)

TENANT_PREFIX_MAP: Final[dict[str, str]] = {
    "ENV": "envoisms",
    "SOU": "soumit",
    "BC": "soumit",
    "CHK": "charikat",
}


class MoneyError(ValueError):
    """Raised when an invalid monetary amount is parsed."""
    pass


def mad_to_centimes(amount: str | int | Decimal) -> int:
    """Safely converts MAD input to integer centimes without IEEE-754 float drift."""
    if isinstance(amount, float):
        raise ValueError("Float inputs are forbidden; pass str, int, or Decimal.")
    try:
        dec = Decimal(str(amount).strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary value: {amount}") from exc
    if dec <= 0:
        raise ValueError("Amount must be strictly positive.")
    centimes = (dec * MAD_DECIMALS).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(centimes)


def centimes_to_mad_str(centimes: int) -> str:
    """Formats integer centimes as a canonical 2-decimal MAD string (e.g., '1250.50')."""
    return f"{centimes // 100}.{centimes % 100:02d}"


def validate_moroccan_rib(rib: str) -> str:
    """
    Validates a 24-digit Moroccan RIB via Bank Al-Maghrib Modulo-97 rule.
    Formula: Replacing the last 2 check digits with '00', remainder mod 97 subtracted from 97 must equal the key.
    """
    cleaned = RIB_CLEAN_RE.sub("", str(rib))
    if len(cleaned) != 24 or not cleaned.isdigit():
        raise ValueError("Moroccan RIB must be exactly 24 numeric digits.")

    base_22 = int(cleaned[:22])
    expected_key = 97 - ((base_22 * 100) % 97)
    actual_key = int(cleaned[22:])
    if expected_key != actual_key:
        raise ValueError(f"Invalid RIB Modulo-97 checksum (expected {expected_key:02d}, got {actual_key:02d}).")
    return cleaned


def extract_canonical_motif(raw_label: str) -> tuple[str, str] | None:
    """
    Extracts canonical (motif, tenant_id) from noisy Moroccan bank wire labels.
    Example: 'VIR INST BAM ENV 12345 STE ALPHA' -> ('ENV-12345', 'envoisms')
    """
    match = MOTIF_EXTRACT_RE.search(raw_label or "")
    if not match:
        return None
    prefix = match.group(1).upper()
    code = match.group(2).upper()
    return f"{prefix}-{code}", TENANT_PREFIX_MAP[prefix]


# --- Backward Compatibility Helpers for Existing Routes & Tests ---
def to_centimes(v: Decimal | int | float | str) -> int:
    """
    Deterministically convert an amount to BIGINT integer centimes.
    Backward-compatible with legacy callers accepting float, int, str, Decimal.
    Strictly forbids fractional centimes (more than 2 decimal places).
    """
    if isinstance(v, float):
        v = Decimal(str(v))
    elif isinstance(v, int):
        v = Decimal(v)
    elif isinstance(v, str):
        v = Decimal(v.strip().replace(",", "."))

    try:
        q = v.quantize(CENT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as e:
        raise MoneyError(f"Bad amount format: {v!r}") from e

    if q != v:
        raise MoneyError(f"Amount has more than 2 decimal places: {v}")
    if v < 0:
        raise MoneyError("Negative amounts are not permitted")

    c = int(q * 100)
    if c > MAX_CENTIMES:
        raise MoneyError("Amount exceeds maximum permitted limit")
    return c


def from_centimes(c: int) -> Decimal:
    """Convert integer centimes back to Decimal MAD."""
    return (Decimal(c) / Decimal(100)).quantize(CENT)
