"""Model config + a disk cache in front of the Anthropic API.

Every call is keyed by hash(model, system, messages, tools, tool_choice, max_tokens). Cached
evals are reproducible and free; the committed demo cache lets the public demo run with no
API key. On a cache miss with no key configured, LlmUnavailable is raised — never a silent stub.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol

# Model ids live here and only here. Override with env vars without touching code.
HYPOTHESIS_MODEL = os.environ.get("RECON_HYPOTHESIS_MODEL", "claude-sonnet-5-5")
CHALLENGER_MODEL = os.environ.get("RECON_CHALLENGER_MODEL", "claude-sonnet-5-5")
SCHEMA_MODEL = os.environ.get("RECON_SCHEMA_MODEL", "claude-haiku-5-5")

CACHE_DIR = Path(os.environ.get("RECON_LLM_CACHE", "data/llm_cache"))


class LlmUnavailable(Exception):
    pass


class Client(Protocol):
    def create(self, **kwargs: Any) -> dict: ...


def _key(kwargs: dict) -> str:
    blob = json.dumps(kwargs, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _load_env_file() -> None:
    env = Path(".env")
    if env.exists() and "ANTHROPIC_API_KEY" not in os.environ:
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "ANTHROPIC_API_KEY" and v.strip():
                os.environ["ANTHROPIC_API_KEY"] = v.strip().strip('"').strip("'")


class CachedAnthropic:
    """`create(**kwargs)` -> plain dict shaped like an Anthropic Message."""

    def __init__(self, cache_dir: Path = CACHE_DIR, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._client = None
        self.hits = self.misses = 0

    def _api(self):
        if self._client is None:
            _load_env_file()
            if self.offline or not os.environ.get("ANTHROPIC_API_KEY"):
                raise LlmUnavailable("cache miss and no ANTHROPIC_API_KEY (or offline mode)")
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def create(self, **kwargs: Any) -> dict:
        path = self.cache_dir / f"{_key(kwargs)}.json"
        if path.exists():
            self.hits += 1
            return json.loads(path.read_text())
        self.misses += 1
        msg = self._api().messages.create(**kwargs)
        out = msg.model_dump(mode="json")
        path.write_text(json.dumps(out, sort_keys=True))
        return out
