"""ReconAgent UI: month-end for a small fee-collecting organisation.

    uv run streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from eval.metrics import score  # noqa: E402
from recon import gaps, pipeline, report  # noqa: E402
from recon.ingest import IngestError, load_bank, load_ledger, load_recon  # noqa: E402
from recon.llm_cache import _load_env_file  # noqa: E402
from recon.money import fmt_inr  # noqa: E402

SAMPLE = ROOT / "data" / "samples" / "demo"
USAGE = ROOT / "data" / "runs" / "usage.jsonl"
PRICE_PER_MTOK = (3.0, 15.0)  # USD per million input/output tokens (Sonnet list price; check before quoting)
st.set_page_config(page_title="ReconAgent", page_icon="🧾", layout="wide")


def cost_usd(usage: dict) -> float:
    return usage.get("input", 0) / 1e6 * PRICE_PER_MTOK[0] + usage.get("output", 0) / 1e6 * PRICE_PER_MTOK[1]


def record_usage(kind: str, book, info: dict) -> None:
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    with USAGE.open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), "kind": kind, "payments": len(book.payments),
                            "bank_lines": len(book.bank), "invoices": len(book.invoices),
                            "matched": len(book.store.matches())}) + "\n")


def usage_counts() -> dict:
    if not USAGE.exists():
        return {"sample": 0, "upload": 0}
    kinds = [json.loads(line)["kind"] for line in USAGE.read_text().splitlines() if line.strip()]
    return {"sample": kinds.count("sample"), "upload": kinds.count("upload")}


@st.cache_resource(show_spinner="Reconciling the sample month…")
def sample_run():
    db = Path(tempfile.mkdtemp()) / "demo.db"
    book, info = pipeline.run(SAMPLE, db, use_agent=True, offline=True)
    info["replayed"] = True
    return book, info


def amount_of(book, level, ids, side):
    if level == "A":
        return sum(book.bank[i].credit_paise for i in ids) if side == "L" else \
            sum(book.settlements[i].amount for i in ids)
    return sum(book.rows[i].amount for i in ids) if side == "L" else \
        sum(book.invoices[i].amount_paise for i in ids)


# ------------------------------------------------------------------ data source

st.sidebar.title("🧾 ReconAgent")
mode = st.sidebar.radio("Data", ["Sample month (synthetic school)", "Your own files"])
book = info = truth = None

if mode.startswith("Sample"):
    book, info = sample_run()
    truth = pipeline.load_truth(SAMPLE)
    if not st.session_state.get("sample_counted"):
        record_usage("sample", book, info)
        st.session_state["sample_counted"] = True
    default_card = gaps.SAMPLE_CONTRACT
else:
    default_card = gaps.FLAT_2PCT
    st.sidebar.caption("Files are processed in memory for this session and not stored. "
                       "Names and phone numbers are masked before anything reaches the AI.")
    up_recon = st.sidebar.file_uploader("Razorpay settlement recon report (CSV)", type="csv")
    up_bank = st.sidebar.file_uploader("Bank statement (CSV, any bank format)", type="csv")
    up_ledger = st.sidebar.file_uploader("Fee ledger / invoices (CSV)", type="csv")
    _load_env_file()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    live = st.sidebar.checkbox("Let the AI agent investigate exceptions", value=has_key, disabled=not has_key,
                               help="Up to 25 exceptions per upload go to the agent; the rest wait for a human.")
    if not (up_recon and up_bank and up_ledger):
        st.title("Reconcile your own month")
        st.markdown("Upload three exports in the sidebar. Not sure of the formats? Download the "
                    "sample files and compare:")
        cols = st.columns(3)
        for c, name in zip(cols, ("razorpay_recon.csv", "bank_statement_B.csv", "ledger.csv")):
            c.download_button(f"Sample {name}", (SAMPLE / name).read_bytes(), file_name=name)
        st.stop()

    key = (up_recon.file_id, up_bank.file_id, up_ledger.file_id, live)
    if st.session_state.get("upload_key") != key:
        tmp = Path(tempfile.mkdtemp())
        paths = {}
        for role, f in (("recon", up_recon), ("bank", up_bank), ("ledger", up_ledger)):
            paths[role] = tmp / f"{role}.csv"
            paths[role].write_bytes(f.getvalue())
        # Format preview: parse everything first and show what was understood.
        st.title("Check your files")
        try:
            rows, setts = load_recon(paths["recon"])
            txns, source = load_bank(paths["bank"])
            invs = load_ledger(paths["ledger"])
        except IngestError as e:
            st.error(f"Could not read the files: {e}")
            st.stop()
        except Exception as e:  # noqa: BLE001 - a stranger's file can fail in any way
            st.error(f"Could not read the files ({type(e).__name__}: {e}). Compare with the sample formats.")
            st.stop()
        c = st.columns(3)
        c[0].metric("Razorpay rows", len(rows), f"{len(setts)} settlements", delta_color="off")
        c[1].metric("Bank lines", len(txns), f"format detected: {source}", delta_color="off")
        c[2].metric("Invoices", len(invs))
        st.caption("Bank statement as understood (running balance verified on every line):")
        st.dataframe(pd.DataFrame([{"date": t.date, "narration": t.narration[:60], "credit": fmt_inr(t.credit_paise),
                                    "debit": fmt_inr(t.debit_paise), "balance": fmt_inr(t.balance_paise)}
                                   for t in txns[:6]]), hide_index=True, width="stretch")
        if not st.button("Looks right: reconcile", type="primary"):
            st.stop()
        with st.spinner("Matching… deterministic rules first, then the agent on what's left"):
            b, i = pipeline.run_files(paths["recon"], paths["bank"], paths["ledger"], tmp / "run.db",
                                      use_agent=live, offline=not live)
        record_usage("upload", b, i)
        st.session_state.update(upload_key=key, upload_book=b, upload_info=i)
        st.rerun()
    book, info = st.session_state["upload_book"], st.session_state["upload_info"]

st.sidebar.divider()
page = st.sidebar.radio("View", ["Overview", "Month-end report", "Settlement gaps", "Matches",
                                 "Needs a human", "How it works"])
u = info.get("usage") or {}
st.sidebar.caption(
    f"Run: {info.get('seconds', 0)} s · {u.get('calls', 0)} AI calls · "
    + (f"replayed from cache (free; ~${cost_usd(u):.2f} if run live)" if info.get("replayed")
       else f"~${cost_usd(u):.2f}"))
uc = usage_counts()
st.sidebar.caption(f"Runs on this server: {uc['sample']} sample, {uc['upload']} uploads")

matches = book.store.matches()
escalations = book.store.escalations()
card = st.session_state.get("rate_card", default_card)
gap = gaps.explain(book, card)
lines, unplaced = report.month_end(book)
credits = book.credits()
explained = {t for m in matches if m.level == "A" for t in m.left_ids}

# ------------------------------------------------------------------ pages

if page == "Overview":
    st.title("Month-end, done in minutes")
    st.markdown(
        "**The problem.** A school, coaching centre, clinic or housing society collects fees through "
        "Razorpay, UPI and bank transfer, and has no accountant. At month end someone must answer three "
        "questions by hand: *who paid, who didn't, and why doesn't the bank match Razorpay?* Bank lines "
        "say `NEFT-AXISN…-RAZORPAY`, settlements bundle dozens of payments minus fees, GST, refunds and "
        "chargebacks, and parents pay with no reference. It takes days, and mistakes mean chasing a parent "
        "who already paid.")
    c = st.columns(4)
    c[0].metric("Bank credits explained", f"{len(explained & {t.txn_id for t in credits})}/{len(credits)}")
    c[1].metric("Invoices paid", sum(line.status == "paid" for line in lines),
                f"{sum(line.status != 'paid' for line in lines)} to chase", delta_color="off")
    c[2].metric("Fees above contract", fmt_inr(gap.leak_paise))
    c[3].metric("Needs a human", len(escalations) + len(info.get("pending", [])))
    st.markdown(
        "**How it decides.** Plain rules clear the easy matches. An AI agent investigates the rest "
        "with tools (search candidates, check UTRs, propose a match). A strict verifier re-checks "
        "every proposal with exact arithmetic before it counts, and a second pass tries to find an "
        "equally valid alternative. Anything it can't prove goes to a person, never a guess.")
    if truth:
        res = score(book, truth)
        lv = res["levels"]
        st.subheader("On this sample month, against the known answer")
        st.write(f"Bank ↔ settlement: **{lv['A']['auto_match_rate']:.0%}** matched automatically, "
                 f"**{lv['A']['false']}** wrong · Payment ↔ invoice: **{lv['B']['auto_match_rate']:.0%}**, "
                 f"**{lv['B']['false']}** wrong · decoys leaked: **{res['negative_control_leakage']}**")
    st.caption("Built first: matching you can trust (zero false matches), then explanations a "
               "non-accountant can act on. Left out on purpose: logins, saved history, bank and Tally "
               "integrations, multi-organisation support.")

elif page == "Month-end report":
    st.title("Month-end report")
    df = pd.DataFrame([{"invoice": x.invoice_id, "customer": x.customer, "amount": fmt_inr(x.amount),
                        "status": x.status, "paid": fmt_inr(x.paid), "due": x.due_on, "proof": x.proof}
                       for x in lines])
    c = st.columns(3)
    c[0].metric("Paid", (df.status == "paid").sum())
    c[1].metric("Partly paid", (df.status == "partly paid").sum())
    c[2].metric("Unpaid (chase)", (df.status == "unpaid").sum())
    show = st.multiselect("Show", ["unpaid", "partly paid", "paid"], ["unpaid", "partly paid"])
    st.dataframe(df[df.status.isin(show)], hide_index=True, width="stretch",
                 column_config={"proof": st.column_config.TextColumn(
                     "proof (payment → settlement → bank credit [rule])", width="large")})
    chase = df[df.status != "paid"]
    st.download_button("Download chase list (CSV)", chase.to_csv(index=False), "chase_list.csv")
    st.subheader(f"Payments nobody could place yet ({len(unplaced)})")
    st.caption("Money that arrived with no invoice it can be proven to belong to. These go to a person "
               "with the agent's candidate explanations (see Needs a human).")
    if unplaced:
        st.dataframe(pd.DataFrame([{**p, "amount": fmt_inr(p["amount"])} for p in unplaced]),
                     hide_index=True, width="stretch")

elif page == "Settlement gaps":
    st.title("Settlement gaps: every rupee explained")
    t = gap.totals
    st.markdown(
        f"Customers paid **{fmt_inr(t['gross'])}** → gateway fees **−{fmt_inr(t['fees'])}** → "
        f"GST on fees **−{fmt_inr(t['gst'])}** → refunds **−{fmt_inr(t['refunds'])}** → "
        f"chargebacks **−{fmt_inr(t['chargebacks'])}**"
        + (f" → other **−{fmt_inr(t['other'])}**" if t["other"] else "")
        + f" → settled **{fmt_inr(t['net'])}**")
    c = st.columns(3)
    c[0].metric("Settlements reached the bank", f"{sum(g.status == 'reached bank' for g in gap.groups)}/{len(gap.groups)}")
    c[1].metric("Settled but not found in bank", fmt_inr(gap.unreceived_paise))
    c[2].metric("Fees above contract", fmt_inr(gap.leak_paise), f"{sum(f.excess > 0 for f in gap.findings)} payments",
                delta_color="off")

    with st.expander("Your contracted rates (edit to your plan)", expanded=mode.startswith("Sample")):
        if mode.startswith("Sample"):
            st.caption("Sample school's contract (synthetic): UPI 0%, net banking 1.75%, cards 2%.")
        ed = st.data_editor(pd.DataFrame([{"method": k, "rate %": v / 100} for k, v in card.items()]),
                            hide_index=True, disabled=["method"], key="card_editor")
        new = {r["method"]: int(round(float(r["rate %"]) * 100)) for r in ed.to_dict("records")}
        if new != card:
            st.session_state["rate_card"] = new
            st.rerun()

    st.subheader("Per settlement")
    st.dataframe(pd.DataFrame([{
        "settled": g.settled_on, "settlement": ", ".join(g.settlement_ids), "gross": fmt_inr(g.gross),
        "fees+GST": fmt_inr(g.fees + g.gst), "refunds": fmt_inr(g.refunds), "chargebacks": fmt_inr(g.chargebacks),
        "net": fmt_inr(g.net), "bank": fmt_inr(g.bank_amount) if g.bank_amount is not None else "—",
        "status": g.status, "bank line": ", ".join(g.bank_txn_ids), "detail": "; ".join(g.evidence)}
        for g in gap.groups]), hide_index=True, width="stretch")

    st.subheader("Fee audit")
    if gap.findings:
        st.dataframe(pd.DataFrame([{
            "payment": f.entity_id, "method": f.method, "amount": fmt_inr(f.amount),
            "fee charged": fmt_inr(f.fee_charged), "fee per contract": fmt_inr(f.fee_expected),
            "GST charged": fmt_inr(f.tax_charged), "excess": fmt_inr(f.excess), "finding": f.kind,
            "settlement": f.settlement_id} for f in gap.findings]), hide_index=True, width="stretch")
    else:
        st.success("Every fee matches the contracted rate.")
    st.subheader("Ticket to raise")
    tk = gaps.ticket(gap, "our account")
    st.text_area("Copy and send", tk, height=280)
    st.download_button("Download ticket", tk, "razorpay_ticket.txt")

elif page == "Matches":
    st.title("Matches")
    rows = [{"match": m.match_id, "level": m.level, "source": m.source, "rule": m.proof.rule,
             "left": ", ".join(m.left_ids), "right": ", ".join(m.right_ids),
             "amount": fmt_inr(amount_of(book, m.level, m.left_ids, "L")),
             "equation": m.proof.equation} for m in matches]
    df = pd.DataFrame(rows)
    st.caption("Level A: bank credit ↔ Razorpay settlement(s). Level B: payment(s) ↔ invoice(s).")
    src = st.multiselect("Source", ["deterministic", "agent", "human"], ["agent", "human"])
    view = df[df["source"].isin(src)] if len(df) else df
    st.dataframe(view, hide_index=True, width="stretch")
    pick = st.selectbox("Proof trace for", view["match"].tolist() if len(view) else [])
    if pick:
        m = next(x for x in matches if x.match_id == pick)
        st.code(m.proof.equation)
        st.write(m.proof.detail)
        trace = book.store.trace(m.left_ids[0])
        if trace:
            for step in trace:
                with st.expander(step.get("step", "?")):
                    st.json(step)
        else:
            st.caption("Deterministic match: the rule and equation above are the whole proof.")

elif page == "Needs a human":
    st.title("Needs a human")
    st.caption("Each case shows every explanation that passed the verifier. Approving one re-runs the "
               "same verifier before it commits.")
    if info.get("pending"):
        st.info(f"{len(info['pending'])} exceptions were not sent to the agent (cost cap or no API key) "
                "and are waiting for review.")
    if not escalations:
        st.success("Nothing waiting for review.")
    for e in escalations:
        with st.container(border=True):
            st.markdown(f"**{e.exception_id}** · level {e.level}")
            st.write(e.reason)
            cols = st.columns(max(1, len(e.explanations)))
            for i, h in enumerate(e.explanations):
                with cols[i]:
                    st.markdown(f"**Explanation {i + 1}**: `{h.pattern}`")
                    right = h.settlement_ids if e.level == "A" else h.invoice_ids
                    st.write(", ".join(right))
                    st.caption(h.rationale[:400])
                    if st.button("Approve", key=f"{e.exception_id}-{i}"):
                        v = pipeline.approve(book, e.exception_id, i)
                        st.error("; ".join(v)) if v else st.rerun()
            st.caption("Reject both = leave it here; nothing is committed.")

else:
    st.title("How it works")
    st.code("""retrieve_candidates -> hypothesize -> verify --pass--> challenge --no valid alternative--> commit
                           ^             |                 |
                           |---fail------|                 '--alternative also verifies--> escalate
                       (attempts < 3)    '--attempts exhausted / declared unmatchable--> escalate""")
    st.markdown((ROOT / "README.md").read_text().split("## Architecture", 1)[-1].split("\n## ", 1)[0]
                if (ROOT / "README.md").exists() else "")
