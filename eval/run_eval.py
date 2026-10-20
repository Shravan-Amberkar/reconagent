"""Run the pipeline on seeded datasets and score it against ground truth.

    python -m eval.run_eval --seeds 1..3 --payments 250 --deterministic-only
    python -m eval.run_eval --seeds 1..3 --payments 250            # full system (needs key or cache)

Writes results/<timestamp>-<config>.json and results/latest-<config>.json, prints a markdown table.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from eval.metrics import markdown, score
from recon import llm_cache
from recon.agent import Agent, RunConfig
from recon.book import Book
from recon.deterministic import residue, run_deterministic
from recon.generator import generate, write

# $ per million tokens (input, output). Check current pricing before trusting cost numbers.
PRICES = {"claude-sonnet-5": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
          "claude-opus-5": (5.0, 25.0)}

RUNS = Path("data/runs")


def seeds_arg(s: str) -> list[int]:
    if ".." in s:
        a, b = s.split("..")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


def dataset(seed: int, payments: int) -> Path:
    out = RUNS / f"seed{seed}-p{payments}"
    if not (out / "ground_truth.json").exists():
        write(generate(seed, payments=payments), out)
    return out


def config_name(a) -> str:
    if a.deterministic_only:
        return "deterministic-only"
    off = [n for n, f in (("verifier", a.no_verifier), ("challenger", a.no_challenger),
                          ("feedback", a.no_feedback)) if f]
    return "full" if not off else "no-" + "-no-".join(off)


def run_seed(seed: int, a, client) -> tuple[dict, dict]:
    folder = dataset(seed, a.payments)
    truth = json.loads((folder / "ground_truth.json").read_text())
    book = Book.load(folder, bank_format=a.bank_format)
    run_deterministic(book)
    stats = {"exceptions": 0, "latencies": [], "calls": 0, "input": 0, "output": 0}
    if not a.deterministic_only:
        cfg = RunConfig(verifier=not a.no_verifier, challenger=not a.no_challenger,
                        feedback=not a.no_feedback)
        agent = Agent(book, client, cfg)
        ra, rb = residue(book)
        for level, ids in (("A", ra), ("B", rb)):
            for ex in ids:
                if not book.free(ex):
                    continue
                t0 = time.perf_counter()
                agent.resolve(level, ex)
                stats["latencies"].append(time.perf_counter() - t0)
                stats["exceptions"] += 1
        stats.update({k: agent.usage[k] for k in ("calls", "input", "output")})
    return score(book, truth), stats


def merge(results: list[dict]) -> dict:
    """Sum counts across seeds and recompute the rates."""
    keys = ("items", "matchable", "correct", "false", "escalated", "unmatched")

    def rates(d):
        committed = d["correct"] + d["false"]
        d["false_match_rate"] = round(d["false"] / committed, 4) if committed else 0.0
        d["auto_match_rate"] = round(d["correct"] / d["matchable"], 4) if d["matchable"] else 0.0
        return d

    levels = defaultdict(lambda: dict.fromkeys(keys, 0))
    per_case = defaultdict(lambda: dict.fromkeys(keys, 0))
    for r in results:
        for scope, target in (("levels", levels), ("per_case", per_case)):
            for name, d in r[scope].items():
                for k in keys:
                    target[name][k] += d[k]
    esc = sum(r["escalations"] for r in results)
    prec = [r["escalation_precision"] * r["escalations"] for r in results if r["escalations"]]
    return {"levels": {k: rates(v) for k, v in sorted(levels.items())},
            "per_case": {k: rates(v) for k, v in sorted(per_case.items())},
            "negative_control_leakage": sum(r["negative_control_leakage"] for r in results),
            "escalations": esc,
            "escalation_precision": round(sum(prec) / esc, 4) if esc else None}


def run(a) -> dict:
    client = llm_cache.CachedAnthropic(offline=a.offline)
    per_seed, all_stats = {}, []
    for seed in seeds_arg(a.seeds):
        res, stats = run_seed(seed, a, client)
        per_seed[seed] = res
        all_stats.append(stats)
    agg = merge(list(per_seed.values()))
    lat = sorted(x for s in all_stats for x in s["latencies"])
    calls = sum(s["calls"] for s in all_stats)
    exc = sum(s["exceptions"] for s in all_stats)
    tin, tout = sum(s["input"] for s in all_stats), sum(s["output"] for s in all_stats)
    pin, pout = PRICES.get(llm_cache.HYPOTHESIS_MODEL, (3.0, 15.0))
    usd = (tin * pin + tout * pout) / 1e6
    bank_lines = sum(r["levels"]["A"]["items"] for r in per_seed.values())
    agg["cost"] = {
        "exceptions": exc, "llm_calls": calls,
        "llm_calls_per_exception": round(calls / exc, 2) if exc else 0,
        "input_tokens": tin, "output_tokens": tout, "usd": round(usd, 4),
        "inr_per_1000_bank_lines": round(usd * 84 * 1000 / bank_lines, 2) if bank_lines else 0,
        "latency_p50_s": round(statistics.median(lat), 3) if lat else None,
        "latency_p95_s": round(lat[int(0.95 * (len(lat) - 1))], 3) if lat else None,
        "cache_hits": client.hits, "cache_misses": client.misses,
    }
    agg["config"] = {"name": config_name(a), "seeds": a.seeds, "payments": a.payments,
                     "bank_format": a.bank_format, "model": llm_cache.HYPOTHESIS_MODEL}
    agg["per_seed"] = per_seed
    return agg


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", default="1..3")
    ap.add_argument("--payments", type=int, default=250)
    ap.add_argument("--bank-format", default="A", choices=["A", "B", "C"])
    ap.add_argument("--deterministic-only", action="store_true")
    ap.add_argument("--no-verifier", action="store_true")
    ap.add_argument("--no-challenger", action="store_true")
    ap.add_argument("--no-feedback", action="store_true")
    ap.add_argument("--offline", action="store_true", help="cache only; fail on any miss")
    a = ap.parse_args()
    res = run(a)
    name = res["config"]["name"]
    out = Path("results")
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = json.dumps(res, indent=1, sort_keys=True, default=str)
    (out / f"{stamp}-{name}.json").write_text(body)
    (out / f"latest-{name}.json").write_text(body)
    print(markdown(res, f"{name} · seeds {a.seeds} · {a.payments} payments/seed"))
    print("\ncost:", json.dumps(res["cost"]))


if __name__ == "__main__":
    main()
