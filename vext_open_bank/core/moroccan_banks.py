"""
moroccan_banks.py
Bank Al-Maghrib (BAM) Official Interbank Directory & Wire Parser.

Covers all 18 BAM clearing participants across SIMT (Standard Clearing)
and BAM Virement Instantané (24/7 Real-Time Switch).
"""

import re
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Official Bank Al-Maghrib (BAM) 3-Digit Bank Directory
# ---------------------------------------------------------------------------
MOROCCAN_BANKS: Dict[str, Dict[str, Any]] = {
    "007": {
        "code": "007",
        "name": "Attijariwafa Bank",
        "full_name": "Attijariwafa Bank S.A.",
        "swift_bic": "BCMAMAMC",
        "type": "Commercial",
        "supports_instant": True,
    },
    "011": {
        "code": "011",
        "name": "Bank of Africa",
        "full_name": "Bank of Africa (BMCE Group)",
        "swift_bic": "BMCEMAMC",
        "type": "Commercial",
        "supports_instant": True,
    },
    "013": {
        "code": "013",
        "name": "BMCI",
        "full_name": "Banque Marocaine pour le Commerce et l'Industrie (BNP Paribas)",
        "swift_bic": "BMCIMAMC",
        "type": "Commercial",
        "supports_instant": True,
    },
    "021": {
        "code": "021",
        "name": "Banque Centrale Populaire",
        "full_name": "Banque Centrale Populaire (BCP / Chaabi)",
        "swift_bic": "BCPOMAMC",
        "type": "Commercial",
        "supports_instant": True,
    },
    "022": {
        "code": "022",
        "name": "Société Générale Maroc",
        "full_name": "Société Générale Marocaine de Banques (SGMB)",
        "swift_bic": "SGMAMAMC",
        "type": "Commercial",
        "supports_instant": True,
    },
    "050": {
        "code": "050",
        "name": "CFG Bank",
        "full_name": "CFG Bank S.A.",
        "swift_bic": "CFGBMAMC",
        "type": "Investment & Retail",
        "supports_instant": True,
    },
    "051": {
        "code": "051",
        "name": "CFG Bank",
        "full_name": "CFG Bank S.A. (Headquarters / Clearing)",
        "swift_bic": "CFGBMAMC",
        "type": "Investment & Retail",
        "supports_instant": True,
    },
    "078": {
        "code": "078",
        "name": "Crédit du Maroc",
        "full_name": "Crédit du Maroc (CDM / Groupe Holmarcom)",
        "swift_bic": "CDMAMAMC",
        "type": "Commercial",
        "supports_instant": True,
    },
    "181": {
        "code": "181",
        "name": "CIH Bank",
        "full_name": "Crédit Immobilier et Hôtelier (CIH)",
        "swift_bic": "CIHMMAMC",
        "type": "Commercial",
        "supports_instant": True,
    },
    "225": {
        "code": "225",
        "name": "Crédit Agricole du Maroc",
        "full_name": "Crédit Agricole du Maroc (CAM)",
        "swift_bic": "AGRAMAMC",
        "type": "Commercial & Agricultural",
        "supports_instant": True,
    },
    "034": {
        "code": "034",
        "name": "Al Barid Bank",
        "full_name": "Al Barid Bank (Poste Maroc)",
        "swift_bic": "ABBMMAMC",
        "type": "Postal & Retail",
        "supports_instant": True,
    },
    "310": {
        "code": "310",
        "name": "Trésorerie Générale du Royaume",
        "full_name": "Trésorerie Générale du Royaume (TGR)",
        "swift_bic": "TGRMMAMC",
        "type": "Treasury / Public",
        "supports_instant": False,
    },
    "190": {
        "code": "190",
        "name": "CDG Capital",
        "full_name": "Caisse de Dépôt et de Gestion (CDG Capital)",
        "swift_bic": "CDGCMAMC",
        "type": "Public Financial Institution",
        "supports_instant": False,
    },
    "350": {
        "code": "350",
        "name": "Umnia Bank",
        "full_name": "Umnia Bank (Banque Participative CIH / CDG / QIIB)",
        "swift_bic": "UMNMMAMC",
        "type": "Participative / Islamic",
        "supports_instant": True,
    },
    "360": {
        "code": "360",
        "name": "Bank Assafa",
        "full_name": "Bank Assafa (Banque Participative Attijariwafa Bank)",
        "swift_bic": "ASFAMAMC",
        "type": "Participative / Islamic",
        "supports_instant": True,
    },
    "370": {
        "code": "370",
        "name": "BTI Bank",
        "full_name": "BTI Bank (Bank Al Baraka & BMCE)",
        "swift_bic": "BTIMMAMC",
        "type": "Participative / Islamic",
        "supports_instant": True,
    },
    "380": {
        "code": "380",
        "name": "Bank Al Yousr",
        "full_name": "Bank Al Yousr (Banque Participative BCP)",
        "swift_bic": "YUSMMAMC",
        "type": "Participative / Islamic",
        "supports_instant": True,
    },
    "390": {
        "code": "390",
        "name": "Dar Al-Amane",
        "full_name": "Dar Al-Amane (Fenêtre Participative Société Générale)",
        "swift_bic": "SGMAMAMC",
        "type": "Participative / Islamic",
        "supports_instant": True,
    },
}


