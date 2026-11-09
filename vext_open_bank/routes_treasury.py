# vext_open_bank/routes_treasury.py
from fastapi import APIRouter, HTTPException, Query
from vext_open_bank.core.db import Database
from vext_open_bank.core.money import centimes_to_mad_str


def build_treasury_router(
    db: Database,
    *,
    rib_formatted: str,
    rib_clean: str,
    beneficiary: str,
    client_code: str,
    user_id: str,
) -> APIRouter:
    router = APIRouter(prefix="/treasury", tags=["Corporate Treasury & Relevé Bancaire"])

    @router.get("/account")
    async def get_corporate_account_summary():
        with db.read_conn() as conn:
            latest = conn.execute(
                "SELECT solde_apres_centimes, created_at FROM treasury_ledger ORDER BY entry_seq DESC LIMIT 1"
            ).fetchone()
            pending = conn.execute(
                "SELECT COALESCE(SUM(expected_centimes - received_centimes), 0) AS pending_c "
                "FROM invoices WHERE status IN ('AWAITING_TRANSFER', 'UNDERPAID')"
            ).fetchone()
            counts = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM invoices GROUP BY status"
            ).fetchall()

        solde_centimes = int(latest["solde_apres_centimes"]) if latest else 0
        pending_centimes = int(pending["pending_c"]) if pending else 0
        status_map = {r["status"]: r["cnt"] for r in counts}

        return {
            "banque": "CFG Bank",
            "code_banque": "050",
            "code_ville": "810",
            "titulaire": beneficiary,
            "code_client": client_code,
            "identifiant_abonne": user_id,
            "rib_formate": rib_formatted,
            "rib_24": rib_clean,
            "devise": "MAD",
            "solde_comptable_centimes": solde_centimes,
            "solde_comptable_mad": centimes_to_mad_str(solde_centimes),
            "encours_attendu_centimes": pending_centimes,
            "encours_attendu_mad": centimes_to_mad_str(pending_centimes),
            "statistiques_factures": status_map,
        }

    @router.get("/releve")
    async def get_bank_statement(
        tenant: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ):
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

        ecritures = [
            {
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
        return {"compte_rib": rib_formatted, "devise": "MAD", "count": len(ecritures), "ecritures": ecritures}

    @router.get("/avis/{bank_tx_id}")
    async def get_avis_de_credit(bank_tx_id: str):
        with db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM treasury_ledger WHERE bank_tx_id = ? OR operation_ref = ? LIMIT 1",
                (bank_tx_id, bank_tx_id),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Avis d'opération introuvable.")

        return {
            "type_document": "AVIS DE CREDIT — VIREMENT INSTANTANE INTERBANCAIRE",
            "reference_operation": row["operation_ref"],
            "reference_interbancaire_bam": row["bank_tx_id"],
            "date_operation": row["date_operation"],
            "date_valeur": row["date_valeur"],
            "beneficiaire": {
                "raison_sociale": beneficiary,
                "banque": "CFG Bank (Code 050 - Casablanca 810)",
                "rib": rib_formatted,
                "code_client": client_code,
            },
            "donneur_ordre": {
                "nom": row["nom_donneur_ordre"],
                "rib": row["rib_donneur_ordre"],
            },
            "details_paiement": {
                "motif_rapprochement": row["motif"],
                "libelle_complet": row["libelle_bancaire"],
                "montant_mad": centimes_to_mad_str(row["montant_centimes"]),
                "devise": "MAD",
                "statut_compensation": "IRREVOCABLE_COMPENSE",
            },
        }

    return router
