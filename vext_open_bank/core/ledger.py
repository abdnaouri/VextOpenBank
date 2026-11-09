# vext_open_bank/core/ledger.py
from datetime import datetime, timezone
import time
from typing import Any
from vext_open_bank.core.db import Database
from vext_open_bank.core.money import centimes_to_mad_str

LEDGER_SCHEMA_EXTENSION = """
CREATE TABLE IF NOT EXISTS treasury_ledger (
    entry_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_ref TEXT NOT NULL UNIQUE,
    bank_tx_id TEXT NOT NULL UNIQUE,
    motif TEXT,
    tenant TEXT,
    sens TEXT NOT NULL CHECK (sens IN ('CREDIT', 'DEBIT')),
    canal TEXT NOT NULL DEFAULT 'VIREMENT_INSTANTANE_BAM',
    libelle_bancaire TEXT NOT NULL,
    nom_donneur_ordre TEXT NOT NULL,
    rib_donneur_ordre TEXT NOT NULL,
    montant_centimes INTEGER NOT NULL CHECK (montant_centimes > 0),
    solde_avant_centimes INTEGER NOT NULL,
    solde_apres_centimes INTEGER NOT NULL,
    date_operation TEXT NOT NULL,
    date_valeur TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_created ON treasury_ledger(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_motif ON treasury_ledger(motif);
"""


def init_ledger_schema(db: Database) -> None:
    with db.write_tx() as conn:
        conn.executescript(LEDGER_SCHEMA_EXTENSION)


def record_treasury_credit_in_tx(
    conn,
    *,
    bank_tx_id: str,
    motif: str | None,
    tenant: str | None,
    raw_label: str,
    sender_name: str,
    sender_rib: str,
    amount_centimes: int,
    canal: str = "VIREMENT_INSTANTANE_BAM",
) -> dict[str, Any]:
    """
    Executes inside `db.write_tx()`. Computes the exact running balance (`solde_apres_centimes`),
    formats canonical Moroccan interbank libellés, and inserts an immutable ledger row.
    """
    now_ts = int(time.time())
    dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    date_iso = dt_utc.strftime("%Y-%m-%d")
    date_compact = dt_utc.strftime("%Y%m%d")

    last_row = conn.execute(
        "SELECT entry_seq, solde_apres_centimes FROM treasury_ledger ORDER BY entry_seq DESC LIMIT 1"
    ).fetchone()

    solde_avant = int(last_row["solde_apres_centimes"]) if last_row else 0
    next_seq = (int(last_row["entry_seq"]) + 1) if last_row else 1
    solde_apres = solde_avant + amount_centimes

    operation_ref = f"CFG-{date_compact}-{next_seq:06d}"
    clean_sender = (sender_name or "DONNEUR D'ORDRE INTERBANCAIRE").upper()[:40]
    prefix_canal = "VIR INST RECU DE" if "INSTANTANE" in canal else "VIREMENT RECU DE"
    libelle_bancaire = f"{prefix_canal} {clean_sender} / MOTIF: {motif or raw_label[:24]}"

    conn.execute(
        """
        INSERT INTO treasury_ledger (
            operation_ref, bank_tx_id, motif, tenant, sens, canal,
            libelle_bancaire, nom_donneur_ordre, rib_donneur_ordre,
            montant_centimes, solde_avant_centimes, solde_apres_centimes,
            date_operation, date_valeur, created_at
        ) VALUES (?, ?, ?, ?, 'CREDIT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_ref,
            bank_tx_id,
            motif,
            tenant,
            canal,
            libelle_bancaire,
            clean_sender,
            sender_rib or "NON COMMUNIQUE",
            amount_centimes,
            solde_avant,
            solde_apres,
            date_iso,
            date_iso,
            now_ts,
        ),
    )

    return {
        "operation_ref": operation_ref,
        "bank_tx_id": bank_tx_id,
        "sens": "CREDIT",
        "canal": canal,
        "libelle_bancaire": libelle_bancaire,
        "nom_donneur_ordre": clean_sender,
        "rib_donneur_ordre": sender_rib or "NON COMMUNIQUE",
        "montant_centimes": amount_centimes,
        "montant_mad": centimes_to_mad_str(amount_centimes),
        "solde_avant_mad": centimes_to_mad_str(solde_avant),
        "solde_apres_mad": centimes_to_mad_str(solde_apres),
        "date_operation": date_iso,
        "date_valeur": date_iso,
        "created_at": now_ts,
    }


def record_treasury_debit_in_tx(
    conn,
    *,
    bank_tx_id: str,
    motif: str | None,
    tenant: str | None,
    raw_label: str,
    beneficiary_name: str,
    beneficiary_rib: str,
    amount_centimes: int,
    canal: str = "COMPENSATION_SIMT",
) -> dict[str, Any]:
    """
    Executes inside `db.write_tx()`. Computes debit running balance (`solde_apres_centimes`),
    formats canonical Moroccan interbank debit libellés, and inserts an immutable ledger row.
    """
    now_ts = int(time.time())
    dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    date_iso = dt_utc.strftime("%Y-%m-%d")
    date_compact = dt_utc.strftime("%Y%m%d")

    last_row = conn.execute(
        "SELECT entry_seq, solde_apres_centimes FROM treasury_ledger ORDER BY entry_seq DESC LIMIT 1"
    ).fetchone()

    solde_avant = int(last_row["solde_apres_centimes"]) if last_row else 0
    next_seq = (int(last_row["entry_seq"]) + 1) if last_row else 1
    solde_apres = solde_avant - amount_centimes

    operation_ref = f"CFG-{date_compact}-{next_seq:06d}"
    clean_beneficiary = (beneficiary_name or "BENEFICIAIRE INTERBANCAIRE").upper()[:40]
    libelle_bancaire = f"VIR EMIS VERS {clean_beneficiary} / MOTIF: {motif or raw_label[:24]}"

    conn.execute(
        """
        INSERT INTO treasury_ledger (
            operation_ref, bank_tx_id, motif, tenant, sens, canal,
            libelle_bancaire, nom_donneur_ordre, rib_donneur_ordre,
            montant_centimes, solde_avant_centimes, solde_apres_centimes,
            date_operation, date_valeur, created_at
        ) VALUES (?, ?, ?, ?, 'DEBIT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_ref,
            bank_tx_id,
            motif,
            tenant,
            canal,
            libelle_bancaire,
            clean_beneficiary,
            beneficiary_rib or "NON COMMUNIQUE",
            amount_centimes,
            solde_avant,
            solde_apres,
            date_iso,
            date_iso,
            now_ts,
        ),
    )

    return {
        "operation_ref": operation_ref,
        "bank_tx_id": bank_tx_id,
        "sens": "DEBIT",
        "canal": canal,
        "libelle_bancaire": libelle_bancaire,
        "nom_donneur_ordre": clean_beneficiary,
        "rib_donneur_ordre": beneficiary_rib or "NON COMMUNIQUE",
        "montant_centimes": amount_centimes,
        "montant_mad": centimes_to_mad_str(amount_centimes),
        "solde_avant_mad": centimes_to_mad_str(solde_avant),
        "solde_apres_mad": centimes_to_mad_str(solde_apres),
        "date_operation": date_iso,
        "date_valeur": date_iso,
        "created_at": now_ts,
    }