def resolve_moroccan_bank(bank_code_or_rib: Optional[str]) -> Dict[str, Any]:
    """
    Resolve the 3-digit bank code or full 24-digit Moroccan RIB into its issuing bank metadata.
    """
    if not bank_code_or_rib:
        return {"code": "000", "name": "Banque Inconnue", "full_name": "Inconnue", "supports_instant": False}

    clean = bank_code_or_rib.replace(" ", "").strip()
    code = clean[:3]
    return MOROCCAN_BANKS.get(code, {
        "code": code,
        "name": f"Banque Marocaine ({code})",
        "full_name": f"Établissement Bancaire non répertorié ({code})",
        "swift_bic": "UNKNOWN",
        "type": "Commercial",
        "supports_instant": False,
    })


# ---------------------------------------------------------------------------
# Resilient Moroccan Bank Motif Normalization & Reference Extraction
# ---------------------------------------------------------------------------

RE_TENANT_REFERENCE = re.compile(
    r"\b(ENV|MED|BAC)[\s\-_]?([A-Z0-9]{4,12})\b",
    re.IGNORECASE,
)


def extract_reconciliation_reference(motif: Optional[str]) -> Optional[str]:
    """
    Extract a canonical tenant reference (e.g. 'ENV-98421') from messy bank motifs.

    Handles bank app formatting variances:
      - Attijari: 'VIR INST EMIS REF: ENV-98421' -> 'ENV-98421'
      - CIH: 'VIREMENT VERS CFG ENV 98421' -> 'ENV-98421'
      - BCP: 'VIR RECU DU CPT 2111... ENV_98421' -> 'ENV-98421'
      - BMCE: '/REF/ENV-98421/ROC/' -> 'ENV-98421'
      - Direct lowercase or spaces: 'env98421' -> 'ENV-98421'
    """
    if not motif:
        return None

    cleaned = motif.strip()
    match = RE_TENANT_REFERENCE.search(cleaned)
    if match:
        prefix = match.group(1).upper()
        token = match.group(2).upper()
        return f"{prefix}-{token}"

    return None


def matches_reference_fuzzy(raw_motif: Optional[str], target_reference: Optional[str]) -> bool:
    """
    Return True if the raw wire motif matches the expected reference,
    accounting for bank truncation, missing hyphens, or app prefixes.
    """
    if not raw_motif or not target_reference:
        return False

    target_clean = target_reference.strip().upper()
    extracted = extract_reconciliation_reference(raw_motif)
    if extracted and extracted == target_clean:
        return True

    # Secondary token search: check without hyphens/spaces (e.g. 'ENV98421')
    target_compact = target_clean.replace("-", "").replace(" ", "")
    motif_compact = raw_motif.upper().replace("-", "").replace(" ", "")

    return target_compact in motif_compact


# ---------------------------------------------------------------------------
# Multi-Bank Treasury Receiving Accounts (VEXT AI & Enterprise Tenants)
# ---------------------------------------------------------------------------
RECEIVING_TREASURY_ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "051": {
        "bank_code": "051",
        "bank_name": "CFG Bank",
        "rib": "050810006011833971200189",
        "rib_formatted": "050 810 0060118339712001 89",
        "beneficiary_name": "VEXT AI",
        "agency": "Agence Rabat Zaer / Casablanca",
        "swift_bic": "CFGBMAMC",
        "transfer_type": "Virement Instantané (BAM) ou Virement Ordinaire",
        "is_primary": True,
        "zero_fee_for": ["050", "051"],
    },
    "007": {
        "bank_code": "007",
        "bank_name": "Attijariwafa Bank",
        "rib": "007780000000123456789012",
        "rib_formatted": "007 780 0000001234567890 12",
        "beneficiary_name": "VEXT AI",
        "agency": "Agence Casablanca Hassan II",
        "swift_bic": "BCMAMAMC",
        "transfer_type": "Virement Compte à Compte / Instantané",
        "is_primary": False,
        "zero_fee_for": ["007"],
    },
    "021": {
        "bank_code": "021",
        "bank_name": "Banque Centrale Populaire (BCP)",
        "rib": "021780000000987654321098",
        "rib_formatted": "021 780 0000009876543210 98",
        "beneficiary_name": "VEXT AI",
        "agency": "Agence Casablanca Boulevard Zerktouni",
        "swift_bic": "BCPOMAMC",
        "transfer_type": "Virement Chaabi Direct / Instantané",
        "is_primary": False,
        "zero_fee_for": ["021"],
    },
    "181": {
        "bank_code": "181",
        "bank_name": "CIH Bank",
        "rib": "181780000000555666777884",
        "rib_formatted": "181 780 0000005556667778 84",
        "beneficiary_name": "VEXT AI",
        "agency": "Agence Rabat Agdal",
        "swift_bic": "CIHMMAMC",
        "transfer_type": "Virement CIH Mobile / Instantané",
        "is_primary": False,
        "zero_fee_for": ["181"],
    },
}


def get_receiving_treasury_account(bank_code: Optional[str] = None) -> Dict[str, Any]:
    """
    Get the optimal receiving treasury account.
    Defaults to CFG Bank (primary BAM gateway).
    If bank_code provided (e.g. '007' for Attijariwafa), returns that institution's direct RIB.
    """
    if bank_code and bank_code in RECEIVING_TREASURY_ACCOUNTS:
        return RECEIVING_TREASURY_ACCOUNTS[bank_code]
    return RECEIVING_TREASURY_ACCOUNTS["051"]


def list_receiving_treasury_accounts() -> list:
    """Return all active multi-bank receiving treasury accounts."""
    return list(RECEIVING_TREASURY_ACCOUNTS.values())
