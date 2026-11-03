"""ReconAgent web API + app.

    uv run uvicorn app.api:app --port 8501

The sample month is reconciled once at startup from the recorded agent run (no API key needed).
Uploaded files are processed in a temporary directory for the session and never kept.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from eval.metrics import score
from recon import gaps, pipeline, report
from recon.ingest import IngestError, load_bank, load_ledger, load_recon
from recon.llm_cache import _load_env_file
from recon.money import fmt_inr

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "samples" / "demo"
WEB = Path(__file__).resolve().parent / "web"
PRICE = (2.0, 10.0)  # claude-sonnet-5-5 USD per million input/output tokens (list price)
MAX_AGENT = int(os.environ.get("RECON_MAX_AGENT_EXCEPTIONS", "25"))

_sample: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Reconcile the sample month once, from the recorded agent run (no API key needed)."""
    db = Path(tempfile.mkdtemp()) / "demo.db"
    book, info = pipeline.run(SAMPLE, db, use_agent=True, offline=True)
    info["replayed"] = True
    _sample.update(book=book, info=info, truth=pipeline.load_truth(SAMPLE))
    yield


app = FastAPI(title="ReconAgent", lifespan=lifespan)
SESSIONS: dict[str, dict] = {}  # token -> {"dir", "book", "info", "created"}


def _cost(u: dict) -> float:
    return u.get("input", 0) / 1e6 * PRICE[0] + u.get("output", 0) / 1e6 * PRICE[1]


def _bank_line(book, tid: str) -> dict:
    t = book.bank[tid]
    return {"id": tid, "date": t.date.isoformat(), "narration": t.narration, "amount": t.credit_paise}


def build(book, info: dict, truth: dict | None, card: dict[str, int], mode: str) -> dict:
    matches = book.store.matches()
    escalations = book.store.escalations()
    lines, unplaced = report.month_end(book)
    gap = gaps.explain(book, card)
    credits = book.credits()
    explained = {t for m in matches if m.level == "A" for t in m.left_ids}

    agent_cases = []
    for m in matches:
        if m.source == "deterministic":
            continue
        trace = book.store.trace(m.left_ids[0]) or []
        if m.level == "A":
            left = [_bank_line(book, i) for i in m.left_ids]
            right = [{"id": s, "amount": book.settlements[s].amount, "date": book.settlements[s].settled_on.isoformat(),
                      "utr": book.settlements[s].utr} for s in m.right_ids]
        else:
            left = [{"id": p, "amount": book.rows[p].amount, "date": book.rows[p].created_at.isoformat(),
                     "notes": book.notes_text(book.rows[p])} for p in m.left_ids]
            right = [{"id": i, "amount": book.invoices[i].amount_paise, "customer": book.invoices[i].customer_name,
                      "due": book.invoices[i].due_on.isoformat()} for i in m.right_ids]
        agent_cases.append({"id": m.match_id, "level": m.level, "source": m.source, "pattern": m.pattern,
                            "equation": m.proof.equation, "rationale": m.proof.detail, "left": left, "right": right,
                            "steps": [s.get("step", "") for s in trace]})

    esc = []
    for e in escalations:
        esc.append({"id": e.exception_id, "level": e.level, "reason": e.reason,
                    "options": [{"pattern": h.pattern, "right": (h.settlement_ids if e.level == "A" else h.invoice_ids),
                                 "rationale": h.rationale[:600]} for h in e.explanations]})

    rule_counts: dict[str, int] = {}
    for m in matches:
        if m.source == "deterministic":
            rule_counts[m.proof.rule] = rule_counts.get(m.proof.rule, 0) + 1

    out = {
        "mode": mode,
        "replayed": bool(info.get("replayed")),
        "headline": {
            "credits": len(credits), "credits_explained": len(explained & {t.txn_id for t in credits}),
            "invoices": len(lines), "paid": sum(x.status == "paid" for x in lines),
            "to_chase": sum(x.status != "paid" for x in lines),
            "to_chase_amount": sum(x.amount - x.paid for x in lines if x.status != "paid"),
            "fee_leak": gap.leak_paise, "needs_you": len(escalations) + len(info.get("pending", [])),
            "collected": gap.totals["gross"], "settled": gap.totals["net"],
            "agent_solved": len(agent_cases),
        },
        "lines": [{"invoice": x.invoice_id, "customer": x.customer, "amount": x.amount, "paid": x.paid,
                   "status": x.status, "due": x.due_on, "proof": x.proof} for x in lines],
        "unplaced": unplaced,
        "gaps": {
            "totals": gap.totals, "leak": gap.leak_paise, "unreceived": gap.unreceived_paise,
            "groups": [{"settlements": g.settlement_ids, "settled_on": g.settled_on, "gross": g.gross, "fees": g.fees,
                        "gst": g.gst, "refunds": g.refunds, "chargebacks": g.chargebacks, "net": g.net,
                        "bank": g.bank_amount, "status": g.status, "bank_lines": g.bank_txn_ids, "detail": g.evidence}
                       for g in gap.groups],
            "findings": [{"payment": f.entity_id, "method": f.method, "amount": f.amount, "fee_charged": f.fee_charged,
                          "fee_expected": f.fee_expected, "excess": f.excess, "kind": f.kind} for f in gap.findings],
            "ticket": gaps.ticket(gap, "our account"),
            "card": card,
        },
        "agent_cases": agent_cases,
        "rules": rule_counts,
        "escalations": esc,
        "pending": len(info.get("pending", [])),
        "run": {"seconds": info.get("seconds", 0), "calls": (info.get("usage") or {}).get("calls", 0),
                "cost_usd": round(_cost(info.get("usage") or {}), 2), "bank_format": info.get("bank_format", "")},
    }
    if truth:
        res = score(book, truth)
        out["eval"] = {"levels": {k: {x: v[x] for x in ("items", "matchable", "correct", "false", "escalated", "unmatched")}
                                  | {"auto_match_rate": v["auto_match_rate"]} for k, v in res["levels"].items()},
                       "leakage": res["negative_control_leakage"], "escalation_precision": res["escalation_precision"]}
    return out


