"""Central logging setup. Call setup_logging() once at process start.

Level is controlled by STOCK_AGENT_LOG_LEVEL (default INFO; use DEBUG for
per-fetch detail). Logs go to stdout so they appear under uvicorn / cron.
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("STOCK_AGENT_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger("app")
    root.setLevel(getattr(logging, level, logging.INFO))
    # Avoid duplicate handlers on reload.
    root.handlers = [handler]
    root.propagate = False
    _CONFIGURED = True
