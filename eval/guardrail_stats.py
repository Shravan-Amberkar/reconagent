"""How often each guardrail actually fired in a run (replayed from the LLM cache).

    python -m eval.guardrail_stats --seeds 1..3 --payments 250
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from eval.run_eval import dataset, seeds_arg
from recon import llm_cache
from recon.agent import Agent, RunConfig
from recon.book import Book
from recon.deterministic import residue, run_deterministic


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1..3")
    ap.add_argument("--payments", type=int, default=250)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    c = Counter()
    for seed in seeds_arg(a.seeds):
        folder = dataset(seed, a.payments)
        truth = json.loads((folder / "ground_truth.json").read_text())
        book = Book.load(folder)
        run_deterministic(book)
        agent = Agent(book, llm_cache.CachedAnthropic(offline=a.offline), RunConfig())
        ra, rb = residue(book)
        for level, ids in (("A", ra), ("B", rb)):
            for ex in ids:
                if not book.free(ex):
                    continue
                out = agent.resolve(level, ex)
                case = (truth["bank"].get(ex) or truth["payments"].get(ex))["case_type"]
                c["exceptions"] += 1
                hyps = [s for s in out["trace"] if s["step"] == "submit_hypothesis"]
                rejected = [s for s in out["trace"] if s["step"] == "verify" and s.get("violations")]
                c["hypotheses"] += len(hyps)
                c["verifier_rejections"] += len(rejected)
                if rejected and out["outcome"] == "matched":
                    c["self_corrected_after_rejection"] += 1
                for s in rejected:
                    for v in s["violations"]:
                        c["violation:" + v.split(":")[0].split(" ")[0]] += 1
                for s in out["trace"]:
                    if s["step"] == "challenge" and s.get("alternative"):
                        c["challenger_alternative_" + ("rejected_by_verifier" if s.get("violations")
                                                       else "verified_so_escalated")] += 1
                if any(s["step"] == "declare_unmatchable" for s in out["trace"]):
                    c[f"declared_unmatchable:{case}"] += 1
                c[f"outcome:{out['outcome']}"] += 1
    print(json.dumps(dict(sorted(c.items())), indent=1))
    Path("results").mkdir(exist_ok=True)
    tag = llm_cache.HYPOTHESIS_MODEL
    Path(f"results/latest-guardrails-{tag}.json").write_text(json.dumps(dict(sorted(c.items())), indent=1))


if __name__ == "__main__":
    main()