def get_account_releve(
    db: Database,
    *,
    tenant: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetches formatted accounting statements (Relevé Bancaire) from the immutable double-entry ledger."""
    with db.read_conn() as conn:
        if tenant:
            rows = conn.execute(
                "SELECT * FROM treasury_ledger WHERE tenant = ? ORDER BY entry_seq DESC LIMIT ?",
                (tenant, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM treasury_ledger ORDER BY entry_seq DESC LIMIT ?",
                (limit,),
            ).fetchall()

    return [
        {
            "entry_seq": r["entry_seq"],
            "operation_ref": r["operation_ref"],
            "bank_tx_id": r["bank_tx_id"],
            "date_operation": r["date_operation"],
            "date_valeur": r["date_valeur"],
            "libelle_bancaire": r["libelle_bancaire"],
            "motif": r["motif"],
            "tenant": r["tenant"],
            "sens": r["sens"],
            "canal": r["canal"],
            "nom_donneur_ordre": r["nom_donneur_ordre"],
            "rib_donneur_ordre": r["rib_donneur_ordre"],
            "montant_centimes": r["montant_centimes"],
            "montant_mad": centimes_to_mad_str(r["montant_centimes"]),
            "solde_avant_mad": centimes_to_mad_str(r["solde_avant_centimes"]),
            "solde_apres_mad": centimes_to_mad_str(r["solde_apres_centimes"]),
            "horodatage": r["created_at"],
        }
        for r in rows
    ]


def get_avis_operation(db: Database, bank_tx_id_or_op_ref: str) -> dict[str, Any] | None:
    """Retrieves structured Avis d'Opération record by interbank reference or internal operation reference."""
    with db.read_conn() as conn:
        row = conn.execute(
            "SELECT * FROM treasury_ledger WHERE bank_tx_id = ? OR operation_ref = ? LIMIT 1",
            (bank_tx_id_or_op_ref, bank_tx_id_or_op_ref),
        ).fetchone()
    if not row:
        return None
    return dict(row)

