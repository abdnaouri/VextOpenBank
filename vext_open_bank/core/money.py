from decimal import Decimal, ROUND_HALF_EVEN, InvalidOperation

CENT = Decimal("0.01")
MAX_CENTIMES = 100_000_000_000  # 1B MAD (10,000,000.00 MAD)

class MoneyError(ValueError):
    """Raised when an invalid monetary amount is parsed."""
    pass

def to_centimes(v: Decimal | int | float | str) -> int:
    """
    Deterministically convert an amount to BIGINT integer centimes.
    Strictly forbids fractional centimes (more than 2 decimal places).
    """
    if isinstance(v, float):
        v = Decimal(str(v))
    elif isinstance(v, int):
        v = Decimal(v)
    elif isinstance(v, str):
        v = Decimal(v)

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
