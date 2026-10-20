"""ReconAgent UI. Function over looks.

    uv run streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from eval.metrics import score  # noqa: E402
from recon import pipeline  # noqa: E402
from recon.money import fmt_inr  # noqa: E402

SAMPLE = ROOT / "data" / "samples" / "demo"
st.set_page_config(page_title="ReconAgent", layout="wide")


@st.cache_resource(show_spinner="Reconciling the sample month…")
def sample_run():
    db = Path(tempfile.mkdtemp()) / "demo.db"
    book, info = pipeline.run(SAMPLE, db, use_agent=True, offline=True)
    return book, info


def uploaded_run(files):
    tmp = Path(tempfile.mkdtemp())
    for name, f in files.items():
        (tmp / name).write_bytes(f.getvalue())
    return pipeline.run(tmp, tmp / "run.db", use_agent=st.session_state.get("live", False),
                        offline=not st.session_state.get("live", False))


def amount_of(book, level, ids, side):
    if level == "A":
        return sum(book.bank[i].credit_paise for i in ids) if side == "L" else \
            sum(book.settlements[i].amount for i in ids)
    return sum(book.rows[i].amount for i in ids) if side == "L" else \
        sum(book.invoices[i].amount_paise for i in ids)


page = st.sidebar.radio("Page", ["Overview", "Matches", "Escalations", "How it works"])
mode = st.sidebar.radio("Data", ["Sample month (cached, free)", "Upload your own"])

if mode == "Upload your own":
    st.sidebar.caption("Upload razorpay_recon.csv, bank_statement_A.csv and ledger.csv "
                       "(synthetic data only).")
    up = {n: st.sidebar.file_uploader(n, type="csv", key=n)
          for n in ("razorpay_recon.csv", "bank_statement_A.csv", "ledger.csv")}
    st.session_state["live"] = st.sidebar.checkbox("Run the agent live (uses an API key)", False)
    if not all(up.values()):
        st.info("Upload all three files to run.")
        st.stop()
    book, info = uploaded_run(up)
    truth = None
else:
    book, info = sample_run()
    truth = pipeline.load_truth(SAMPLE)

matches = book.store.matches()
escalations = book.store.escalations()

if page == "Overview":
    st.title("ReconAgent")
    st.caption("Which invoices did this bank credit actually pay? Deterministic rules clear "
               "the easy cases; an agent handles the residue — but the LLM proposes, arithmetic "
               "decides, and an adversary tries to falsify every match before it commits.")
    c = st.columns(5)
    c[0].metric("Matched (deterministic)", sum(m.source == "deterministic" for m in matches))
    c[1].metric("Matched (agent)", sum(m.source == "agent" for m in matches))
    c[2].metric("Matched (human)", sum(m.source == "human" for m in matches))
    c[3].metric("Escalated", len(escalations))
    c[4].metric("Pending (uncached)", len(info.get("pending", [])))
    if info.get("pending"):
        st.warning(f"{len(info['pending'])} exceptions have no cached agent run and were left "
                   "pending. Run the eval with an API key to fill the cache.")
    if truth:
        res = score(book, truth)
        st.subheader("Scored against ground truth")
        rows = [{"scope": k, **{x: v[x] for x in ("items", "matchable", "correct", "false",
                                                    "escalated", "unmatched")},
                 "auto-match": f"{v['auto_match_rate']:.1%}",
                 "false-match": f"{v['false_match_rate']:.1%}"}
                for k, v in list(res["levels"].items()) + list(res["per_case"].items())]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.write(f"Negative-control leakage: **{res['negative_control_leakage']}** · "
                 f"escalation precision: {res['escalation_precision']}")
    abl = ROOT / "results" / "ablations.md"
    if abl.exists():
        st.subheader("Ablations")
        st.markdown(abl.read_text())

elif page == "Matches":
    st.title("Matches")
    rows = [{"match": m.match_id, "level": m.level, "source": m.source, "rule": m.proof.rule,
             "left": ", ".join(m.left_ids), "right": ", ".join(m.right_ids),
             "amount": fmt_inr(amount_of(book, m.level, m.left_ids, "L")),
             "equation": m.proof.equation} for m in matches]
    df = pd.DataFrame(rows)
    src = st.multiselect("Source", ["deterministic", "agent", "human"], ["agent", "human"])
    view = df[df["source"].isin(src)] if len(df) else df
    st.dataframe(view, hide_index=True, use_container_width=True)
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
            st.caption("Deterministic match — the rule and equation above are the whole proof.")

elif page == "Escalations":
    st.title("Escalations")
    st.caption("Each escalation shows every explanation that passed the verifier. Approving one "
               "re-runs the same verifier before it commits.")
    if not escalations:
        st.success("Nothing waiting for review.")
    for e in escalations:
        with st.container(border=True):
            st.markdown(f"**{e.exception_id}** · level {e.level}")
            st.write(e.reason)
            cols = st.columns(max(1, len(e.explanations)))
            for i, h in enumerate(e.explanations):
                with cols[i]:
                    st.markdown(f"**Explanation {i + 1}** — `{h.pattern}`")
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
