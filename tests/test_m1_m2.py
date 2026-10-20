import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from recon import ingest
from recon.book import Book
from recon.deterministic import find_subset_sum, run_deterministic
from recon.generator import generate, write
from recon.models import Adjustment, Match, Proof, ReconRow
from recon.money import (add_business_days, credit_within_window, fee_for, fmt_inr,
                         gst_on_fee, payment_net, require_paise)
from recon.store import AlreadyReconciled, Store
from eval.metrics import score

CASES_B = {"clean", "name_only", "partial_payment", "many_to_one", "lump_sum", "erp_typo",
           "ambiguous_twin", "prompt_injection"}
CASES_A = {"clean", "utr_truncated", "holiday_shift", "split_settlement", "netted_refund",
           "chargeback", "duplicate_entry", "negative_control"}


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    out = tmp_path_factory.mktemp("seed7")
    write(generate(7, payments=300), out)
    return out


@pytest.fixture(autouse=True)
def isolated_schema_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "SCHEMA_CACHE", tmp_path / "schema_cache.json")


# ---------------------------------------------------------------- money

def test_money_rejects_float_everywhere():
    with pytest.raises(TypeError):
        require_paise(100.0)
    with pytest.raises(TypeError):
        require_paise(True)
    with pytest.raises(TypeError):
        payment_net(1000, 20.0, 4)
    with pytest.raises(ValidationError):
        Adjustment(kind="fee", source_entity_id="pay_x", amount_paise=10.5)
    with pytest.raises(ValidationError):
        ReconRow(entity_id="p", type="payment", debit=0, credit=980.0, amount=1000, fee=20,
                 tax=4, created_at=date(2026, 9, 1), settled_at=date(2026, 9, 3),
                 settlement_id="s", settlement_utr="u")


def test_gst_round_half_up():
    assert gst_on_fee(100) == 18
    assert gst_on_fee(25) == 5          # 4.5 -> 5 (half up)
    assert gst_on_fee(19) == 3          # 3.42 -> 3
    assert fee_for(45000_00) == 900_00


def test_holiday_aware_window():
    # 2026-09-14 is a synthetic holiday (a Monday): Fri 11th + 1 business day = Tue 15th.
    assert add_business_days(date(2026, 9, 11), 1) == date(2026, 9, 15)
    assert credit_within_window(date(2026, 9, 11), date(2026, 9, 15))
    assert not credit_within_window(date(2026, 9, 11), date(2026, 9, 10))


def test_fmt_inr():
    assert fmt_inr(12345678) == "₹1,23,456.78"
    assert fmt_inr(5) == "₹0.05"


# ---------------------------------------------------------------- generator

def test_same_seed_byte_identical():
    assert generate(11, payments=150) == generate(11, payments=150)
    assert generate(11, payments=150) != generate(12, payments=150)


def test_settlement_arithmetic_consistent(sample):
    rows, setl = ingest.load_recon(sample / "razorpay_recon.csv")   # validates net/tax per row
    truth = json.loads((sample / "ground_truth.json").read_text())
    assert {k: s.amount for k, s in setl.items()} == truth["settlements"]
    for s in setl.values():
        members = [r for r in rows if r.settlement_id == s.settlement_id]
        assert s.amount == sum(r.credit for r in members) - sum(r.debit for r in members)


def test_every_bank_format_balances_and_agrees(sample):
    parsed = [ingest.load_bank(sample / f"bank_statement_{f}.csv")[0] for f in "ABC"]
    dumps = [[t.model_dump() for t in p] for p in parsed]
    assert dumps[0] == dumps[1] == dumps[2]


def test_ground_truth_covers_everything(sample):
    truth = json.loads((sample / "ground_truth.json").read_text())
    book = Book.load(sample)
    assert set(truth["bank"]) == set(book.bank)
    assert set(truth["payments"]) == set(book.payments)
    assert {v["case_type"] for v in truth["payments"].values()} >= CASES_B
    assert {v["case_type"] for v in truth["bank"].values()} >= CASES_A


# ---------------------------------------------------------------- ingest

def test_pii_masked():
    s = ingest.mask_pii("NEFT-HDFCN123456789012-X A/C 50100234567890 MOB 9123456780 a.b@okicici")
    assert "50100234567890" not in s and "9123456780" not in s and "a.b@" not in s
    assert "HDFCN123456789012" in s                       # UTRs survive


