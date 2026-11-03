"""Settlement gap explainer and month-end report on the seeded sample month."""

from pathlib import Path

import pytest

from recon import gaps, pipeline, report

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "demo"


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    b, _ = pipeline.run(SAMPLE, tmp_path_factory.mktemp("g") / "g.db", use_agent=True, offline=True)
    return b


def test_waterfall_adds_up_exactly(book):
    r = gaps.explain(book, gaps.FLAT_2PCT)
    t = r.totals
    assert t["gross"] - t["fees"] - t["gst"] - t["refunds"] - t["chargebacks"] - t["other"] == t["net"]
    assert t["net"] == sum(s.amount for s in book.settlements.values())
    for g in r.groups:
        if g.status == "reached bank":
            assert g.gap == 0


def test_flat_contract_finds_no_leak_and_sample_contract_finds_upi(book):
    assert gaps.explain(book, gaps.FLAT_2PCT).leak_paise == 0
    r = gaps.explain(book, gaps.SAMPLE_CONTRACT)
    assert r.leak_paise > 0
    upi = [f for f in r.findings if f.method == "upi"]
    assert upi and all(f.kind == "fee above contract" and f.fee_expected == 0 for f in upi)
    # excess = whole fee + whole GST when the contract rate is zero
    assert all(f.excess == f.fee_charged + f.tax_charged for f in upi)


def test_unmatched_settlement_is_reported_not_found(book):
    with book.store.db:
        sid = next(iter(book.settlements))
        rows = [m for m in book.store.matches() if m.level == "A" and sid in m.right_ids]
    r = gaps.explain(book, gaps.FLAT_2PCT)
    g = next(g for g in r.groups if sid in g.settlement_ids)
    assert (g.status == "reached bank") == bool(rows)


def test_ticket_cites_payment_ids(book):
    r = gaps.explain(book, gaps.SAMPLE_CONTRACT)
    t = gaps.ticket(r, "Test School")
    top = r.findings[0].entity_id
    assert "Test School" in t and top in t
    assert gaps.ticket(gaps.explain(book, gaps.FLAT_2PCT)).startswith("Nothing to raise") or "Settlements" in t


def test_month_end_every_paid_invoice_has_a_proof_chain(book):
    lines, unplaced = report.month_end(book)
    paid = [x for x in lines if x.status == "paid"]
    assert paid and all("->" in x.proof for x in paid)
    assert all(x.proof == "" for x in lines if x.status == "unpaid")
    placed = {p for m in book.store.matches() if m.level == "B" for p in m.left_ids}
    assert {u["payment_id"] for u in unplaced}.isdisjoint(placed)


def test_upload_with_any_bank_format(tmp_path):
    b, info = pipeline.run_files(SAMPLE / "razorpay_recon.csv", SAMPLE / "bank_statement_B.csv",
                                 SAMPLE / "ledger.csv", tmp_path / "u.db", use_agent=False, offline=True)
    assert info["bank_format"] in {"heuristic", "cache"} and info["deterministic"] > 0
