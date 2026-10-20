"""Money and calendar rules. The single source of truth for every amount and date window.

All money is integer paise. Nothing in this module (or anywhere else) accepts a float.
"""

from __future__ import annotations

from datetime import date, timedelta

# Synthetic bank holidays for the demo year. Small and hard-coded on purpose: the
# point is that settlement windows must be holiday-aware, not that this calendar is
# complete. A production system would load the RBI/NPCI holiday calendar.
HOLIDAYS: frozenset[date] = frozenset({
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 27),   # Ganesh Chaturthi (synthetic)
    date(2026, 9, 14),   # synthetic mid-month bank holiday
    date(2026, 10, 2),   # Gandhi Jayanti
})

FEE_BPS = 200            # gateway fee: 2.00% of the payment amount
GST_PERCENT = 18         # GST charged on the fee
TAX_TOLERANCE_PAISE = 1  # the ONLY tolerance in the system, and only for the GST rule

SETTLEMENT_T_PLUS = 2         # settlement initiated T+2 business days after capture
MAX_CREDIT_LAG_BUSINESS = 4   # bank credit may land up to this many business days later


def require_paise(value: object, name: str = "amount") -> int:
    """Reject anything that is not a plain int. bool is an int subclass, so reject it too."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int paise, got {type(value).__name__}: {value!r}")
    return value


def round_half_up_div(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero, for non-negative numerators."""
    require_paise(numerator, "numerator")
    if numerator < 0:
        raise ValueError("round_half_up_div expects a non-negative numerator")
    return (2 * numerator + denominator) // (2 * denominator)


def fee_for(amount: int) -> int:
    return round_half_up_div(require_paise(amount) * FEE_BPS, 10_000)


def gst_on_fee(fee: int) -> int:
    """tax = round_half_up(fee * 18 / 100), in paise."""
    return round_half_up_div(require_paise(fee, "fee") * GST_PERCENT, 100)


def tax_matches(fee: int, tax: int) -> bool:
    return abs(gst_on_fee(fee) - require_paise(tax, "tax")) <= TAX_TOLERANCE_PAISE


def payment_net(amount: int, fee: int, tax: int) -> int:
    """What the merchant actually receives for one payment row."""
    return require_paise(amount) - require_paise(fee, "fee") - require_paise(tax, "tax")


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def add_business_days(d: date, n: int) -> date:
    out = d
    added = 0
    while added < n:
        out += timedelta(days=1)
        if is_business_day(out):
            added += 1
    return out


def business_days_between(start: date, end: date) -> int:
    """Business days strictly after `start` up to and including `end` (0 if end <= start)."""
    if end <= start:
        return 0
    count, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if is_business_day(cur):
            count += 1
    return count


def credit_within_window(settled_on: date, credit_on: date) -> bool:
    """A bank credit belongs to a settlement only if it lands on or after the settlement
    date and no more than MAX_CREDIT_LAG_BUSINESS business days later (holiday-aware)."""
    if credit_on < settled_on:
        return False
    return business_days_between(settled_on, credit_on) <= MAX_CREDIT_LAG_BUSINESS


def fmt_inr(paise: int) -> str:
    """₹1,23,456.78 — Indian digit grouping, for display only."""
    require_paise(paise)
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}₹{s}.{p:02d}"
