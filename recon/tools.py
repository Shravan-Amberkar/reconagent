"""Agent tools: typed, read-only, bounded. The LLM never sees SQL and never writes anything.

Inputs are pydantic models; a validation error goes back to the model as a tool error so it
can fix its own call. Every string that came from outside (narrations, notes, names,
descriptions) is PII-masked and returned inside an <untrusted_data> block.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Callable

from pydantic import BaseModel, Field, ValidationError
from rapidfuzz import fuzz

from recon.book import Book
from recon.deterministic import find_subset_sum as _subset_sum
from recon.ingest import mask_pii

MAX_RESULTS = 25


def wrap(source: str, payload) -> str:
    body = json.dumps(payload, default=str, ensure_ascii=False, sort_keys=True)
    return f'<untrusted_data source="{source}">\n{body}\n</untrusted_data>'


# --------------------------------------------------------------- inputs

class SettlementQuery(BaseModel):
    settlement_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    include_rows: bool = Field(False, description="include the payment/refund/adjustment rows")


class SubsetSumQuery(BaseModel):
    candidate_ids: list[str] = Field(..., max_length=40,
                                     description="settlement, payment or invoice ids")
    target_paise: int
    max_k: int = Field(5, ge=1, le=5)
    tolerance_paise: int = Field(0, ge=0, le=0, description="must be 0: sums are exact")


class LedgerFilter(BaseModel):
    customer_id: str | None = None
    name_contains: str | None = Field(None, description="fuzzy match on customer name")
    amount_paise: int | None = None
    amount_max_paise: int | None = None
    issued_from: date | None = None
    issued_to: date | None = None
    only_open: bool = True
    limit: int = Field(MAX_RESULTS, ge=1, le=MAX_RESULTS)


class BankFilter(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    amount_paise: int | None = None
    narration_contains: str | None = None
    limit: int = Field(MAX_RESULTS, ge=1, le=MAX_RESULTS)


class PaymentFilter(BaseModel):
    customer_id: str | None = None
    name_contains: str | None = Field(None, description="fuzzy match on payment notes")
    amount_paise: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    only_unmatched: bool = True
    limit: int = Field(MAX_RESULTS, ge=1, le=MAX_RESULTS)


# --------------------------------------------------------------- views

def settlement_view(book: Book, sid: str, rows: bool = False) -> dict:
    s = book.settlements[sid]
    out = {"settlement_id": sid, "settled_on": s.settled_on, "amount_paise": s.amount,
           "utr": s.utr, "reconciled": not book.free(sid), "row_count": len(s.row_ids)}
    if rows:
        out["rows"] = [{"entity_id": r, "type": book.rows[r].type, "amount": book.rows[r].amount,
                        "fee": book.rows[r].fee, "tax": book.rows[r].tax,
                        "payment_id": book.rows[r].payment_id} for r in s.row_ids]
    return out


def invoice_view(book: Book, iid: str) -> dict:
    i = book.invoices[iid]
    return {"invoice_id": iid, "customer_id": i.customer_id,
            "customer_name": mask_pii(i.customer_name), "amount_paise": i.amount_paise,
            "issued_on": i.issued_on, "due_on": i.due_on, "status": i.status,
            "receipt_ref": i.receipt_ref, "reconciled": not book.free(iid)}


def payment_view(book: Book, pid: str) -> dict:
    p = book.rows[pid]
    return {"payment_id": pid, "amount_paise": p.amount, "captured_on": p.created_at,
            "settlement_id": p.settlement_id, "order_receipt": p.order_receipt,
            "description": mask_pii(p.description),
            "notes": {k: mask_pii(v) for k, v in p.notes.items()},
            "reconciled": not book.free(pid)}


def bank_view(book: Book, tid: str) -> dict:
    t = book.bank[tid]
    return {"txn_id": tid, "date": t.date, "narration": mask_pii(t.narration),
            "credit_paise": t.credit_paise, "debit_paise": t.debit_paise,
            "utr_extracted": t.utr, "utr_confidence": t.utr_confidence,
            "export_duplicate_of": t.duplicate_of, "reconciled": not book.free(tid)}


# --------------------------------------------------------------- tools

def fetch_settlement_recon(book: Book, q: SettlementQuery) -> list[dict]:
    if q.settlement_id:
        if q.settlement_id not in book.settlements:
            raise ValueError(f"unknown settlement {q.settlement_id}")
        return [settlement_view(book, q.settlement_id, q.include_rows)]
    out = [settlement_view(book, s.settlement_id, q.include_rows)
           for s in sorted(book.settlements.values(), key=lambda s: s.settled_on)
           if (not q.date_from or s.settled_on >= q.date_from)
           and (not q.date_to or s.settled_on <= q.date_to)]
    return out[:MAX_RESULTS]


def _amount_of(book: Book, iid: str) -> int:
    if iid in book.settlements:
        return book.settlements[iid].amount
    if iid in book.invoices:
        return book.invoices[iid].amount_paise
    if iid in book.rows and book.rows[iid].type == "payment":
        return book.rows[iid].amount
    raise ValueError(f"unknown id {iid}")


def find_subset_sum(book: Book, q: SubsetSumQuery) -> list[list[str]]:
    cands = [(i, _amount_of(book, i)) for i in q.candidate_ids]
    return _subset_sum(cands, q.target_paise, max_k=q.max_k, tolerance_paise=0, limit=10)


def query_ledger(book: Book, f: LedgerFilter) -> list[dict]:
    out = []
    for i in sorted(book.invoices.values(), key=lambda i: i.invoice_id):
        if f.only_open and (i.status != "open" or not book.free(i.invoice_id)):
            continue
        if f.customer_id and i.customer_id != f.customer_id:
            continue
        if f.amount_paise is not None and i.amount_paise != f.amount_paise:
            continue
        if f.amount_max_paise is not None and i.amount_paise > f.amount_max_paise:
            continue
        if f.issued_from and i.issued_on < f.issued_from:
            continue
        if f.issued_to and i.issued_on > f.issued_to:
            continue
        if f.name_contains and fuzz.token_set_ratio(f.name_contains.lower(),
                                                    i.customer_name.lower()) < 80:
            continue
        out.append(invoice_view(book, i.invoice_id))
        if len(out) >= f.limit:
            break
    return out


def search_bank(book: Book, f: BankFilter) -> list[dict]:
    out = []
    for t in book.bank.values():
        if f.date_from and t.date < f.date_from:
            continue
        if f.date_to and t.date > f.date_to:
            continue
        if f.amount_paise is not None and t.credit_paise != f.amount_paise:
            continue
        if f.narration_contains and f.narration_contains.lower() not in t.narration.lower():
            continue
        out.append(bank_view(book, t.txn_id))
        if len(out) >= f.limit:
            break
    return out


def search_payments(book: Book, f: PaymentFilter) -> list[dict]:
    out = []
    for p in sorted(book.payments.values(), key=lambda p: p.entity_id):
        if f.only_unmatched and not book.free(p.entity_id):
            continue
        if f.customer_id and p.notes.get("customer_id") != f.customer_id:
            continue
        if f.amount_paise is not None and p.amount != f.amount_paise:
            continue
        if f.date_from and p.created_at < f.date_from:
            continue
        if f.date_to and p.created_at > f.date_to:
            continue
        if f.name_contains and fuzz.partial_ratio(f.name_contains.lower(),
                                                  book.notes_text(p).lower()) < 85:
            continue
        out.append(payment_view(book, p.entity_id))
        if len(out) >= f.limit:
            break
    return out


TOOLS: dict[str, tuple[type[BaseModel], Callable, str]] = {
    "fetch_settlement_recon": (SettlementQuery, fetch_settlement_recon,
                               "Look up settlements by id or by settled-on date range."),
    "find_subset_sum": (SubsetSumQuery, find_subset_sum,
                        "All subsets (size <= max_k) of the given ids whose amounts sum EXACTLY "
                        "to target_paise. Use it instead of doing arithmetic yourself."),
    "query_ledger": (LedgerFilter, query_ledger, "Search merchant ledger invoices."),
    "search_bank": (BankFilter, search_bank, "Search bank statement lines."),
    "search_payments": (PaymentFilter, search_payments, "Search gateway payment rows."),
}


def tool_specs() -> list[dict]:
    return [{"name": n, "description": d, "input_schema": m.model_json_schema()}
            for n, (m, _, d) in TOOLS.items()]


def run_tool(book: Book, name: str, args: dict) -> tuple[str, bool]:
    """Returns (content, is_error). Errors are returned to the model, never raised."""
    if name not in TOOLS:
        return f"unknown tool {name}", True
    model, fn, _ = TOOLS[name]
    try:
        parsed = model.model_validate(args)
    except ValidationError as exc:
        return f"invalid arguments: {exc.errors(include_url=False)}", True
    try:
        return wrap(f"tool:{name}", fn(book, parsed)), False
    except ValueError as exc:
        return str(exc), True
