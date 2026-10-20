"""Score a finished run against ground truth. Strict: committing anything on an item that
should escalate or should not match is a FALSE match, even if it happens to be 'right'."""

from __future__ import annotations

from collections import defaultdict

from recon.book import Book

AMBIGUOUS_CASES = {"erp_typo", "ambiguous_twin"}


def _blank():
    return {"items": 0, "matchable": 0, "correct": 0, "false": 0, "escalated": 0, "unmatched": 0}


def score(book: Book, truth: dict) -> dict:
    matches = book.store.matches()
    esc_ids = {e.exception_id for e in book.store.escalations()}
    by_item: dict[str, object] = {}
    for m in matches:
        for i in m.left_ids:
            by_item[i] = m

    per_case: dict[str, dict] = defaultdict(_blank)
    rows = []

    for txn_id, t in truth["bank"].items():
        if t["case_type"] == "debit":
            continue
        rows.append(("A", txn_id, t["case_type"], t["expect"],
                     set(t["settlement_ids"] or []), {txn_id}))
    for pid, t in truth["payments"].items():
        rows.append(("B", pid, t["case_type"], t["expect"], set(t["invoice_ids"] or []),
                     set(t["payment_group"])))

    totals = {"A": _blank(), "B": _blank()}
    leakage = 0
    for level, item, case, expect, right_truth, left_truth in rows:
        c, tot = per_case[f"{level}:{case}"], totals[level]
        for d in (c, tot):
            d["items"] += 1
            if expect == "match":
                d["matchable"] += 1
        m = by_item.get(item)
        if m is not None:
            ok = (expect == "match" and set(m.right_ids) == right_truth
                  and set(m.left_ids) == left_truth)
            for d in (c, tot):
                d["correct" if ok else "false"] += 1
            if case == "negative_control":
                leakage += 1
        elif item in esc_ids:
            for d in (c, tot):
                d["escalated"] += 1
        else:
            for d in (c, tot):
                d["unmatched"] += 1

    escalations = book.store.escalations()
    esc_precise = 0
    for e in escalations:
        t = truth["bank"].get(e.exception_id) or truth["payments"].get(e.exception_id)
        if t and (t["expect"] != "match" or t["case_type"] in AMBIGUOUS_CASES):
            esc_precise += 1

    def rates(d):
        committed = d["correct"] + d["false"]
        return {**d,
                "false_match_rate": round(d["false"] / committed, 4) if committed else 0.0,
                "auto_match_rate": round(d["correct"] / d["matchable"], 4) if d["matchable"] else 0.0}

    return {
        "levels": {k: rates(v) for k, v in totals.items()},
        "per_case": {k: rates(v) for k, v in sorted(per_case.items())},
        "negative_control_leakage": leakage,
        "escalations": len(escalations),
        "escalation_precision": round(esc_precise / len(escalations), 4) if escalations else None,
        "by_source": {src: sum(1 for m in matches if m.source == src)
                      for src in ("deterministic", "agent", "human")},
    }


def markdown(result: dict, title: str = "") -> str:
    out = [f"### {title}"] if title else []
    out.append("| scope | items | matchable | correct | false | escalated | unmatched | "
               "auto-match | false-match |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for name, d in list(result["levels"].items()) + list(result["per_case"].items()):
        out.append(f"| {name} | {d['items']} | {d['matchable']} | {d['correct']} | {d['false']} "
                   f"| {d['escalated']} | {d['unmatched']} | {d['auto_match_rate']:.1%} "
                   f"| {d['false_match_rate']:.1%} |")
    out.append("")
    out.append(f"negative-control leakage: **{result['negative_control_leakage']}** · "
               f"escalations: {result['escalations']} · escalation precision: "
               f"{result['escalation_precision']}")
    return "\n".join(out)
