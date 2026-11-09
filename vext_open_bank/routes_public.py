# vext_open_bank/routes_public.py
import html
import json
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from vext_open_bank.core.db import Database
from vext_open_bank.core.money import centimes_to_mad_str
from vext_open_bank.core.qr import generate_qr_data_uri


def build_public_renderer_router(
    db: Database,
    *,
    rib_formatted: str,
    rib_clean: str,
    beneficiary: str,
    client_code: str,
    user_id: str,
) -> APIRouter:
    router = APIRouter(tags=["Corporate Payment & Receipt Renderer"])

    @router.get("/p/inv/{motif}/status")
    async def get_public_invoice_status(motif: str):
        clean_motif = motif.strip().upper()
        with db.read_conn() as conn:
            inv = conn.execute(
                "SELECT motif, tenant, status, expected_centimes, received_centimes, confirmed_at FROM invoices WHERE motif = ?",
                (clean_motif,),
            ).fetchone()
            ledger = conn.execute(
                "SELECT operation_ref, bank_tx_id, date_operation FROM treasury_ledger WHERE motif = ? ORDER BY entry_seq DESC LIMIT 1",
                (clean_motif,),
            ).fetchone()
        if not inv:
            raise HTTPException(status_code=404, detail="Facture introuvable.")
        return {
            "motif": inv["motif"],
            "status": inv["status"],
            "expected_mad": centimes_to_mad_str(inv["expected_centimes"]),
            "received_mad": centimes_to_mad_str(inv["received_centimes"]),
            "confirmed_at": inv["confirmed_at"],
            "operation_ref": ledger["operation_ref"] if ledger else None,
            "bank_tx_id": ledger["bank_tx_id"] if ledger else None,
        }

    @router.get("/p/inv/{motif}/stream")
    async def stream_public_invoice_status(motif: str):
        """
        Server-Sent Events (SSE) stream for real-time confirmation.
        Pushes state changes immediately without aggressive client polling.
        """
        clean_motif = motif.strip().upper()

        async def event_generator():
            last_status = None
            for _ in range(120):  # Stream for up to ~2 minutes
                with db.read_conn() as conn:
                    inv = conn.execute(
                        "SELECT motif, tenant, status, expected_centimes, received_centimes, confirmed_at FROM invoices WHERE motif = ?",
                        (clean_motif,),
                    ).fetchone()
                    ledger = conn.execute(
                        "SELECT operation_ref, bank_tx_id, date_operation FROM treasury_ledger WHERE motif = ? ORDER BY entry_seq DESC LIMIT 1",
                        (clean_motif,),
                    ).fetchone()

                if not inv:
                    yield f"event: error\ndata: {json.dumps({'error': 'Invoice not found'})}\n\n"
                    break

                payload = {
                    "motif": inv["motif"],
                    "status": inv["status"],
                    "expected_mad": centimes_to_mad_str(inv["expected_centimes"]),
                    "received_mad": centimes_to_mad_str(inv["received_centimes"]),
                    "confirmed_at": inv["confirmed_at"],
                    "operation_ref": ledger["operation_ref"] if ledger else None,
                    "bank_tx_id": ledger["bank_tx_id"] if ledger else None,
                }

                current_status = inv["status"]
                if current_status != last_status:
                    last_status = current_status
                    yield f"event: status\ndata: {json.dumps(payload)}\n\n"

                if current_status in ("CONFIRMED", "OVERPAID"):
                    yield f"event: confirmed\ndata: {json.dumps(payload)}\n\n"
                    break

                await asyncio.sleep(1.0)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.get("/p/inv/{motif}", response_class=HTMLResponse)
    async def render_public_invoice_sheet(motif: str):
        clean_motif = motif.strip().upper()
        with db.read_conn() as conn:
            inv = conn.execute("SELECT * FROM invoices WHERE motif = ?", (clean_motif,)).fetchone()
        if not inv:
            raise HTTPException(status_code=404, detail="Ordre de virement introuvable.")

        amount_mad = centimes_to_mad_str(inv["expected_centimes"])
        qr_uri = generate_qr_data_uri(rib_clean, beneficiary, inv["expected_centimes"], clean_motif)
        safe_motif = html.escape(clean_motif)
        safe_tenant = html.escape(inv["tenant"].upper())
        safe_status = html.escape(inv["status"])

        return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ordre de Virement Interbancaire — {safe_motif}</title>
