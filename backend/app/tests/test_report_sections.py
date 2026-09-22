"""What the exported case file actually contains.

The PDF is the artifact that leaves the building - it is what an officer puts
in front of someone who never sees the dashboard. So these assert on the
rendered document, not on the data that went into it, and they assert the
things that make it defensible: that a stated risk carries its reasons, that a
drawn route matches the traced route, and that the report never claims a
screening result or a KYC source it does not have.
"""

from __future__ import annotations

import re
import zlib

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.reports import _route_of

client = TestClient(app)


def pdf_text(payload: bytes) -> str:
    """Recover the visible text from an fpdf2 document.

    fpdf2 writes text as string operands inside FlateDecode streams; no
    third-party reader is pulled in just to assert on our own output.
    """
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", payload, re.S):
        chunk = m.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass
        for t in re.finditer(rb"\((?:\\.|[^()\\])*\)", chunk):
            s = t.group(0)[1:-1]
            s = s.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
            out.append(s.decode("latin-1"))
    # Collapsed to single spaces: fpdf2 wraps a paragraph into one text
    # operand per rendered line, so a sentence the reader sees as continuous
    # arrives here split across several. Asserting on the wrapped form would
    # make these tests fail whenever a margin or font size changed, which is
    # not what they are meant to protect.
    return re.sub(r"\s+", " ", " ".join(out))


# ---------------------------------------------------------------------------
# _route_of - what may and may not be drawn
# ---------------------------------------------------------------------------
EXPOSURE = {
    "kind": "indirect",
    "candidates": [
        {
            "rank": 2,
            "features": {"service": "Runner-up", "shortest_path": ["a", "b"], "hop": 1},
        },
        {
            "rank": 1,
            "features": {
                "service": "Meridian Exchange",
                "service_type": "exchange",
                "hop": 7,
                "total_volume_inr": 2003813.202072,
                "shortest_path": ["addr0", "addr1", "addr2", "addr3"],
            },
        },
    ],
}


def test_route_uses_the_top_ranked_candidate_not_the_first_in_the_list():
    # The API does not promise list order matches rank. Drawing candidates[0]
    # would put the runner-up in the case file as though it were the finding.
    route = _route_of(EXPOSURE)
    assert route["service"] == "Meridian Exchange"
    assert route["hop"] == 7


def test_route_steps_name_the_ends_and_number_the_middle():
    steps = _route_of(EXPOSURE)["steps"]
    assert [role for role, _ in steps] == [
        "Reported wallet", "Hop 1", "Hop 2", "Meridian Exchange",
    ]
    # Every address survives, in order: the drawing is the traced path, not a
    # summary of it.
    assert [addr for _, addr in steps] == ["addr0", "addr1", "addr2", "addr3"]


@pytest.mark.parametrize(
    "exposure",
    [
        pytest.param({}, id="no exposure result"),
        pytest.param({"kind": "none", "candidates": []}, id="nothing found"),
        pytest.param(
            {"candidates": [{"rank": 1, "features": {"shortest_path": ["only-one"]}}]},
            id="path too short to be a route",
        ),
        pytest.param(
            {"candidates": [{"rank": 1, "features": {}}]},
            id="candidate with no path recorded",
        ),
    ],
)
def test_nothing_is_drawn_when_there_is_no_route(exposure):
    # A placeholder diagram in a case file is a picture no data supports.
    assert _route_of(exposure) is None


# ---------------------------------------------------------------------------
# The rendered document
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def report_pdf() -> bytes:
    """A real exported report, generated through the API the officer uses.

    Generated end to end rather than by calling the renderer directly: the
    sections below depend on the endpoint assembling attribution, risk factors
    and exposure, and a unit call would not catch that assembly breaking.
    """
    ready = client.get("/api/v1/health/ready")
    if ready.status_code != 200 or ready.json().get("status") != "ready":
        pytest.skip("compose stack not fully up")

    from app.services.connectors.synthetic import get_complaints

    complaints = get_complaints()
    if not complaints:
        pytest.skip("synthetic dataset not generated")

    client.post("/api/v1/auth/seed-demo-users")
    token = client.post(
        "/api/v1/auth/login",
        data={"username": "investigator", "password": "investigator123"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    address = next(c["address"] for c in complaints if c["chain"] == "TRON")
    case_id = client.post(
        "/api/v1/wallets", json={"address": address, "source": "synthetic"}
    ).json()["case_id"]

    created = client.post(f"/api/v1/cases/{case_id}/report", headers=headers)
    assert created.status_code == 201, created.text
    download = client.get(
        f"/api/v1/cases/{case_id}/report/{created.json()['report_id']}/download",
        headers=headers,
    )
    assert download.status_code == 200
    return download.content


def test_the_fixture_really_produced_a_pdf(report_pdf):
    assert report_pdf[:5] == b"%PDF-"


def test_report_carries_the_reasons_behind_the_risk_rating(report_pdf):
    text = pdf_text(report_pdf)
    assert "Why this rating was given" in text
    # The factor text itself, not just the heading.
    assert "trace to this cluster" in text


def test_report_separates_who_owns_the_wallet_from_where_the_money_went(report_pdf):
    # These are different questions with legitimately different answers. The
    # export must not read as though it contradicts itself.
    text = pdf_text(report_pdf)
    assert "Attribution and risk (the reported wallet)" in text
    assert "REPORTED WALLET" in text
    assert "Where the money ended up is a separate" in text


def test_report_states_the_screening_result_without_implying_the_funds_are_clean(report_pdf):
    text = pdf_text(report_pdf)
    assert "Mixer and sanctions screening" in text
    assert "not that the funds" in text and "are clean" in text


def test_report_never_claims_a_kyc_source(report_pdf):
    text = pdf_text(report_pdf)
    assert "no exchange KYC data" in text
    # And any rupee figure it prints is marked as an estimate.
    if "Amount reaching destination" in text:
        assert "estimated from public exchange rates" in text


def test_report_does_not_create_an_str_draft_as_a_side_effect(report_pdf):
    # Drafting an STR is a deliberate act by an officer. Exporting a PDF is
    # not, so with no draft on the case the section must say so rather than
    # producing one.
    text = pdf_text(report_pdf)
    assert "Suspicious Transaction Report draft" in text
    assert "No STR has been drafted for this case" in text


def test_report_records_who_generated_it_and_how_to_verify_it(report_pdf):
    text = pdf_text(report_pdf)
    assert "Generated by" in text
    assert "recompute the digest" in text
    assert "requires explicit approval by" in text