def test_wrong_schema_rejected_by_invariant(sample):
    text = (sample / "bank_statement_A.csv").read_text()
    swapped = ingest.FORMAT_A.model_copy(update={"debit_col": "Deposit Amt",
                                                 "credit_col": "Withdrawal Amt"})
    with pytest.raises(ingest.InvariantViolation):
        ingest.parse_with_schema(text, swapped)


def test_unknown_format_without_llm_needs_human(tmp_path):
    p = tmp_path / "weird.csv"
    p.write_text("When,What,Money,Left\n01/09/26,x,10.00,10.00\n02/09/26,y,10.00,99.00\n")
    with pytest.raises(ingest.HumanNeeded):
        ingest.infer_statement_schema(p.read_text(), llm=None, use_cache=False)


def test_known_format_hits_cache_second_time(sample):
    text = (sample / "bank_statement_B.csv").read_text()
    assert ingest.infer_statement_schema(text)[2] == "heuristic"
    assert ingest.infer_statement_schema(text)[2] == "cache"


def test_duplicate_export_line_flagged(sample):
    txns, _ = ingest.load_bank(sample / "bank_statement_A.csv")
    truth = json.loads((sample / "ground_truth.json").read_text())
    dups = {t.txn_id for t in txns if t.duplicate_of}
    assert dups == {k for k, v in truth["bank"].items() if v["case_type"] == "duplicate_entry"}


# ---------------------------------------------------------------- matching

def test_subset_sum_returns_all_solutions():
    c = [("a", 100), ("b", 200), ("c", 300), ("d", 400)]
    assert sorted(find_subset_sum(c, 300)) == [["a", "b"], ["c"]]
    assert find_subset_sum(c, 1000, max_k=3) == []
    assert ["a", "b", "c", "d"] in find_subset_sum(c, 1000, max_k=4)


def test_store_refuses_reuse_and_edits():
    st = Store()
    p = Proof(rule="t", equation="1=1", ids=("x", "y"))
    st.commit(Match(match_id="M1", level="B", left_ids=("x",), right_ids=("y",),
                    source="deterministic", proof=p))
    with pytest.raises(AlreadyReconciled):
        st.commit(Match(match_id="M2", level="B", left_ids=("x",), right_ids=("z",),
                        source="deterministic", proof=p))
    with pytest.raises(Exception):
        with st.db:
            st.db.execute("UPDATE matches SET source='human'")


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_deterministic_never_false_matches(seed, tmp_path):
    write(generate(seed, payments=250), tmp_path)
    book = Book.load(tmp_path)
    run_deterministic(book)
    res = score(book, json.loads((tmp_path / "ground_truth.json").read_text()))
    assert res["levels"]["A"]["false"] == 0
    assert res["levels"]["B"]["false"] == 0
    assert res["negative_control_leakage"] == 0


def test_name_evidence_needs_contiguous_name():
    from recon.verifier import name_evidence
    notes = "students: Myra Pillai, Saanvi Sharma"
    assert name_evidence("Myra Pillai", notes)
    assert not name_evidence("Myra Sharma", notes)        # tokens from two different people
    assert not name_evidence("Kabir Shah", "student: Kabir Sharma")


def test_llm_schema_fallback_gated_by_invariant(tmp_path):
    """A wrong LLM mapping is rejected, the failing rows are fed back, the corrected one wins."""
    text = "Dt,Info,Out,In,Bal\n01/09/26,a,,100.00,100.00\n02/09/26,b,40.00,,60.00\n"
    good = ingest.StatementSchema(header_row=0, date_col="Dt", narration_col="Info",
                                  balance_col="Bal", date_format="%d/%m/%y",
                                  sign_convention="split", debit_col="Out", credit_col="In")
    bad = good.model_copy(update={"debit_col": "In", "credit_col": "Out"})
    seen = []

    def fake_llm(rows, feedback):
        seen.append(feedback)
        return bad if len(seen) == 1 else good

    schema, txns, src = ingest.infer_statement_schema(text, llm=fake_llm, use_cache=False)
    assert src == "llm" and schema == good and len(txns) == 2
    assert seen[0] is None and seen[1]                      # failures fed back on retry


def test_utr_prefix_contradiction_rejected(sample):
    from recon.models import Hypothesis
    from recon.verifier import verify
    book = Book.load(sample)
    t = next(x for x in book.credits() if x.utr_confidence == "full")
    wrong = next(s for s in book.settlements.values() if not s.utr.startswith(t.utr))
    h = Hypothesis(pattern="utr_truncated", bank_txn_ids=[t.txn_id], settlement_ids=[wrong.settlement_id])
    assert any("UTR mismatch" in v for v in verify(h, "A", book, t.txn_id))
