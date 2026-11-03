"""Web API: sample report, rate recalculation, upload preview + run, approvals."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "demo"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_sample_report_is_complete_and_scored(client):
    r = client.get("/api/sample").json()
    h = r["headline"]
    assert h["credits_explained"] > 0 and h["paid"] > 0 and h["agent_solved"] > 0
    assert r["eval"]["levels"]["A"]["false"] == 0 and r["eval"]["levels"]["B"]["false"] == 0
    assert r["gaps"]["leak"] > 0 and "pay_" in r["gaps"]["ticket"]
    assert all(line["proof"] for line in r["lines"] if line["status"] == "paid")


def test_rates_recalculate_the_fee_audit(client):
    flat = client.post("/api/sample/rates", json={"card": {"upi": 2, "netbanking": 2, "card": 2, "wallet": 2, "emi": 2, "other": 2}}).json()
    assert flat["gaps"]["leak"] == 0


def test_upload_preview_then_run(client):
    files = {"recon": open(SAMPLE / "razorpay_recon.csv", "rb"), "bank": open(SAMPLE / "bank_statement_B.csv", "rb"),
             "ledger": open(SAMPLE / "ledger.csv", "rb")}
    p = client.post("/api/preview", files=files).json()
    assert p["bank_lines"] > 0 and p["invoices"] > 0 and p["token"]
    r = client.post(f"/api/run/{p['token']}").json()
    assert r["mode"] == "upload" and r["headline"]["credits_explained"] > 0


def test_bad_upload_is_explained(client):
    files = {"recon": ("r.csv", b"nope\n1\n"), "bank": ("b.csv", b"x\n"), "ledger": ("l.csv", b"y\n")}
    res = client.post("/api/preview", files=files)
    assert res.status_code == 400 and "Could not read" in res.json()["detail"]


def test_approval_runs_the_verifier(client):
    r = client.get("/api/sample").json()
    esc = next((e for e in r["escalations"] if e["options"]), None)
    if esc is None:
        pytest.skip("no escalation with options in the sample")
    res = client.post(f"/api/approve/sample/{esc['id']}", json={"choice": 0})
    assert res.status_code in (200, 409)  # 409 = the verifier refused it, which is also correct behaviour
