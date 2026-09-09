"""Canonical SHA-256 hashing for a loaded agent config dict (Sprint 4
Phase 5 — S-13b, CLAUDE.md Section 62.4/R68).

DELIBERATELY DUPLICATED from app/shared/config_hash.py's identical
`sha256(json.dumps(data, sort_keys=True, default=str))` algorithm, not
imported (F8/R10 — this service has zero dependency on the Factory
Runtime). Matches the same duplication precedent as tool_executor.py's
tool_lambda_name() vs. app/modules/iac_generator/naming.py's — both
pinned against silent drift by a cross-service test
(tests/test_config_hash.py here, mirroring
test_tool_executor.py::test_lambda_name_matches_terraform_naming_
convention's cross-import pattern in the opposite direction).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_config_hash(configuration: dict[str, Any]) -> str:
    canonical = json.dumps(configuration, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
