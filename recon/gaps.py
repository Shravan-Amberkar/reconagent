"""Settlement gap explainer: every rupee between what customers paid and what reached the bank.

For each Razorpay settlement the waterfall is

    gross collected - gateway fee - GST on fee - refunds - chargebacks/adjustments = settlement

and the settlement is then compared with the bank credit(s) it was matched to. Separately, every
payment's fee is recomputed from the merchant's contracted rate card and GST is recomputed at 18%
of the fee actually charged; anything above contract is a fee leak with the exact rows as proof.

All arithmetic is integer paise and deterministic. The agent's job ends at matching; this module
only explains, so its output is reproducible and auditable line by line.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recon.book import Book
from recon.money import fmt_inr, gst_on_fee

# Basis points of the payment amount, per payment method.
FLAT_2PCT = {"card": 200, "netbanking": 200, "upi": 200, "wallet": 200, "emi": 200, "other": 200}
# The demo school's (synthetic) contract. Edit in the UI to your own plan.
SAMPLE_CONTRACT = {"card": 200, "netbanking": 175, "upi": 0, "wallet": 200, "emi": 200, "other": 200}
TOLERANCE_PAISE = 100  # ₹1 rounding slack per payment


@dataclass
class FeeFinding:
    entity_id: str
    method: str
    amount: int
    fee_charged: int
    fee_expected: int
    tax_charged: int
    tax_expected: int
    settlement_id: str

    @property
    def excess(self) -> int:
        """Paise charged above contract (fee plus the GST on that excess)."""
        return (self.fee_charged + self.tax_charged) - (self.fee_expected + self.tax_expected)

    @property
    def kind(self) -> str:
        fee_off = abs(self.fee_charged - self.fee_expected) > TOLERANCE_PAISE
        tax_off = abs(self.tax_charged - gst_on_fee(self.fee_charged)) > TOLERANCE_PAISE
        if fee_off and self.fee_charged > self.fee_expected:
            return "fee above contract"
        if fee_off:
            return "fee below contract"
        if tax_off:
            return "GST not 18% of fee"
        return "ok"


@dataclass
class SettlementGap:
    settlement_ids: list[str]
    utrs: list[str]
    settled_on: str
    gross: int = 0
    fees: int = 0
    gst: int = 0
    refunds: int = 0
    chargebacks: int = 0
    other: int = 0
    net: int = 0
    bank_txn_ids: list[str] = field(default_factory=list)
    bank_amount: int | None = None
    status: str = ""  # reached bank | not found in bank | amount differs
    evidence: list[str] = field(default_factory=list)

    @property
    def gap(self) -> int | None:
        return None if self.bank_amount is None else self.bank_amount - self.net


@dataclass
class GapReport:
    groups: list[SettlementGap]
    findings: list[FeeFinding]

    @property
    def leak_paise(self) -> int:
        return sum(f.excess for f in self.findings if f.excess > 0)

    @property
    def unreceived_paise(self) -> int:
        return sum(g.net for g in self.groups if g.status == "not found in bank")

    @property
    def totals(self) -> dict[str, int]:
        keys = ("gross", "fees", "gst", "refunds", "chargebacks", "other", "net")
        return {k: sum(getattr(g, k) for g in self.groups) for k in keys}


def _expected_fee(amount: int, method: str | None, card: dict[str, int]) -> int:
    bps = card.get(method or "other", card.get("other", 200))
    return amount * bps // 10_000


def explain(book: Book, rate_card: dict[str, int]) -> GapReport:
    # Group settlements by the bank credit(s) they were matched to (one credit can carry several).
    by_sid: dict[str, list[str]] = {}
    for m in book.store.matches():
        if m.level == "A":
            for sid in m.right_ids:
                by_sid[sid] = list(m.left_ids)
    groups: dict[tuple, SettlementGap] = {}
    for sid, s in sorted(book.settlements.items(), key=lambda kv: (kv[1].settled_on, kv[0])):
        bank_ids = by_sid.get(sid)
        key = tuple(bank_ids) if bank_ids else (sid,)
        g = groups.get(key)
        if g is None:
            g = groups[key] = SettlementGap(settlement_ids=[], utrs=[], settled_on=s.settled_on.isoformat())
            if bank_ids:
                g.bank_txn_ids = bank_ids
                g.bank_amount = sum(book.bank[t].credit_paise for t in bank_ids)
        g.settlement_ids.append(sid)
        g.utrs.append(s.utr)
        for rid in s.row_ids:
            r = book.rows[rid]
            if r.type == "payment":
                g.gross += r.amount
                g.fees += r.fee
                g.gst += r.tax
            elif r.type == "refund":
                g.refunds += r.debit
                g.evidence.append(f"{rid}: refund {fmt_inr(r.debit)} ({r.description or 'refund'})")
            elif "chargeback" in (r.description or "").lower() or r.dispute_id:
                g.chargebacks += r.debit
                g.evidence.append(f"{rid}: chargeback {fmt_inr(r.debit)}")
            else:
                g.other += r.debit - r.credit
                g.evidence.append(f"{rid}: adjustment {fmt_inr(r.debit - r.credit)} ({r.description})")
        g.net += s.amount
    for g in groups.values():
        if g.bank_amount is None:
            g.status = "not found in bank"
        elif g.bank_amount == g.net:
            g.status = "reached bank"
        else:
            g.status = "amount differs"

    findings = []
    for r in book.payments.values():
        exp = _expected_fee(r.amount, r.method, rate_card)
        f = FeeFinding(entity_id=r.entity_id, method=r.method or "other", amount=r.amount,
                       fee_charged=r.fee, fee_expected=exp, tax_charged=r.tax,
                       tax_expected=gst_on_fee(exp), settlement_id=r.settlement_id)
        if f.kind != "ok":
            findings.append(f)
    findings.sort(key=lambda f: -f.excess)
    return GapReport(groups=list(groups.values()), findings=findings)


def ticket(report: GapReport, merchant: str = "our account") -> str:
    """A support ticket a merchant can send as is. Every figure cites the rows behind it."""
    over = [f for f in report.findings if f.excess > 0]
    missing = [g for g in report.groups if g.status != "reached bank"]
    if not over and not missing:
        return "Nothing to raise: every settlement reached the bank and every fee matches the contract."
    lines = [f"Subject: Fee and settlement discrepancies for {merchant}", "",
             "Hello Razorpay support,", "",
             "Reconciling our settlements against our contracted pricing and bank statement, we found:", ""]
    if over:
        by_method: dict[str, list[FeeFinding]] = {}
        for f in over:
            by_method.setdefault(f.method, []).append(f)
        lines.append(f"1. Fees above our contracted rate, total {fmt_inr(report.leak_paise)} (fee + GST):")
        for method, fs in sorted(by_method.items(), key=lambda kv: -sum(f.excess for f in kv[1])):
            ids = ", ".join(f.entity_id for f in fs[:5]) + (f" and {len(fs) - 5} more" if len(fs) > 5 else "")
            lines.append(f"   - {method}: {len(fs)} payments, excess {fmt_inr(sum(f.excess for f in fs))} "
                         f"(e.g. {ids})")
        lines.append("")
    if missing:
        n = 2 if over else 1
        lines.append(f"{n}. Settlements not matching a bank credit:")
        for g in missing:
            what = "no matching credit found" if g.bank_amount is None else \
                f"bank received {fmt_inr(g.bank_amount)}, expected {fmt_inr(g.net)}"
            lines.append(f"   - {', '.join(g.settlement_ids)} (UTR {', '.join(g.utrs)}, {g.settled_on}): {what}")
        lines.append("")
    lines += ["Please confirm the applicable rates and refund the excess, or share the basis for the",
              "charges, and confirm the status of the settlements above.", "", "Thank you."]
    return "\n".join(lines)
