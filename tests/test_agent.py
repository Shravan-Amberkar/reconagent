"""Agent loop tests with a scripted LLM. They test the MACHINERY — verifier gating, feedback,
challenger, caps, injection boundary, PII — independent of how good any real model is."""

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from recon import ingest
from recon.agent import CHALLENGER_SYSTEM, Agent, RunConfig
from recon.book import Book
from recon.deterministic import run_deterministic
from recon.generator import generate, write


@pytest.fixture(autouse=True)
def isolated_schema_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "SCHEMA_CACHE", tmp_path / "schema_cache.json")


@pytest.fixture
def world(tmp_path):
    write(generate(42, payments=400), tmp_path)
    book = Book.load(tmp_path)
    run_deterministic(book)
    truth = json.loads((tmp_path / "ground_truth.json").read_text())
    return book, truth


def first(truth, section, case, book):
    for k, v in truth[section].items():
        if v["case_type"] == case and book.free(k):
            return k, v
    raise AssertionError(f"no free {case} in {section}")


class Scripted:
    """Fake client. `policy(kwargs, n)` returns the content list for call n."""

    def __init__(self, hyp_policy, challenger_policy=None):
        self.hyp, self.chal = hyp_policy, challenger_policy or (lambda kw, n: [tu("declare_none", {"reason": "none"})])
        self.requests, self.n = [], 0

    def create(self, **kw):
        self.requests.append(kw)
        self.n += 1
        fn = self.chal if kw["system"] == CHALLENGER_SYSTEM else self.hyp
        return {"content": fn(kw, self.n), "usage": {"input_tokens": 100, "output_tokens": 20}}


def tu(name, inp, i=[0]):
    i[0] += 1
    return {"type": "tool_use", "id": f"tu_{i[0]}", "name": name, "input": inp}


def b_hyp(pids, invs, pattern="many_to_one", **extra):
    return {"pattern": pattern, "recon_entity_ids": pids, "invoice_ids": invs,
            "rationale": "scripted", **extra}


def all_text(requests):
    return json.dumps([r["messages"] for r in requests], default=str)


# ---------------------------------------------------------------- happy paths

@pytest.mark.parametrize("case", ["name_only", "lump_sum", "prompt_injection"])
def test_truthful_hypothesis_commits(world, case):
    book, truth = world
    pid, t = first(truth, "payments", case, book)
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp(t["payment_group"], t["invoice_ids"]))])
    out = Agent(book, client).resolve("B", pid)
    assert out["outcome"] == "matched", out["trace"]
    m = [m for m in book.store.matches() if pid in m.left_ids][0]
    assert sorted(m.right_ids) == sorted(t["invoice_ids"]) and m.source == "agent"
    assert "payments" in m.proof.equation and book.store.trace(pid)


def test_holiday_shift_commits_with_wide_window(world):
    book, truth = world
    tid, t = first(truth, "bank", "holiday_shift", book)
    h = {"pattern": "holiday_shift", "bank_txn_ids": [tid], "settlement_ids": t["settlement_ids"],
         "rationale": "credit landed after a bank holiday"}
    out = Agent(book, Scripted(lambda kw, n: [tu("submit_hypothesis", h)])).resolve("A", tid)
    assert out["outcome"] == "matched"


# ---------------------------------------------------------------- guardrails

def test_twin_escalates_via_challenger(world):
    book, truth = world
    pid, t = first(truth, "payments", "ambiguous_twin", book)
    out = Agent(book, Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], t["invoice_ids"]))])).resolve("B", pid)
    assert out["outcome"] == "escalated"
    esc = [e for e in book.store.escalations() if e.exception_id == pid][0]
    assert len(esc.explanations) == 2 and "ambiguous" in esc.reason


def test_erp_typo_retries_with_verbatim_feedback_then_escalates(world):
    book, truth = world
    pid, t = first(truth, "payments", "erp_typo", book)
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], t["invoice_ids"]))])
    out = Agent(book, client).resolve("B", pid)
    assert out["outcome"] == "escalated" and out["attempts"] == 3
    second_prompt = client.requests[1]["messages"][0]["content"]
    assert "<verifier_feedback>" in second_prompt and "sum mismatch: payments=" in second_prompt
    assert not any(pid in m.left_ids for m in book.store.matches())


def test_no_feedback_ablation_hides_violations(world):
    book, truth = world
    pid, t = first(truth, "payments", "erp_typo", book)
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], t["invoice_ids"]))])
    Agent(book, client, RunConfig(feedback=False)).resolve("B", pid)
    assert "sum mismatch" not in client.requests[1]["messages"][0]["content"]


