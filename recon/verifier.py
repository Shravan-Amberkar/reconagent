"""The only path to committing a match. Pure functions: no I/O except reads of the Book.

`verify` returns a list of specific, human-readable violations. Empty list = pass. The
violations are fed back VERBATIM to the agent's next attempt — that is the self-correction loop.
"""

from __future__ import annotations

import re
from datetime import timedelta

from rapidfuzz import fuzz

from recon.book import Book
from recon.models import Hypothesis, Level
from recon.money import credit_within_window, tax_matches

INVOICE_EARLY_DAYS = 3     # a payment may precede its invoice's issue date by this much
INVOICE_LATE_DAYS = 30     # ...and follow its due date by this much
NAME_MATCH = 95            # rapidfuzz partial-ratio fallback: tolerates ~1 typo, no more


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def name_evidence(name: str, notes_text: str) -> bool:
    """Does the payment's free text actually name this customer?

    The full name must appear as a contiguous phrase, with a strict fuzzy fallback for one
    typo. Tokens scattered across different people do NOT count: notes naming "Myra Pillai,
    Saanvi Sharma" are not evidence for "Myra Sharma"."""
    n, t = _norm(name), _norm(notes_text)
    if not n:
        return False
    if re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", t):
        return True
    return fuzz.partial_ratio(n, t) >= NAME_MATCH


def verify(h: Hypothesis, level: Level, book: Book, exception_id: str) -> list[str]:
    v: list[str] = []
    if level == "A":
        left, right = h.bank_txn_ids, h.settlement_ids
        if h.recon_entity_ids or h.invoice_ids:
            v.append("level A hypothesis must not reference payments or invoices")
    else:
        left, right = h.recon_entity_ids, h.invoice_ids
        if h.bank_txn_ids or h.settlement_ids:
            v.append("level B hypothesis must not reference bank lines or settlements")
    if not left or not right:
        v.append("both sides of the match must be non-empty")
        return v
    if exception_id not in left:
        v.append(f"hypothesis does not include the exception being resolved ({exception_id})")
    for ids in (left, right):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            v.append(f"duplicate ids: {dupes}")

    # 1. every referenced id exists (hallucination guard) — and is the right kind of thing
    if level == "A":
        for t in left:
            txn = book.bank.get(t)
            if txn is None:
                v.append(f"unknown id {t}")
            elif txn.credit_paise <= 0:
                v.append(f"{t} is not a credit")
            elif txn.duplicate_of:
                v.append(f"{t} is an export duplicate of {txn.duplicate_of}")
        for s in right:
            if s not in book.settlements:
                v.append(f"unknown id {s}")
    else:
        for p in left:
            row = book.rows.get(p)
            if row is None:
                v.append(f"unknown id {p}")
            elif row.type != "payment":
                v.append(f"{p} is a {row.type} row, not a payment")
        for i in right:
            inv = book.invoices.get(i)
            if inv is None:
                v.append(f"unknown id {i}")
            elif inv.status != "open":
                v.append(f"{i} is {inv.status}, not open")
    if v:
        return v   # the rest of the checks need real ids

    # 2. nothing already consumed
    for i in list(left) + list(right):
        m = book.store.consumed_by(i)
        if m:
            v.append(f"{i} already reconciled in match {m}")

    # 3. every adjustment is traceable to a real recon row — no free-floating "plugs"
    for adj in h.adjustments:
        row = book.rows.get(adj.source_entity_id)
        if row is None:
            v.append(f"adjustment cites unknown recon row {adj.source_entity_id}")
            continue
        if level == "A" and row.settlement_id not in right:
            v.append(f"adjustment {adj.source_entity_id} belongs to {row.settlement_id}, "
                     f"which is not in this match")
        if level == "B" and row.entity_id not in left and row.payment_id not in left:
            v.append(f"adjustment {adj.source_entity_id} does not act on any payment in this match")
        expected = {"fee": row.fee, "tax": row.tax,
                    "refund": row.amount if row.type == "refund" else None,
                    "chargeback": row.amount if row.type == "adjustment" else None}[adj.kind]
        if expected is None:
            v.append(f"adjustment {adj.source_entity_id} is a {row.type} row, not a {adj.kind}")
        elif adj.kind == "tax":
            if not tax_matches(row.fee, adj.amount_paise) or abs(row.tax - adj.amount_paise) > 1:
                v.append(f"tax adjustment {adj.amount_paise} != row tax {row.tax}")
        elif adj.amount_paise != expected:
            v.append(f"{adj.kind} adjustment {adj.amount_paise} != row amount {expected} "
                     f"on {adj.source_entity_id}")

    # 4. arithmetic — the numbers, and the gap, in the message
    if level == "A":
        bank_sum = sum(book.bank[t].credit_paise for t in left)
        setl_sum = sum(book.settlements[s].amount for s in right)
        if bank_sum != setl_sum:
            v.append(f"sum mismatch: bank={bank_sum} settlements={setl_sum} "
                     f"gap={bank_sum - setl_sum}")
    else:
        pay_sum = sum(book.rows[p].amount for p in left)
        inv_sum = sum(book.invoices[i].amount_paise for i in right)
        if pay_sum != inv_sum:
            v.append(f"sum mismatch: payments={pay_sum} invoices={inv_sum} gap={pay_sum - inv_sum}")

    # 5. date window (holiday-aware for bank credits)
    if level == "A":
        for t in left:
            for s in right:
                st = book.settlements[s]
                if not credit_within_window(st.settled_on, book.bank[t].date):
                    v.append(f"date window: {t} on {book.bank[t].date} is outside "
                             f"{s} settled {st.settled_on} + 4 business days")
    else:
        for p in left:
            paid = book.rows[p].created_at
            for i in right:
                inv = book.invoices[i]
                lo = inv.issued_on - timedelta(days=INVOICE_EARLY_DAYS)
                hi = inv.due_on + timedelta(days=INVOICE_LATE_DAYS)
                if not lo <= paid <= hi:
                    v.append(f"date window: {p} paid {paid} outside {i} window {lo}..{hi}")

    # 6. customer consistency (Level B)
    if level == "B":
        for p in left:
            row = book.rows[p]
            cid = book.customer_id(row)
            text = book.notes_text(row)
            for i in right:
                inv = book.invoices[i]
                if cid is not None:
                    if inv.customer_id != cid:
                        v.append(f"customer mismatch: {p} is {cid}, {i} is {inv.customer_id}")
                elif not name_evidence(inv.customer_name, text):
                    v.append(f"no evidence in {p}'s notes that it pays {i} "
                             f"({inv.customer_name})")
    return v
