"""Inject generated result tables into README.md between markers. Never hand-type results.

    python -m eval.readme_tables
"""

import json
import re
from pathlib import Path

from eval.metrics import markdown

README = Path("README.md")


def block(name: str, body: str) -> None:
    text = README.read_text()
    pat = re.compile(rf"(<!-- BEGIN {name} -->\n).*?(\n<!-- END {name} -->)", re.S)
    if not pat.search(text):
        raise SystemExit(f"marker {name} missing from README")
    README.write_text(pat.sub(lambda m: m.group(1) + body.strip() + m.group(2), text))


def main():
    abl = Path("results/ablations.md")
    block("ABLATIONS", abl.read_text() if abl.exists() else
          "_Not yet generated — run `python -m eval.ablations` with an API key (or a filled cache)._")
    for name in ("deterministic-only", "full"):
        p = Path(f"results/latest-{name}.json")
        if p.exists():
            r = json.loads(p.read_text())
            c = r["config"]
            body = markdown(r, f"{name} · seeds {c['seeds']} · {c['payments']} payments/seed · "
                               f"model {c['model'] if name == 'full' else '—'}")
        else:
            body = f"_Not yet generated — run `python -m eval.run_eval` ({name})._"
        block(name.upper().replace("-", "_"), body)
    print("README tables updated")


if __name__ == "__main__":
    main()
