"""One reconciliation run end to end, persisted to SQLite. Shared by the CLI and the UI."""

from __future__ import annotations

import json
from pathlib import Path

from recon import llm_cache
from recon.agent import Agent, RunConfig
from recon.book import Book
from recon.deterministic import residue, run_deterministic
from recon.models import Match, Proof
from recon.store import Store
from recon.verifier import verify


def run(folder: Path, db: Path, use_agent: bool = True, offline: bool = True,
        bank_format: str = "A") -> tuple[Book, dict]:
    import time
    t0 = time.monotonic()
    """Deterministic passes, then the agent on the residue. With offline=True the agent runs
    from the committed LLM cache only; any uncached exception is left as `pending`."""
    db = Path(db)
    if db.exists():
        db.unlink()
    book = Book.load(folder, bank_format=bank_format, store=Store(db))
    det = run_deterministic(book)
    info = {"deterministic": len(det), "agent_matched": 0, "escalated": 0, "pending": []}
    if use_agent:
        agent = Agent(book, llm_cache.CachedAnthropic(offline=offline), RunConfig())
        ra, rb = residue(book)
        for level, ids in (("A", ra), ("B", rb)):
            for ex in ids:
                if not book.free(ex):
                    continue
                try:
                    out = agent.resolve(level, ex)
                except llm_cache.LlmUnavailable:
                    info["pending"].append(ex)
                    continue
                info["agent_matched" if out.get("outcome") == "matched" else "escalated"] += 1
        info["usage"] = agent.usage
    info["seconds"] = round(time.monotonic() - t0, 1)
    return book, info


def run_files(recon: Path, bank: Path, ledger: Path, db: Path, use_agent: bool, offline: bool,
              max_agent_exceptions: int = 25, schema_llm=None) -> tuple[Book, dict]:
    """Same as run() for uploaded files with arbitrary names. With a live agent, at most
    `max_agent_exceptions` exceptions are sent to the model per upload (cost cap); the rest stay
    pending for a human, never guessed."""
    import time
    t0 = time.monotonic()
    db = Path(db)
    if db.exists():
        db.unlink()
    book, source = Book.from_files(recon, bank, ledger, store=Store(db), schema_llm=schema_llm)
    det = run_deterministic(book)
    info = {"deterministic": len(det), "agent_matched": 0, "escalated": 0, "pending": [],
            "bank_format": source, "usage": {"calls": 0, "input": 0, "output": 0}}
    if use_agent:
        agent = Agent(book, llm_cache.CachedAnthropic(offline=offline), RunConfig())
        ra, rb = residue(book)
        sent = 0
        for level, ids in (("A", ra), ("B", rb)):
            for ex in ids:
                if not book.free(ex):
                    continue
                if sent >= max_agent_exceptions:
                    info["pending"].append(ex)
                    continue
                sent += 1
                try:
                    out = agent.resolve(level, ex)
                except llm_cache.LlmUnavailable:
                    info["pending"].append(ex)
                    continue
                info["agent_matched" if out.get("outcome") == "matched" else "escalated"] += 1
        info["usage"] = agent.usage
    info["seconds"] = round(time.monotonic() - t0, 1)
    return book, info


def approve(book: Book, exception_id: str, choice: int) -> list[str]:
    """Human approval of one explanation. It goes through the SAME verifier as everything else."""
    esc = next(e for e in book.store.escalations() if e.exception_id == exception_id)
    h = esc.explanations[choice]
    v = verify(h, esc.level, book, exception_id)
    if v:
        return v
    left = h.bank_txn_ids if esc.level == "A" else h.recon_entity_ids
    right = h.settlement_ids if esc.level == "A" else h.invoice_ids
    book.store.commit(Match(match_id=book.store.next_match_id(), level=esc.level,
                            left_ids=tuple(sorted(left)), right_ids=tuple(sorted(right)),
                            source="human", pattern=h.pattern,
                            proof=Proof(rule="human", equation=f"approved explanation #{choice}",
                                        ids=tuple(sorted(left) + sorted(right)),
                                        detail=h.rationale[:300])))
    with book.store.db:
        book.store.db.execute("DELETE FROM escalations WHERE exception_id=?", (exception_id,))
    return []


def summary(book: Book, truth: dict | None) -> dict:
    from eval.metrics import score
    return score(book, truth) if truth else {}


def load_truth(folder: Path) -> dict | None:
    p = Path(folder) / "ground_truth.json"
    return json.loads(p.read_text()) if p.exists() else None