class Rates(BaseModel):
    card: dict[str, float]  # method -> percent


def _card(r: Rates | None, default: dict[str, int]) -> dict[str, int]:
    if not r:
        return default
    return {k: int(round(max(0.0, min(v, 10.0)) * 100)) for k, v in r.card.items()}


@app.get("/api/sample")
def sample() -> dict:
    return build(_sample["book"], _sample["info"], _sample["truth"], gaps.SAMPLE_CONTRACT, "sample")


@app.post("/api/sample/rates")
def sample_rates(r: Rates) -> dict:
    return build(_sample["book"], _sample["info"], _sample["truth"], _card(r, gaps.SAMPLE_CONTRACT), "sample")


@app.get("/api/config")
def config() -> dict:
    _load_env_file()
    return {"live_ai": bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("RECON_LIVE_AI") == "true",
            "max_agent": MAX_AGENT}


@app.get("/api/files/{name}")
def sample_file(name: str):
    allowed = {"razorpay_recon.csv", "bank_statement_B.csv", "ledger.csv"}
    if name not in allowed:
        raise HTTPException(404)
    return FileResponse(SAMPLE / name, media_type="text/csv", filename=name)


def _gc() -> None:
    for k, s in list(SESSIONS.items()):
        if time.time() - s["created"] > 3600:
            SESSIONS.pop(k, None)


@app.post("/api/preview")
async def preview(recon: UploadFile = File(...), bank: UploadFile = File(...), ledger: UploadFile = File(...)) -> dict:
    """Parse the three files and show what was understood, before any matching or AI call."""
    _gc()
    tmp = Path(tempfile.mkdtemp())
    paths = {}
    for role, f in (("recon", recon), ("bank", bank), ("ledger", ledger)):
        data = await f.read()
        if len(data) > 10 << 20:
            raise HTTPException(413, f"{role} file is larger than 10 MB")
        paths[role] = tmp / f"{role}.csv"
        paths[role].write_bytes(data)
    try:
        rows, setts = load_recon(paths["recon"])
        txns, source = load_bank(paths["bank"])
        invs = load_ledger(paths["ledger"])
    except (IngestError, KeyError, ValueError) as e:
        raise HTTPException(400, f"Could not read the files: {e}. Compare with the sample files.") from e
    token = secrets.token_urlsafe(12)
    SESSIONS[token] = {"dir": tmp, "paths": paths, "created": time.time()}
    return {"token": token, "razorpay_rows": len(rows), "settlements": len(setts), "bank_lines": len(txns),
            "bank_format": source, "invoices": len(invs),
            "bank_sample": [{"date": t.date.isoformat(), "narration": t.narration[:70], "credit": fmt_inr(t.credit_paise),
                             "debit": fmt_inr(t.debit_paise), "balance": fmt_inr(t.balance_paise)} for t in txns[:6]]}


@app.post("/api/run/{token}")
def run(token: str, r: Rates | None = None) -> dict:
    s = SESSIONS.get(token)
    if not s:
        raise HTTPException(404, "upload expired; please upload again")
    _load_env_file()
    live = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("RECON_LIVE_AI") == "true"
    if "book" not in s:
        p = s["paths"]
        book, info = pipeline.run_files(p["recon"], p["bank"], p["ledger"], s["dir"] / "run.db", use_agent=True,
                                        offline=not live, max_agent_exceptions=MAX_AGENT)
        s.update(book=book, info=info)
    return build(s["book"], s["info"], None, _card(r, gaps.FLAT_2PCT), "upload")


class Approval(BaseModel):
    choice: int


@app.post("/api/approve/{token}/{exception_id}")
def approve(token: str, exception_id: str, a: Approval) -> dict:
    s = _sample if token == "sample" else SESSIONS.get(token)
    if not s or "book" not in s:
        raise HTTPException(404, "session not found")
    violations = pipeline.approve(s["book"], exception_id, a.choice)
    if violations:
        raise HTTPException(409, "; ".join(violations))
    truth = s.get("truth") if token == "sample" else None
    card = gaps.SAMPLE_CONTRACT if token == "sample" else gaps.FLAT_2PCT
    return build(s["book"], s["info"], truth, card, "sample" if token == "sample" else "upload")


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