<style>
  :root {{ --navy:#0f1f38; --crimson:#b81d33; --gold:#c59b27; --bg:#f4f6f9; --border:#dde2ea; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--navy); margin:0; padding:24px; }}
  .sheet {{ max-width:760px; margin:0 auto; background:#fff; border:1px solid var(--border); border-top:6px solid var(--crimson); border-radius:8px; padding:32px; box-shadow:0 8px 24px rgba(15,31,56,.06); }}
  .hdr {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid var(--border); padding-bottom:20px; margin-bottom:24px; }}
  .badge {{ padding:6px 12px; border-radius:4px; font-size:12px; font-weight:700; letter-spacing:.05em; }}
  .badge.wait {{ background:#fff8e6; color:#9a6700; border:1px solid #f0d078; }}
  .badge.ok {{ background:#e6f6ed; color:#0e6245; border:1px solid #8fd6b2; }}
  .rib-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; background:#f8fafc; border:1px solid var(--border); padding:16px; border-radius:6px; margin:16px 0; text-align:center; }}
  .rib-cell small {{ display:block; font-size:11px; color:#64748b; text-transform:uppercase; }}
  .rib-cell strong {{ font-family:monospace; font-size:18px; color:var(--navy); letter-spacing:1px; }}
  .motif-box {{ background:#fef2f3; border:1px dashed var(--crimson); padding:14px 18px; border-radius:6px; display:flex; justify-content:space-between; align-items:center; margin:18px 0; }}
  .qr-row {{ display:flex; gap:24px; align-items:center; margin-top:24px; padding-top:20px; border-top:1px solid var(--border); }}
  .qr-row img {{ width:136px; height:136px; border:1px solid var(--border); border-radius:6px; padding:6px; }}
  .actions-row {{ margin-top:18px; display:flex; justify-content:flex-end; gap:10px; }}
  .btn-print {{ background:var(--navy); color:#fff; border:none; padding:8px 16px; border-radius:4px; cursor:pointer; font-weight:600; font-size:13px; text-decoration:none; display:inline-block; }}
  @media print {{ body {{ background:#fff; padding:0; }} .sheet {{ box-shadow:none; border:1px solid #000; }} .no-print {{ display:none; }} }}
</style>
</head>
<body>
<div class="sheet">
  <div class="hdr">
    <div>
      <div style="font-size:12px;color:#64748b;font-weight:600;letter-spacing:.08em;">INSTRUCTION DE REGLEMENT INTERBANCAIRE (MAROC / BAM)</div>
      <h1 style="margin:6px 0 4px;font-size:22px;">{beneficiary} — Trésorerie Centrale ({safe_tenant})</h1>
      <div style="font-size:13px;color:#475569;">Code Client : <strong>{client_code}</strong> &bull; Identifiant : <strong>{user_id}</strong></div>
    </div>
    <span id="status-badge" class="badge {'ok' if safe_status in ('CONFIRMED', 'OVERPAID') else 'wait'}">{safe_status}</span>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:baseline;">
    <span style="font-size:14px;color:#475569;">Montant exact à virer (TTC) :</span>
    <span style="font-size:28px;font-weight:800;color:var(--navy);">{amount_mad} MAD</span>
  </div>

  <div class="rib-grid">
    <div class="rib-cell"><small>Code Banque (CFG)</small><strong>{rib_clean[:3]}</strong></div>
    <div class="rib-cell"><small>Code Ville</small><strong>{rib_clean[3:6]}</strong></div>
    <div class="rib-cell"><small>N° de Compte (16 chiffres)</small><strong>{rib_clean[6:22]}</strong></div>
    <div class="rib-cell"><small>Clé RIB</small><strong>{rib_clean[22:]}</strong></div>
  </div>

  <div class="motif-box">
    <div>
      <div style="font-size:11px;font-weight:700;color:var(--crimson);text-transform:uppercase;">Motif / Libellé obligatoire du virement (Rapprochement Instantané)</div>
      <div style="font-size:20px;font-weight:800;font-family:monospace;margin-top:2px;">{safe_motif}</div>
    </div>
    <div style="display:flex; gap:8px;">
      <button onclick="navigator.clipboard.writeText('{rib_formatted}')" style="background:#e2e8f0;color:var(--navy);border:none;padding:8px 12px;border-radius:4px;cursor:pointer;font-weight:600;font-size:12px;">Copier le RIB</button>
      <button onclick="navigator.clipboard.writeText('{safe_motif}')" style="background:var(--navy);color:#fff;border:none;padding:8px 14px;border-radius:4px;cursor:pointer;font-weight:600;font-size:12px;">Copier le Motif</button>
    </div>
  </div>

  <div class="qr-row">
    <img src="{qr_uri}" alt="EMVCo MarocPay QR">
    <div style="font-size:13px;line-height:1.55;color:#334155;">
      <strong>Paiement Mobile &amp; Virement Instantané BAM (20 secondes)</strong><br>
      Scannez ce QR code interbancaire EMVCo depuis votre application bancaire marocaine ou saisissez le RIB <strong>{rib_formatted}</strong> avec la référence stricte <strong>{safe_motif}</strong>.
      <div id="op-ref" style="margin-top:8px;font-family:monospace;color:#0e6245;font-weight:700;"></div>
      <div id="avis-link-container" style="margin-top:6px;"></div>
    </div>
  </div>

  <div class="actions-row no-print">
    <button onclick="window.print()" class="btn-print">Imprimer l'Ordre de Virement</button>
  </div>
</div>
<script>
  function applyConfirmed(d) {{
    const b = document.getElementById('status-badge');
    b.textContent = d.status;
    b.className = 'badge ok';
    if (d.operation_ref) {{
      document.getElementById('op-ref').textContent = '✓ COMPENSÉ — Réf: ' + d.operation_ref + ' (' + (d.bank_tx_id || '') + ')';
      if (d.bank_tx_id) {{
        document.getElementById('avis-link-container').innerHTML = '<a href="/p/avis/' + d.bank_tx_id + '" style="color:#0e6245; font-weight:700; text-decoration:underline;">Télécharger l\\'Avis d\\'Opération Officiel &rarr;</a>';
      }}
    }}
  }}

  // Setup Server-Sent Events with fallback to polling
  if (!!window.EventSource) {{
    const sse = new EventSource('/p/inv/{safe_motif}/stream');
    sse.addEventListener('status', function(e) {{
      try {{
        const d = JSON.parse(e.data);
        if (d.status === 'CONFIRMED' || d.status === 'OVERPAID') {{
          applyConfirmed(d);
          sse.close();
        }}
      }} catch(err) {{}}
    }});
    sse.addEventListener('confirmed', function(e) {{
      try {{
        const d = JSON.parse(e.data);
        applyConfirmed(d);
        sse.close();
      }} catch(err) {{}}
    }});
    sse.onerror = function() {{
      sse.close();
      poll();
    }};
  }} else {{
    poll();
  }}

  async function poll() {{
    try {{
      const r = await fetch('/p/inv/{safe_motif}/status');
      if (!r.ok) return;
      const d = await r.json();
      if (d.status === 'CONFIRMED' || d.status === 'OVERPAID') {{
        applyConfirmed(d);
        return;
      }}
    }} catch (e) {{}}
    setTimeout(poll, 3000);
  }}
</script>
</body>
</html>"""

    @router.get("/p/avis/{bank_tx_id}", response_class=HTMLResponse)
    async def render_public_avis_sheet(bank_tx_id: str):
        """
        Public French/Moroccan Avis d'Opération / Ordre de Virement print & download view.
        Indistinguishable from native CFG Bank / Central Treasury statements.
        """
        with db.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM treasury_ledger WHERE bank_tx_id = ? OR operation_ref = ? LIMIT 1",
                (bank_tx_id, bank_tx_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Avis d'opération introuvable.")

        op_ref = html.escape(str(row["operation_ref"]))
        bam_ref = html.escape(str(row["bank_tx_id"]))
        dt_op = html.escape(str(row["date_operation"]))
        dt_val = html.escape(str(row["date_valeur"]))
        sens = html.escape(str(row["sens"]))
        canal = html.escape(str(row["canal"]).replace("_", " "))
        libelle = html.escape(str(row["libelle_bancaire"]))
        donneur = html.escape(str(row["nom_donneur_ordre"]))
        donneur_rib = html.escape(str(row["rib_donneur_ordre"]))
        montant_mad = centimes_to_mad_str(row["montant_centimes"])
        solde_apres_mad = centimes_to_mad_str(row["solde_apres_centimes"])
        motif_escaped = html.escape(str(row["motif"] or "N/A"))
        tenant_name = html.escape(str(row["tenant"] or "Charikat.ma").upper())
        beneficiaire_nom = html.escape(beneficiary)

        return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Avis d'Opération Interbancaire — {op_ref}</title>
<style>
  :root {{ --navy:#0f1f38; --crimson:#b81d33; --border:#dde2ea; --bg:#f4f6f9; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--navy); margin:0; padding:24px; }}
  .sheet {{ max-width:760px; margin:0 auto; background:#fff; border:1px solid var(--border); border-top:6px solid var(--navy); border-radius:8px; padding:36px; box-shadow:0 8px 24px rgba(15,31,56,.06); }}
  .hdr {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:2px solid var(--navy); padding-bottom:18px; margin-bottom:24px; }}
  .meta-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:24px; font-size:13px; }}
  .meta-box {{ background:#f8fafc; border:1px solid var(--border); border-radius:6px; padding:14px; }}
  .meta-box h4 {{ margin:0 0 8px; font-size:12px; text-transform:uppercase; color:#64748b; letter-spacing:.05em; }}
  .table {{ width:100%; border-collapse:collapse; margin:20px 0; font-size:13px; }}
  .table th, .table td {{ padding:10px 12px; border:1px solid var(--border); text-align:left; }}
  .table th {{ background:#f8fafc; font-weight:600; color:#475569; }}
  .amount-box {{ background:#e6f6ed; border:1px solid #8fd6b2; border-radius:6px; padding:16px; margin:20px 0; display:flex; justify-content:space-between; align-items:center; }}
  .badge-confirmed {{ background:#0e6245; color:#fff; padding:4px 8px; border-radius:4px; font-size:11px; font-weight:700; }}
  .footer {{ font-size:11px; color:#64748b; margin-top:32px; padding-top:16px; border-top:1px solid var(--border); line-height:1.5; }}
  @media print {{ body {{ background:#fff; padding:0; }} .sheet {{ box-shadow:none; border:none; }} .no-print {{ display:none; }} }}
</style>
</head>
<body>
<div class="sheet">
  <div class="hdr">
    <div>
      <div style="font-size:11px; font-weight:700; color:var(--crimson); letter-spacing:.1em; text-transform:uppercase;">CFG BANK &bull; TRESORERIE CENTRALE CHARIKAT.MA</div>
      <h1 style="margin:4px 0 2px; font-size:22px;">AVIS D'OPÉRATION BANCAIRE</h1>
      <div style="font-size:13px; color:#64748b;">Réf. Comptable : <strong style="font-family:monospace; color:var(--navy);">{op_ref}</strong></div>
    </div>
    <div style="text-align:right;">
      <span class="badge-confirmed">COMPENSÉ &bull; IRREVOCABLE</span>
      <div style="font-size:12px; color:#64748b; margin-top:6px;">Date Opération : <strong>{dt_op}</strong></div>
      <div style="font-size:12px; color:#64748b;">Date Valeur : <strong>{dt_val}</strong></div>
    </div>
  </div>

  <div class="meta-grid">
    <div class="meta-box">
      <h4>Bénéficiaire (Crédité)</h4>
      <div style="font-weight:700; font-size:14px; margin-bottom:4px;">{beneficiaire_nom} ({tenant_name})</div>
      <div>CFG Bank &bull; Code Banque 050 &bull; Guichet 810</div>
      <div style="font-family:monospace; font-size:12px; margin-top:4px;">RIB : {rib_formatted}</div>
      <div style="font-size:12px; color:#64748b; margin-top:2px;">Code Client : {client_code} &bull; Id : {user_id}</div>
    </div>
    <div class="meta-box">
      <h4>Donneur d'Ordre (Débité)</h4>
      <div style="font-weight:700; font-size:14px; margin-bottom:4px;">{donneur}</div>
      <div style="font-family:monospace; font-size:12px;">RIB : {donneur_rib}</div>
      <div style="font-size:12px; color:#64748b; margin-top:4px;">Canal : {canal}</div>
      <div style="font-size:12px; color:#64748b;">Réf. Interbancaire BAM : <span style="font-family:monospace;">{bam_ref}</span></div>
    </div>
  </div>

  <table class="table">
    <thead>
      <tr>
        <th>Libellé Bancaire Officiel</th>
        <th>Motif de Rapprochement</th>
        <th>Sens</th>
        <th style="text-align:right;">Montant Net</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td style="font-weight:600;">{libelle}</td>
        <td style="font-family:monospace; font-weight:700; color:var(--crimson);">{motif_escaped}</td>
        <td><strong style="color:#0e6245;">{sens}</strong></td>
        <td style="text-align:right; font-weight:800; font-size:15px;">{montant_mad} MAD</td>
      </tr>
    </tbody>
  </table>

  <div class="amount-box">
    <div>
      <div style="font-size:12px; color:#0e6245; font-weight:700; text-transform:uppercase;">Solde Comptable Après Compensation</div>
      <div style="font-size:13px; color:#475569;">Inscrit au grand livre de trésorerie centrale</div>
    </div>
    <div style="font-size:24px; font-weight:800; color:#0e6245;">{solde_apres_mad} MAD</div>
  </div>

  <div class="no-print" style="margin:24px 0 12px; display:flex; gap:12px; justify-content:flex-end;">
    <button onclick="window.print()" style="background:var(--navy); color:#fff; border:none; padding:10px 18px; border-radius:4px; font-weight:600; cursor:pointer;">Imprimer / Télécharger PDF</button>
  </div>

  <div class="footer">
    Avis édité sous le contrôle de la plateforme centrale de trésorerie <strong>Charikat.ma</strong> via le moteur bancaire <strong>VEXT Open Banking</strong> pour <strong>CFG Bank</strong>.<br>
    Conforme aux règles de compensation interbancaire SIMT et Virement Instantané de Bank Al-Maghrib (BAM). Ce document numérique fait foi d'écriture comptable irrévocable.
  </div>
</div>
</body>
</html>"""

    return router
