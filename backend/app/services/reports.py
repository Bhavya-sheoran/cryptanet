"""Forensic report generation with a verifiable chain-of-custody hash.

The hash is of the *finished PDF bytes*, computed once and stored. Anyone
holding the file can run `sha256sum` and compare against the digest printed
inside the report and returned by the API - if the file were altered after
export, the recomputed digest would not match the stored one.

fpdf2 is used because it is pure Python: no system libraries to install, which
matters for a container that has to build reproducibly.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Case,
    CaseNote,
    CaseWallet,
    Evidence,
    Report,
    StrDraft,
    TraceRun,
    User,
    Wallet,
)
from app.services import exposure as exposure_svc

logger = logging.getLogger(__name__)
settings = get_settings()

STORAGE = Path(settings.storage_dir)
REPORT_DIR = STORAGE / "reports"

SYNTHETIC_NOTICE = (
    "DEMONSTRATION DOCUMENT. This report was produced by a prototype built for "
    "Smart India Hackathon 2026 (SIH26183). The complaint and blockchain data it "
    "describes are synthetic or drawn from public datasets. It contains no real "
    "NCRP complaint data and no exchange KYC data, and it is not a law-enforcement "
    "record."
)

RECOMMENDATION_NOTICE = (
    "Findings are automated recommendations. No freeze, disclosure or enforcement "
    "action is taken by this system. Any such action requires explicit approval by "
    "an authorised officer."
)


def _ascii(text: str) -> str:
    """fpdf2's core fonts are Latin-1; drop anything they cannot encode.

    Wallet addresses and case numbers are ASCII, so this only ever affects free
    text an investigator typed.
    """
    return (text or "").encode("latin-1", "replace").decode("latin-1")


class ForensicPDF(FPDF):
    def __init__(self, case_number: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.case_number = case_number
        self.set_auto_page_break(auto=True, margin=18)

    def header(self):
        self.set_font("Helvetica", "B", 13)
        self.cell(
            0, 8, "Cryptocurrency Fraud Trace - Forensic Report",
            new_x="LMARGIN", new_y="NEXT",
        )
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 4, f"SIH26183 prototype | Case {self.case_number}",
            new_x="LMARGIN", new_y="NEXT",
        )
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120, 120, 120)
        self.cell(0, 4, "SYNTHETIC / PUBLIC-DATASET DEMONSTRATION - not a law-enforcement record",
                  align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0, 0, 0)

    # -- helpers --------------------------------------------------------
    def h2(self, text: str):
        self.ln(2)
        self.set_font("Helvetica", "B", 10.5)
        self.cell(0, 6, _ascii(text), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)

    def kv(self, key: str, value: str):
        self.set_font("Helvetica", "B", 9)
        self.cell(45, 5, _ascii(key), new_x="RIGHT", new_y="TOP")
        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, _ascii(str(value)), new_x="LMARGIN", new_y="NEXT")

    def para(self, text: str, size: float = 9):
        self.set_font("Helvetica", "", size)
        self.multi_cell(0, 4.6, _ascii(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def notice(self, text: str):
        self.set_fill_color(250, 243, 219)
        self.set_font("Helvetica", "I", 8)
        self.multi_cell(0, 4.2, _ascii(text), border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def flow(self, steps: list[tuple[str, str]]):
        """Draw the route as a vertical chain of labelled boxes.

        Drawn with fpdf2's own primitives rather than an embedded image: it
        keeps the report a pure-Python build with no plotting dependency, and
        the addresses stay selectable text rather than pixels, which is what
        makes them checkable against a block explorer.
        """
        box_w, box_h, gap = 150.0, 9.0, 4.0
        for i, (role, address) in enumerate(steps):
            # Keep a box and its connector together across a page break.
            if self.get_y() + box_h + gap > self.h - self.b_margin:
                self.add_page()

            x, y = self.l_margin, self.get_y()
            last = i == len(steps) - 1
            if i == 0:
                self.set_fill_color(226, 236, 250)
            elif last:
                self.set_fill_color(250, 226, 226)
            else:
                self.set_fill_color(244, 244, 244)
            self.set_draw_color(170, 170, 170)
            self.rect(x, y, box_w, box_h, style="DF")

            self.set_xy(x + 2, y + 1.2)
            self.set_font("Helvetica", "B", 7)
            self.cell(30, 3, _ascii(role), new_x="LMARGIN", new_y="NEXT")
            self.set_xy(x + 2, y + 4.6)
            self.set_font("Helvetica", "", 7.5)
            self.cell(box_w - 4, 3.4, _ascii(address), new_x="LMARGIN", new_y="NEXT")

            self.set_y(y + box_h)
            if not last:
                # Connector between this hop and the next.
                mid = x + box_w / 2
                self.line(mid, self.get_y(), mid, self.get_y() + gap)
                self.set_y(self.get_y() + gap)
        self.ln(2)


def _gather(db: Session, case: Case) -> dict:
    wallets = list(
        db.execute(
            select(Wallet, CaseWallet.role)
            .join(CaseWallet, CaseWallet.wallet_id == Wallet.id)
            .where(CaseWallet.case_id == case.id)
        ).all()
    )
    traces = list(
        db.execute(
            select(TraceRun).where(TraceRun.case_id == case.id).order_by(TraceRun.started_at)
        ).scalars().all()
    )
    notes = list(
        db.execute(
            select(CaseNote).where(CaseNote.case_id == case.id).order_by(CaseNote.created_at)
        ).scalars().all()
    )
    exhibits = list(
        db.execute(
            select(Evidence).where(Evidence.case_id == case.id).order_by(Evidence.uploaded_at)
        ).scalars().all()
    )
    # The most recent STR draft, if an officer has drafted one. The report
    # reproduces an existing draft; it never creates one, because drafting an
    # STR is a deliberate act that belongs to the officer, not a side effect of
    # exporting a PDF.
    str_draft = db.execute(
        select(StrDraft)
        .where(StrDraft.case_id == case.id)
        .order_by(StrDraft.created_at.desc())
    ).scalars().first()
    return {
        "wallets": wallets,
        "traces": traces,
        "notes": notes,
        "exhibits": exhibits,
        "str_draft": str_draft,
    }


def _route_of(exposure: dict) -> dict | None:
    """The top-ranked route from an exposure result, ready to draw.

    Returns None when there is nothing real to draw - no exposure, a direct
    payment (which is a single transaction, not a route), or a candidate with
    no path recorded. Drawing a placeholder diagram in those cases would put a
    picture in a case file that no data supports.
    """
    if not exposure:
        return None
    candidates = exposure.get("candidates") or []
    if not candidates:
        return None

    top = min(candidates, key=lambda c: c.get("rank", 99))
    features = top.get("features") or {}
    path = features.get("shortest_path") or []
    if len(path) < 2:
        return None

    steps = []
    for i, addr in enumerate(path):
        if i == 0:
            role = "Reported wallet"
        elif i == len(path) - 1:
            role = features.get("service") or "Destination"
        else:
            role = f"Hop {i}"
        steps.append((role, addr))

    return {
        "service": features.get("service") or "an unidentified service",
        "service_type": (features.get("service_type") or "unknown").replace("_", " "),
        "hop": features.get("hop", len(path) - 1),
        "amount_inr": features.get("total_volume_inr"),
        "path": path,
        "steps": steps,
    }


def _scam_report_line(summary: dict | None) -> str:
    """One case-file line for the Chainabuse screening of the reported wallet.

    "Not checked" is stated as such. Printing "0 reports" when the lookup never
    ran would put a false negative in evidence.
    """
    status = (summary or {}).get("status")
    if status == "checked":
        n = summary.get("report_count", 0)
        if not n:
            return "checked - no reports filed against this wallet"
        top = list((summary.get("categories") or {}).items())[:4]
        cats = ", ".join(f"{k} ({v})" for k, v in top)
        verified = summary.get("verified_reports", 0)
        return (
            f"{n} report(s) filed against this wallet"
            + (f", {verified} verified by Chainabuse" if verified else "")
            + (f" - {cats}" if cats else "")
        )
    if status == "unavailable":
        return "not checked - Chainabuse did not respond"
    return "not checked - no Chainabuse API key configured"


def generate_case_report(
    db: Session, case: Case, generated_by: User | None = None, analysis: dict | None = None
) -> Report:
    """Render the case to PDF, hash the bytes, and record the artifact."""
    data = _gather(db, case)
    pdf = ForensicPDF(case.case_number)
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.notice(SYNTHETIC_NOTICE)

    pdf.h2("1. Case")
    pdf.kv("Case number", case.case_number)
    pdf.kv("Status", case.status)
    pdf.kv("Source", case.source)
    pdf.kv("NCRP reference", case.ncrp_ref or "not supplied")
    pdf.kv("Reported at", case.reported_at.isoformat() if case.reported_at else "-")
    pdf.kv("Victim reference", case.victim_ref or "not supplied (pseudonymous only)")
    pdf.kv("Amount (INR)", f"{case.amount_inr}" if case.amount_inr is not None else "not stated")
    if case.narrative:
        pdf.ln(1)
        pdf.para(f"Narrative: {case.narrative}")

    pdf.h2("2. Reported addresses")
    if data["wallets"]:
        for wallet, role in data["wallets"]:
            pdf.kv(f"{wallet.chain} ({role})", wallet.address)
    else:
        pdf.para("None recorded.")

    pdf.h2("3. Trace runs")
    if data["traces"]:
        for t in data["traces"]:
            pdf.kv(
                "Trace",
                f"depth {t.max_depth} | status {t.status} | hops {t.hops_discovered} | "
                f"addresses {t.addresses_touched} | mixer contact: "
                f"{'yes' if t.mixer_interaction else 'no'} | data source: {t.data_source}",
            )
    else:
        pdf.para("No trace has been run for this case.")

    if analysis:
        pdf.h2("4. Attribution and risk (the reported wallet)")
        pdf.para(
            "This section identifies and rates the REPORTED WALLET's cluster - who appears to "
            "control the address in the complaint. Where the money ended up is a separate "
            "question, answered in the money-flow section below. The two can legitimately "
            "differ: an unidentified wallet can still send money to a named exchange.",
            size=8,
        )
        attribution = analysis.get("attribution") or {}
        pdf.kv("Attribution method", attribution.get("method", "none"))
        pdf.kv("Entity", attribution.get("entity_name") or "not named")
        pdf.kv("Entity type", attribution.get("entity_type") or "-")
        pdf.kv("Tag source", attribution.get("source") or "-")
        pdf.kv("Confidence", str(attribution.get("confidence", "-")))
        pdf.kv("Risk label", str(analysis.get("risk_label", "-")).upper())
        pdf.kv("Risk score", f"{analysis.get('risk_score', 0)} / 100")

        if attribution.get("method") == "classifier":
            pdf.para(
                "Attribution came from the behavioural classifier, not a curated tag. The service "
                "category is a suggestion; no entity is named because behaviour alone cannot "
                "identify one."
            )

        factors = analysis.get("factors") or []
        if factors:
            # The factor list is the plain-English reason for the rating. A
            # score without it cannot be justified in a case file, which is
            # the same rule the dashboard follows.
            pdf.ln(1)
            pdf.para("Why this rating was given:")
            for factor in factors:
                pdf.para(f"  - {factor}", size=8.5)

        contributions = analysis.get("contributions") or []
        if contributions:
            pdf.h2("5. Cases contributing to the risk score")
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(42, 5, "Case", border=1)
            pdf.cell(26, 5, "Reported", border=1)
            pdf.cell(16, 5, "Age", border=1, align="R")
            pdf.cell(24, 5, "Decay wt", border=1, align="R")
            pdf.cell(20, 5, "Points", border=1, align="R")
            pdf.ln()
            pdf.set_font("Helvetica", "", 8)
            for c in contributions[:30]:
                pdf.cell(42, 5, _ascii(str(c.get("case_number", ""))), border=1)
                pdf.cell(26, 5, str(c.get("reported_at", ""))[:10], border=1)
                pdf.cell(16, 5, f"{c.get('age_days', 0)}d", border=1, align="R")
                pdf.cell(24, 5, f"{c.get('decay_weight', 0):.4f}", border=1, align="R")
                pdf.cell(20, 5, f"{c.get('points', 0):.2f}", border=1, align="R")
                pdf.ln()
            pdf.ln(1)
            pdf.para(
                "Score = sum of points, plus aggravating factors, mapped onto 0-100 by a "
                "saturating curve. The figures above are sufficient to recompute it."
            )

    exposure = (analysis or {}).get("exposure") or {}
    route = _route_of(exposure)
    if route:
        pdf.h2("6. Money flow")
        service = route["service"]
        pdf.para(
            f"The reported wallet did not pay {service} directly. The money was followed "
            f"onward through {len(route['path']) - 1} transfer(s) to reach it. Each box below "
            "is one wallet the money passed through, in order."
        )
        pdf.flow(route["steps"])
        if route["amount_inr"] is not None:
            pdf.kv("Amount reaching destination", f"INR {route['amount_inr']:,.2f} (estimated)")
        pdf.kv("Destination", f"{service} ({route['service_type']})")
        pdf.kv("Hops", str(route["hop"]))
        pdf.para(
            "Rupee figures are estimated from public exchange rates at the time of analysis. "
            "They are not exchange records and carry no KYC. Transfers below the dust floor of "
            f"INR {exposure_svc.VOLUME_DUST_INR:,.0f} score zero on value, so a negligible "
            "amount arriving at a service cannot rank as exposure on proximity alone.",
            size=8,
        )

    pdf.h2("7. Mixer and sanctions screening")
    mixer_seen = any(t.mixer_interaction for t in data["traces"])
    pdf.kv("Tagged mixer contact", "yes - flagged, not unwound" if mixer_seen else "none observed")
    entity_type = ((analysis or {}).get("attribution") or {}).get("entity_type")
    factor_text = " ".join((analysis or {}).get("factors") or []).lower()
    sanctioned = entity_type == "sanctioned" or "sdn" in factor_text
    pdf.kv("Sanctions screening", "MATCH - destination on the OFAC SDN list" if sanctioned
           else "no match against the loaded sanctions list")
    pdf.kv("Scam reports (Chainabuse)", _scam_report_line((analysis or {}).get("scam_reports")))
    pdf.para(
        "Screening is against the public sanctions and mixer labels loaded into this system. "
        "A 'no match' means nothing matched those lists, not that the funds are clean.",
        size=8,
    )

    pdf.h2("8. Case notes")
    if data["notes"]:
        for n in data["notes"]:
            stamp = n.created_at.isoformat() if n.created_at else ""
            pdf.para(f"[{stamp}] {n.body}")
    else:
        pdf.para("No notes recorded.")

    pdf.h2("9. Evidence exhibits (chain of custody)")
    if data["exhibits"]:
        for e in data["exhibits"]:
            pdf.kv(e.filename, f"sha256 {e.sha256} | {e.size_bytes} bytes | {e.uploaded_at}")
    else:
        pdf.para("No exhibits attached.")

    pdf.h2("10. Suspicious Transaction Report draft")
    draft = data["str_draft"]
    if draft is not None:
        pdf.kv("Draft status", draft.status)
        pdf.kv("Drafted at", draft.created_at.isoformat() if draft.created_at else "-")
        pdf.para(
            "Reproduced below as drafted. It has not been filed with FIU-IND by this system.",
            size=8,
        )
        pdf.ln(1)
        pdf.para(draft.body, size=7.5)
    else:
        pdf.para(
            "No STR has been drafted for this case. A draft can be produced from the case "
            "screen; drafting and filing are deliberate acts for an authorised officer, so "
            "this export does not create one."
        )

    pdf.h2("11. Integrity and limitations")
    generated_at = datetime.now(UTC)
    pdf.kv("Generated at", generated_at.isoformat())
    pdf.kv("Generated by", generated_by.username if generated_by else "system")
    pdf.para(
        "The SHA-256 digest of this document is returned by the export API and stored against "
        "the case record. To verify this file has not been altered, recompute the digest of the "
        "PDF and compare it with the stored value."
    )
    pdf.notice(RECOMMENDATION_NOTICE)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = bytes(pdf.output())
    digest = hashlib.sha256(payload).hexdigest()

    filename = f"{case.case_number}_{generated_at.strftime('%Y%m%dT%H%M%SZ')}_{digest[:12]}.pdf"
    path = REPORT_DIR / filename
    path.write_bytes(payload)

    report = Report(
        case_id=case.id,
        kind="forensic_pdf",
        sha256=digest,
        storage_path=str(path),
        generated_by=generated_by.id if generated_by else None,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    logger.info("forensic report %s written for %s (sha256 %s)", filename, case.case_number, digest)
    return report


def verify_report(report: Report) -> dict:
    """Recompute the digest of the stored file and compare with the record."""
    path = Path(report.storage_path)
    if not path.exists():
        return {"verified": False, "reason": "report file is missing from storage"}
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "verified": actual == report.sha256,
        "stored_sha256": report.sha256,
        "recomputed_sha256": actual,
        "size_bytes": path.stat().st_size,
    }
