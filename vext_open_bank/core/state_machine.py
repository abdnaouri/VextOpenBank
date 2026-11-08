from enum import Enum
from typing import Dict, Set

class InvoiceState(str, Enum):
    INSTRUCTIONS_ISSUED = "INSTRUCTIONS_ISSUED"
    AWAITING_TRANSFER = "AWAITING_TRANSFER"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    CONFIRMED = "CONFIRMED"
    ALREADY_RECONCILED = "ALREADY_RECONCILED"
    REJECTED_UNDERPAYMENT = "REJECTED_UNDERPAYMENT"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

API_STATUS: Dict[InvoiceState, str] = {
    InvoiceState.INSTRUCTIONS_ISSUED: "PENDING_CONFIRMATION",
    InvoiceState.AWAITING_TRANSFER: "PENDING_CONFIRMATION",
    InvoiceState.PARTIALLY_PAID: "REJECTED_UNDERPAYMENT",
    InvoiceState.REJECTED_UNDERPAYMENT: "REJECTED_UNDERPAYMENT",
    InvoiceState.CONFIRMED: "CONFIRMED",
    InvoiceState.ALREADY_RECONCILED: "ALREADY_RECONCILED",
    InvoiceState.CANCELLED: "REJECTED_UNDERPAYMENT",
    InvoiceState.EXPIRED: "REJECTED_UNDERPAYMENT",
}

_LEGAL: Dict[InvoiceState, Set[InvoiceState]] = {
    InvoiceState.INSTRUCTIONS_ISSUED: {
        InvoiceState.AWAITING_TRANSFER,
        InvoiceState.CANCELLED,
        InvoiceState.EXPIRED,
    },
    InvoiceState.AWAITING_TRANSFER: {
        InvoiceState.PARTIALLY_PAID,
        InvoiceState.CONFIRMED,
        InvoiceState.CANCELLED,
        InvoiceState.EXPIRED,
    },
    InvoiceState.PARTIALLY_PAID: {
        InvoiceState.CONFIRMED,
        InvoiceState.PARTIALLY_PAID,
        InvoiceState.CANCELLED,
        InvoiceState.EXPIRED,
    },
    InvoiceState.REJECTED_UNDERPAYMENT: {
        InvoiceState.CONFIRMED,
        InvoiceState.CANCELLED,
    },
    InvoiceState.CONFIRMED: {
        InvoiceState.ALREADY_RECONCILED,
    },
    InvoiceState.ALREADY_RECONCILED: set(),
    InvoiceState.CANCELLED: set(),
    InvoiceState.EXPIRED: set(),
}

class IllegalTransition(Exception):
    """Raised when an illegal transition is requested on an Invoice state machine."""
    pass

def transition(current: InvoiceState, nxt: InvoiceState) -> None:
    if nxt not in _LEGAL.get(current, set()):
        raise IllegalTransition(f"Illegal invoice transition: {current.value} → {nxt.value}")
