"""Deterministic matching. No LLM. Conservative by construction: a match commits only when it
is the ONLY explanation touching any of its ids, and it passes the same verifier the agent uses.

Level A (bank credit <-> settlements):  L1 exact UTR · L2 exact amount · L3 bounded subset-sum
Level B (payments <-> invoices):        L1 receipt ref · L2 exact amount · L3 bounded subset-sum
                                        (L2/L3 scoped to one customer)
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from recon.book import Book
from recon.models import Hypothesis, Match, Proof
from recon.money import credit_within_window, fmt_inr
from recon.verifier import verify

STRICT_CREDIT_LAG_DAYS = 1   # deterministic passes only trust same-day / next-day credits


def find_subset_sum(candidates: list[tuple[str, int]], target_paise: int, max_k: int = 5,
                    tolerance_paise: int = 0, limit: int = 64) -> list[list[str]]:
    """Every subset of size 1..max_k whose sum is within tolerance of target.

    Exact search over a bounded depth: amounts are paise in the crores, so a DP table over
    paise would need ~10^9 cells, while a pruned DFS with k <= 5 over tens of candidates is
    exact and fast. Returns at most limit+1 solutions — callers treat "more than one" as
    ambiguity, so they only need to know that a second solution exists."""
    items = sorted(((i, a) for i, a in candidates if a > 0), key=lambda x: -x[1])
    out: list[list[str]] = []

    def dfs(start: int, chosen: list[str], total: int) -> None:
        if len(out) > limit:
            return
        if chosen and abs(total - target_paise) <= tolerance_paise:
            out.append(sorted(chosen))
        if len(chosen) == max_k:
            return
        for j in range(start, len(items)):
            iid, amt = items[j]
            if total + amt > target_paise + tolerance_paise:
                continue
            chosen.append(iid)
            dfs(j + 1, chosen, total + amt)
            chosen.pop()

    dfs(0, [], 0)
    return out


def _commit(book: Book, level, left, right, rule, equation, detail="", pattern=None) -> Match:
    m = Match(match_id=book.store.next_match_id(), level=level, left_ids=tuple(sorted(left)),
              right_ids=tuple(sorted(right)), source="deterministic", pattern=pattern,
              proof=Proof(rule=rule, equation=equation, ids=tuple(sorted(left) + sorted(right)),
                          detail=detail))
    book.store.commit(m)
    return m


def _eq_a(book: Book, left, right) -> str:
    lhs = " + ".join(fmt_inr(book.bank[t].credit_paise) for t in sorted(left))
    rhs = " + ".join(fmt_inr(book.settlements[s].amount) for s in sorted(right))
    return f"bank {lhs} = settlements {rhs}"


def _eq_b(book: Book, left, right) -> str:
    lhs = " + ".join(fmt_inr(book.rows[p].amount) for p in sorted(left))
    rhs = " + ".join(fmt_inr(book.invoices[i].amount_paise) for i in sorted(right))
    return f"payments {lhs} = invoices {rhs}"


def _commit_unique(book: Book, level: str, explanations: list[tuple[list[str], list[str], str]],
                   rivals: list[tuple[list[str], list[str]]] = ()):
    """Commit each explanation whose ids appear in no other explanation and which verifies.

    `rivals` are explanations that are plausible but outside the strict window: they never
    commit, but they DO count as competing claimants. Without them, a decoy credit inside the
    strict window looks unique when the real credit landed late after a bank holiday."""
    touches = Counter(i for left, right, _ in explanations for i in set(left) | set(right))
    seen = {(tuple(sorted(l)), tuple(sorted(r))) for l, r, _ in explanations}
    extra = {(tuple(sorted(l)), tuple(sorted(r))) for l, r in rivals} - seen
    touches.update(i for left, right in extra for i in set(left) | set(right))
    committed = []
    for left, right, rule in explanations:
        if any(touches[i] > 1 for i in set(left) | set(right)):
            continue
        h = (Hypothesis(pattern="fee_gst_deduction", bank_txn_ids=left, settlement_ids=right)
             if level == "A" else
             Hypothesis(pattern="many_to_one", recon_entity_ids=left, invoice_ids=right))
        if verify(h, level, book, left[0]):
            continue
        eq = _eq_a(book, left, right) if level == "A" else _eq_b(book, left, right)
        committed.append(_commit(book, level, left, right, rule, eq))
    return committed


# --------------------------------------------------------------- level A

def run_level_a(book: Book) -> list[Match]:
    out: list[Match] = []
    by_utr = {s.utr: s for s in book.settlements.values()}
    # L1: full UTR + exact amount + window
    for t in book.credits():
        if t.utr_confidence != "full" or not book.free(t.txn_id):
            continue
        s = by_utr.get(t.utr)
        if s is None or not book.free(s.settlement_id) or s.amount != t.credit_paise:
            continue
        h = Hypothesis(pattern="fee_gst_deduction", bank_txn_ids=[t.txn_id],
                       settlement_ids=[s.settlement_id])
        if verify(h, "A", book, t.txn_id):
            continue
        out.append(_commit(book, "A", [t.txn_id], [s.settlement_id], "L1_utr",
                           _eq_a(book, [t.txn_id], [s.settlement_id]),
                           detail=f"UTR {t.utr} in narration"))

    # L2 / L3: amount and bounded subset-sum inside a strict window, unique only
    explanations = []
    free_setl = [s for s in book.settlements.values() if book.free(s.settlement_id)]
    rivals = []
    for t in book.credits():
        if not book.free(t.txn_id):
            continue
        strict = [(s.settlement_id, s.amount) for s in free_setl
                  if s.settled_on <= t.date <= s.settled_on + timedelta(days=STRICT_CREDIT_LAG_DAYS)]
        for sol in find_subset_sum(strict, t.credit_paise, max_k=3):
            explanations.append(([t.txn_id], sol, "L2_amount" if len(sol) == 1 else "L3_subset_sum"))
        wide = [(s.settlement_id, s.amount) for s in free_setl
                if credit_within_window(s.settled_on, t.date)]
        for sol in find_subset_sum(wide, t.credit_paise, max_k=3):
            rivals.append(([t.txn_id], sol))
    out += _commit_unique(book, "A", explanations, rivals)
    return out


# --------------------------------------------------------------- level B

def run_level_b(book: Book) -> list[Match]:
    out: list[Match] = []
    by_receipt = {i.receipt_ref: i for i in book.invoices.values() if i.receipt_ref}
    for p in book.payments.values():
        if not p.order_receipt or not book.free(p.entity_id):
            continue
        inv = by_receipt.get(p.order_receipt)
        if inv is None or not book.free(inv.invoice_id) or inv.amount_paise != p.amount:
            continue
        h = Hypothesis(pattern="many_to_one", recon_entity_ids=[p.entity_id],
                       invoice_ids=[inv.invoice_id])
        if verify(h, "B", book, p.entity_id):
            continue
        out.append(_commit(book, "B", [p.entity_id], [inv.invoice_id], "L1_receipt",
                           _eq_b(book, [p.entity_id], [inv.invoice_id]),
                           detail=f"order_receipt {p.order_receipt} = receipt_ref"))

    # Customer-scoped exact sums. Shapes: 1 payment -> k invoices, or k payments -> 1 invoice.
    pays_by_c: dict[str, list] = {}
    for p in book.payments.values():
        cid = book.customer_id(p)
        if cid and book.free(p.entity_id):
            pays_by_c.setdefault(cid, []).append(p)
    invs_by_c: dict[str, list] = {}
    for i in book.open_invoices():
        invs_by_c.setdefault(i.customer_id, []).append(i)

    for cid, pays in sorted(pays_by_c.items()):
        invs = invs_by_c.get(cid, [])
        if not invs:
            continue
        explanations = []
        inv_c = [(i.invoice_id, i.amount_paise) for i in invs]
        for p in pays:
            for sol in find_subset_sum(inv_c, p.amount, max_k=5):
                explanations.append(([p.entity_id], sol, "L2_amount" if len(sol) == 1
                                     else "L3_subset_sum"))
        pay_c = [(p.entity_id, p.amount) for p in pays]
        for i in invs:
            for sol in find_subset_sum(pay_c, i.amount_paise, max_k=3):
                if len(sol) >= 2:
                    explanations.append((sol, [i.invoice_id], "L3_subset_sum"))
        out += _commit_unique(book, "B", explanations)
    return out


def run_deterministic(book: Book) -> list[Match]:
    return run_level_a(book) + run_level_b(book)


def residue(book: Book) -> tuple[list[str], list[str]]:
    """Unmatched exceptions left for the agent: bank credits (A) and payments (B)."""
    # Most-constrained first: the credit with the most specific UTR evidence resolves first, so
    # a weaker credit sees only the settlements that are genuinely still available to it.
    open_a = [t for t in book.credits() if book.free(t.txn_id)]
    open_a.sort(key=lambda t: (-(len(t.utr) if t.utr else 0), t.date, t.txn_id))
    a = [t.txn_id for t in open_a]
    b = [p for p in book.payments if book.free(p)]
    return a, b
