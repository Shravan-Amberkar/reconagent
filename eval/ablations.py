"""Ablation table: the same seeds under each configuration. Writes results/ablations.{json,md}.

    python -m eval.ablations --seeds 1..3 --payments 250

The table in the README is generated from results/ablations.md — never hand-typed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from eval.run_eval import run

CONFIGS = [
    ("deterministic-only", dict(deterministic_only=True)),
    ("full", dict()),
    ("no-verifier", dict(no_verifier=True)),
    ("no-challenger", dict(no_challenger=True)),
    ("no-feedback", dict(no_feedback=True)),
]


def table(rows: dict) -> str:
    out = ["| config | auto-match B | auto-match A | false matches | false-match rate (B) | "
           "neg-control leakage | escalations | esc. precision | twins/typos force-matched | "
           "LLM calls/exception |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in rows.items():
        a, b = r["levels"]["A"], r["levels"]["B"]
        forced = sum(r["per_case"].get(f"B:{c}", {}).get("false", 0)
                     for c in ("ambiguous_twin", "erp_typo"))
        out.append(f"| {name} | {b['auto_match_rate']:.1%} | {a['auto_match_rate']:.1%} | "
                   f"{a['false'] + b['false']} | {b['false_match_rate']:.1%} | "
                   f"{r['negative_control_leakage']} | {r['escalations']} | "
                   f"{r['escalation_precision']} | {forced} | "
                   f"{r['cost']['llm_calls_per_exception']} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1..3")
    ap.add_argument("--payments", type=int, default=250)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only", default="", help="comma list of config names")
    a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    rows = {}
    for name, flags in CONFIGS:
        if only and name not in only:
            continue
        ns = SimpleNamespace(seeds=a.seeds, payments=a.payments, bank_format="A",
                             deterministic_only=False, no_verifier=False, no_challenger=False,
                             no_feedback=False, offline=a.offline, **flags)
        rows[name] = run(ns)
        print(f"done {name}")
    out = Path("results")
    out.mkdir(exist_ok=True)
    (out / "ablations.json").write_text(json.dumps(rows, indent=1, sort_keys=True, default=str))
    md = table(rows)
    (out / "ablations.md").write_text(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