def test_injection_cannot_steer_to_decoy(world):
    book, truth = world
    pid, t = first(truth, "payments", "prompt_injection", book)
    decoy = book.rows[pid].notes["remarks"].split("to ")[1].split()[0]
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], [decoy]))])
    out = Agent(book, client).resolve("B", pid)
    assert out["outcome"] == "escalated"
    assert not any(decoy in m.right_ids for m in book.store.matches())
    first_prompt = client.requests[0]["messages"][0]["content"]
    i = first_prompt.find("IGNORE PREVIOUS INSTRUCTIONS")
    assert i > first_prompt.find("<untrusted_data") and i < first_prompt.rfind("</untrusted_data>")


def test_hallucinated_id_rejected(world):
    book, truth = world
    pid, t = first(truth, "payments", "name_only", book)
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], ["INV-99999"]))])
    out = Agent(book, client).resolve("B", pid)
    assert out["outcome"] == "escalated"
    assert "unknown id INV-99999" in client.requests[1]["messages"][0]["content"]


def test_plug_adjustment_rejected(world):
    book, truth = world
    pid, t = first(truth, "payments", "name_only", book)
    plug = b_hyp(t["payment_group"], t["invoice_ids"],
                 adjustments=[{"kind": "fee", "source_entity_id": pid, "amount_paise": 777}])
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", plug)])
    out = Agent(book, client).resolve("B", pid)
    assert out["outcome"] == "escalated"
    assert "fee adjustment 777 != row amount" in client.requests[1]["messages"][0]["content"]


def test_negative_control_declared_unmatchable(world):
    book, truth = world
    tid = [k for k, v in truth["bank"].items() if v["case_type"] == "negative_control"
           and "SELF TRANSFER" in book.bank[k].narration][0]
    client = Scripted(lambda kw, n: [tu("declare_unmatchable", {"reason": "not a gateway credit"})])
    out = Agent(book, client).resolve("A", tid)
    assert out["outcome"] == "escalated"
    assert "9876543210" not in all_text(client.requests)      # PII masked before the LLM


def test_tool_budget_caps_runaway(world):
    book, truth = world
    pid, _ = first(truth, "payments", "name_only", book)
    client = Scripted(lambda kw, n: [tu("query_ledger", {"amount_paise": 100})])
    out = Agent(book, client, RunConfig(max_tool_calls=2)).resolve("B", pid)
    assert out["outcome"] == "escalated"
    assert any(s["step"] == "budget" for s in out["trace"])
    assert client.n <= 3 * 3                                      # 3 attempts x (2 tools + 1)


def test_tool_validation_error_returned_to_model(world):
    book, truth = world
    pid, t = first(truth, "payments", "name_only", book)
    calls = []

    def policy(kw, n):
        calls.append(kw)
        if len(calls) == 1:
            return [tu("find_subset_sum", {"candidate_ids": ["x"], "target_paise": "lots"})]
        return [tu("submit_hypothesis", b_hyp(t["payment_group"], t["invoice_ids"]))]

    out = Agent(book, Scripted(policy)).resolve("B", pid)
    assert out["outcome"] == "matched"
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "invalid arguments" in tool_result["content"]


# ---------------------------------------------------------------- ablations

def test_no_verifier_commits_garbage(world):
    book, truth = world
    pid, t = first(truth, "payments", "prompt_injection", book)
    decoy = book.rows[pid].notes["remarks"].split("to ")[1].split()[0]
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], [decoy]))])
    out = Agent(book, client, RunConfig(verifier=False, challenger=False)).resolve("B", pid)
    assert out["outcome"] == "matched"          # this is why the verifier exists
    assert any(decoy in m.right_ids for m in book.store.matches())


def test_no_challenger_force_matches_twin(world):
    book, truth = world
    pid, t = first(truth, "payments", "ambiguous_twin", book)
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], t["invoice_ids"]))])
    out = Agent(book, client, RunConfig(challenger=False)).resolve("B", pid)
    assert out["outcome"] == "matched"          # this is why the challenger exists


# ---------------------------------------------------------------- human queue

def test_human_interrupt_and_approve(world):
    book, truth = world
    pid, t = first(truth, "payments", "ambiguous_twin", book)
    client = Scripted(lambda kw, n: [tu("submit_hypothesis", b_hyp([pid], t["invoice_ids"]))])
    agent = Agent(book, client, RunConfig(human_in_loop=True), checkpointer=InMemorySaver())
    app, cfg = agent.graph(), {"configurable": {"thread_id": pid}}
    first_run = app.invoke({"exception": {"level": "B", "id": pid}}, cfg)
    assert "__interrupt__" in first_run
    payload = first_run["__interrupt__"][0].value["escalation"]
    assert len(payload["explanations"]) == 2
    done = app.invoke(Command(resume={"approve": 0}), cfg)
    assert done["outcome"] == "matched"
    assert [m for m in book.store.matches() if pid in m.left_ids][0].source == "human"
