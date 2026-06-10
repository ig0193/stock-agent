"""Environment/config helpers: load a .env file and report LLM status."""
from __future__ import annotations

import logging
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
log = logging.getLogger("app.config")


def load_env() -> None:
    """Load KEY=VALUE pairs from a project-root .env into os.environ.

    Does not override variables already present in the environment. Keeps the
    app dependency-free (no python-dotenv needed).
    """
    if not os.path.exists(_ENV_PATH):
        return
    try:
        with open(_ENV_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        log.warning("could not read .env: %s", exc)


def llm_status() -> str:
    """One-line description of whether the LLM path is active."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        model = os.environ.get("STOCK_AGENT_MODEL", "claude-sonnet-4-6")
        return f"LLM ENABLED (model={model})"
    return "LLM DISABLED (no ANTHROPIC_API_KEY) — using rule-based fallback"
