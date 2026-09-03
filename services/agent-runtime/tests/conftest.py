"""Adds the service root to sys.path — these are top-level modules
(config_loader, orchestrator, …), not a package, matching how the built
Docker image actually runs them (WORKDIR /app, `uvicorn main:app`)."""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
