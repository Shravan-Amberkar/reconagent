"""The working set for one reconciliation run: parsed inputs plus the match store."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from recon.ingest import load_bank, load_ledger, load_recon
from recon.models import BankTxn, Invoice, ReconRow, Settlement
from recon.store import Store


@dataclass
class Book:
    rows: dict[str, ReconRow]
    settlements: dict[str, Settlement]
    bank: dict[str, BankTxn]
    invoices: dict[str, Invoice]
    store: Store = field(default_factory=Store)

    @classmethod
    def load(cls, folder: Path, bank_format: str = "A", store: Store | None = None,
             schema_llm=None) -> "Book":
        folder = Path(folder)
        rows, settlements = load_recon(folder / "razorpay_recon.csv")
        bank, _ = load_bank(folder / f"bank_statement_{bank_format}.csv", llm=schema_llm)
        invoices = load_ledger(folder / "ledger.csv")
        return cls(rows={r.entity_id: r for r in rows}, settlements=settlements,
                   bank={t.txn_id: t for t in bank}, invoices={i.invoice_id: i for i in invoices},
                   store=store or Store())

    @classmethod
    def from_files(cls, recon: Path, bank: Path, ledger: Path, store: Store | None = None,
                   schema_llm=None) -> tuple["Book", str]:
        """Load from explicit paths (uploads arrive with any file name). Returns the bank-format
        source too: format_a | cache | heuristic | llm."""
        rows, settlements = load_recon(Path(recon))
        txns, source = load_bank(Path(bank), llm=schema_llm)
        invoices = load_ledger(Path(ledger))
        return cls(rows={r.entity_id: r for r in rows}, settlements=settlements,
                   bank={t.txn_id: t for t in txns}, invoices={i.invoice_id: i for i in invoices},
                   store=store or Store()), source

    # ---- views

    @property
    def payments(self) -> dict[str, ReconRow]:
        return {k: r for k, r in self.rows.items() if r.type == "payment"}

    def credits(self) -> list[BankTxn]:
        """Bank credits that could belong to a settlement (debits and export duplicates excluded)."""
        return [t for t in self.bank.values() if t.credit_paise > 0 and t.duplicate_of is None]

    def free(self, item_id: str) -> bool:
        return not self.store.is_consumed(item_id)

    def open_invoices(self) -> list[Invoice]:
        return [i for i in self.invoices.values() if i.status == "open" and self.free(i.invoice_id)]

    def customer_id(self, payment: ReconRow) -> str | None:
        return payment.notes.get("customer_id")

    def notes_text(self, payment: ReconRow) -> str:
        return " | ".join(f"{k}: {v}" for k, v in sorted(payment.notes.items()))
