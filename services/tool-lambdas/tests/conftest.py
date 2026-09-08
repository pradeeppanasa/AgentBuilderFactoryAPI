"""Adds the service root to sys.path — `shared` is a real package under it
(services/tool-lambdas/shared/), imported the same way a real tool
handler eventually will (`from shared.ssrf_guard import ...`), matching
how a Lambda Layer or bundled shared code would resolve it in production.
Same pattern as services/agent-runtime/tests/conftest.py."""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
