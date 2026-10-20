"""Domain models. Every money field is StrictInt paise: pydantic rejects a float outright."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Paise = StrictInt


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------- source data

class ReconRow(Frozen):
    """One row of the settlement recon report (mirrors Razorpay's combined recon API)."""
    entity_id: str
    type: Literal["payment", "refund", "adjustment"]
    debit: Paise
    credit: Paise
    amount: Paise
    currency: str = "INR"
    fee: Paise
    tax: Paise
    on_hold: bool = False
    settled: bool = True
    created_at: date
    settled_at: date
    settlement_id: str
    settlement_utr: str
    description: str = ""
    notes: dict[str, str] = Field(default_factory=dict)
    payment_id: str | None = None      # for refunds/adjustments: the payment they act on
    order_id: str | None = None
    order_receipt: str | None = None
    method: str | None = None
    dispute_id: str | None = None


class Settlement(Frozen):
    settlement_id: str
    utr: str
    settled_on: date
    amount: Paise
    row_ids: tuple[str, ...]


class BankTxn(Frozen):
    txn_id: str
    date: date
    narration: str
    debit_paise: Paise
    credit_paise: Paise
    balance_paise: Paise
    utr: str | None = None
    utr_confidence: Literal["full", "partial", "none"] = "none"
    duplicate_of: str | None = None   # set at ingest when the export repeated a line


class Invoice(Frozen):
    invoice_id: str
    customer_id: str
    customer_name: str
    amount_paise: Paise
    issued_on: date
    due_on: date
    status: Literal["open", "paid", "cancelled"] = "open"
    receipt_ref: str | None = None


# --------------------------------------------------------------- matching

Level = Literal["A", "B"]   # A: bank credit <-> settlement(s); B: payment(s) <-> invoice(s)

Pattern = Literal[
    "netted_refund", "split_settlement", "fee_gst_deduction", "chargeback",
    "partial_payment", "many_to_one", "lump_sum", "holiday_shift", "utr_truncated",
]


class Adjustment(Frozen):
    kind: Literal["fee", "tax", "refund", "chargeback"]
    source_entity_id: str          # MUST reference a real recon row
    amount_paise: Paise


class Hypothesis(Frozen):
    pattern: Pattern
    bank_txn_ids: list[str] = Field(default_factory=list)
    recon_entity_ids: list[str] = Field(default_factory=list)   # Level B: the payment ids
    settlement_ids: list[str] = Field(default_factory=list)
    invoice_ids: list[str] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    rationale: str = ""

    def key(self) -> tuple:
        """Identity of the explanation, ignoring wording — used to tell alternatives apart."""
        return (tuple(sorted(self.bank_txn_ids)), tuple(sorted(self.settlement_ids)),
                tuple(sorted(self.recon_entity_ids)), tuple(sorted(self.invoice_ids)))


class Proof(Frozen):
    rule: str                      # "L1_utr", "L2_amount", "L3_subset_sum", "agent", "human"
    equation: str                  # human-readable arithmetic, e.g. "118000 = 118000"
    ids: tuple[str, ...]
    detail: str = ""


class Match(Frozen):
    match_id: str
    level: Level
    left_ids: tuple[str, ...]      # A: bank txn ids · B: payment ids
    right_ids: tuple[str, ...]     # A: settlement ids · B: invoice ids
    source: Literal["deterministic", "agent", "human"]
    proof: Proof
    pattern: str | None = None


class Escalation(Frozen):
    level: Level
    exception_id: str
    reason: str
    explanations: list[Hypothesis] = Field(default_factory=list)
