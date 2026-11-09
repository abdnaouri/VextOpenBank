# vext_open_bank/core/rib.py
import re
from vext_open_bank.core.money import validate_moroccan_rib

RIB_RE = re.compile(r"^\d{24}$")


def validate_rib(raw: str) -> str:
    """Normalize and validate a 24-digit Moroccan RIB (length & digit check)."""
    digits = re.sub(r"\s+", "", str(raw))
    if not RIB_RE.match(digits):
        raise ValueError(f"RIB must be exactly 24 digits, got {digits!r}")
    return digits


def format_rib(raw: str) -> str:
    """Format 24-digit Moroccan RIB into standard human-readable groups."""
    d = validate_rib(raw)
    return f"{d[0:3]} {d[3:6]} {d[6:22]} {d[22:24]}"


__all__ = ["validate_rib", "format_rib", "validate_moroccan_rib"]
