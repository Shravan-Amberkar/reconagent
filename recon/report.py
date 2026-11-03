"""Month-end report: who paid, who to chase, and payments nobody can place, each line citing the
rows that prove it (invoice -> payment -> settlement -> bank credit)."""

from __future__ import annotations

from dataclasses import dataclass

from recon.book import Book


@dataclass
class Line:
    invoice_id: str
    customer: str
    amount: int
    status: str            # paid | partly paid | unpaid
    paid: int
    proof: str             # chain of ids and the rule that matched them
    due_on: str


def month_end(book: Book) -> tuple[list[Line], list[dict]]:
    matches = book.store.matches()
    bank_for: dict[str, list[str]] = {}
    for m in matches:
        if m.level == "A":
            for sid in m.right_ids:
                bank_for[sid] = list(m.left_ids)
    paid_by: dict[str, list] = {}
    placed: set[str] = set()
    for m in matches:
        if m.level == "B":
            placed.update(m.left_ids)
            for inv in m.right_ids:
                paid_by.setdefault(inv, []).append(m)

    lines = []
    for inv in sorted(book.invoices.values(), key=lambda i: i.invoice_id):
        if inv.status == "cancelled":
            continue
        ms = paid_by.get(inv.invoice_id, [])
        pays = [book.rows[p] for m in ms for p in m.left_ids]
        paid = sum(p.amount for p in pays)
        if ms and len(ms[0].right_ids) > 1:
            paid = inv.amount_paise  # one payment covering several invoices: matched as a whole
        chain = []
        for m in ms:
            for p in m.left_ids:
                r = book.rows[p]
                bank = ", ".join(bank_for.get(r.settlement_id, ["bank credit not matched yet"]))
                chain.append(f"{p} -> {r.settlement_id} -> {bank} [{m.proof.rule}]")
        status = "unpaid" if not ms else ("paid" if paid >= inv.amount_paise else "partly paid")
        lines.append(Line(invoice_id=inv.invoice_id, customer=inv.customer_name, amount=inv.amount_paise,
                          status=status, paid=min(paid, inv.amount_paise), proof="; ".join(chain),
                          due_on=inv.due_on.isoformat()))
    unplaced = [{"payment_id": r.entity_id, "amount": r.amount, "date": r.created_at.isoformat(),
                 "notes": book.notes_text(r), "settlement_id": r.settlement_id}
                for r in book.payments.values() if r.entity_id not in placed]
    return lines, unplaced
