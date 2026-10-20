# Decisions

Short log of design choices, deviations from the original spec, and what was cut.

## Data model

- **Recon rows mirror Razorpay's Settlement Recon API** (`entity_id, type, debit, credit, amount,
  fee, tax, settlement_id, settlement_utr, order_receipt, notes, …`). Payment rows carry
  `credit = amount − fee − tax`; refund and chargeback rows carry `debit`. Settlement amount =
  Σcredit − Σdebit, recomputed and checked at ingest.
- **Level B is n:m with exact sums**, not partial allocation. A hypothesis is a set of payments
  and a set of invoices whose totals are equal. This one shape covers instalments (n payments →
  1 invoice), siblings and lender lump sums (1 payment → n invoices) without an allocation table,
  and keeps the verifier's arithmetic exact.
- **Settlement amounts are authoritative at Level A.** The gateway already nets fees, GST,
  refunds and chargebacks, so a bank credit is matched against settlement amounts. Adjustments in
  a hypothesis are explanatory: they must still trace exactly to a real row in a cited settlement,
  so they can never be used as a plug — but they don't enter the arithmetic. This is also why
  `netted_refund` and `chargeback` are easy at Level A against the recon API: the hard part of
  refunds for a merchant reconciling from their own sales register moves to Level B.
- **The pattern list is the spec's, unchanged.** The agent picks the closest one; the verifier
  never relies on the pattern field, so a mislabelled pattern can't cause a wrong commit.
- **`name_only` is an extra chaos case** (payment link with only a student's name, no customer
  id). It is common in Indian school-fee collection and is where most legitimate agent work is.

## Matching

- **Deterministic passes commit only unique explanations** — an explanation commits only if none
  of its ids appears in any other candidate explanation, and it passes the same verifier the
  agent uses. Customer-scoped at Level B.
- **Bounded DFS instead of a paise DP for subset-sum.** Amounts are lakhs of rupees in paise; a DP
  table over paise would need ~10⁹ cells. A pruned DFS with k ≤ 5 over tens of candidates is exact
  and returns *all* solutions, which is what uniqueness needs.
- **Name evidence is a contiguous full-name phrase** (plus a 95-point fuzzy fallback for one
  typo). Stricter than "customer consistency" in the spec: lump-sum payments with a documented
  payer must still name every student, otherwise subset-sum over all open invoices would find
  wrong combinations.
- **The challenger has two halves.** An exhaustive search over the retrieved counterparts (cheap,
  catches equal-amount twins and same-name collisions) runs before the LLM challenger. The
  `--no-challenger` ablation disables both.
- **`declare_unmatchable` tool.** Not in the spec's graph, but without it the agent burns all
  three attempts on negative controls and typos. It routes straight to escalate.

## Ingest

- **Format A is hard-coded; B and C go through `infer_statement_schema`**, which tries the
  cached schema for that header fingerprint, then a deterministic search over column-name
  synonyms × date formats, then the LLM (Haiku) with the failing rows fed back, max 3 attempts,
  then `HumanNeeded`. Every source is gated by the same running-balance invariant. On the
  synthetic formats the deterministic search succeeds, so the LLM path is exercised by a unit test
  rather than the eval.
- **Duplicate export lines are detected by the invariant**: an exact repeat including the balance
  is flagged `duplicate_of` and excluded from the invariant and from matching.

## Human queue

- `escalate` calls LangGraph `interrupt()` when `human_in_loop=True` (tested with an in-memory
  checkpointer). The Streamlit UI approves through `pipeline.approve`, which re-runs the same
  verifier before committing as `source=human`. A durable SQLite checkpointer across UI restarts
  is not wired.

## Cost

- Default eval size is **250 payments × 3 seeds** rather than 1,500: residue is ~15–20% of
  payments and each exception is several model calls, so 1,500 × 5 seeds would cost tens of
  dollars per ablation sweep. The generator supports 1,500; the ratios hold at the smaller size.
- All LLM responses are cached by `hash(model, system, messages, tools)`, so re-runs and the demo
  are free and reproducible.

## Bugs the guardrails caught while building this

1. **Cross-student name match.** The first name-evidence rule checked that every name token
   appeared somewhere in the notes. A lender lump-sum listing "Myra Pillai, …, Saanvi Sharma" was
   therefore "evidence" for an unrelated "Myra Sharma". The **challenger** caught it — it found a
   second verifying explanation and escalated — before it could become a false match. Fixed by
   requiring a contiguous phrase.
2. **Decoy credit leak in the deterministic pass.** On one seed of five, an unrelated
   self-transfer with exactly the same amount as a real settlement was committed by L2. The real
   credit had landed after a bank holiday, outside L2's strict window, so inside the window the
   decoy looked unique. Fixed by counting competing claimants over the full verifier window. The
   eval's negative-control leakage metric surfaced it; it now reads 0 across 10 seeds.

## Cut

PDF statements, multi-merchant, live test-mode pull, Tally/Zoho export, embeddings — see README.
