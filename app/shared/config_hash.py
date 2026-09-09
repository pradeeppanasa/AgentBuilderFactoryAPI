"""Canonical SHA-256 hashing for AgentConfiguration (Sprint 4 Phase 5 —
S-13b, CLAUDE.md Section 62.4/R68).

`sha256(json.dumps(data, sort_keys=True, default=str))` — the same
canonicalisation pattern already established in this codebase for
exactly this kind of "hash something for later independent
recomputation" need (app/modules/audit/security_log.py's hash-chain,
app/modules/iac_generator/tfvars.py's reproducible tfvars output), not
`model_dump_json()`'s field-DECLARATION-order serialisation.

That choice is load-bearing here, not stylistic: the value MUST be
independently recomputable by services/agent-runtime/config_loader.py, a
SEPARATE deployable (F8/R10) that never imports this Pydantic model — or
this module — at all; it only ever sees a plain dict pulled out of
DynamoDB. `sort_keys` canonicalises a bare dict; Pydantic declaration
order does not survive a DynamoDB round-trip (json.dumps -> stored as a
String -> json.loads back into a plain dict) in any way this Runtime's
own model class could enforce on the other side. The identical
`_hash_canonical`-shaped algorithm is deliberately DUPLICATED (not
imported) into services/agent-runtime/config_hash.py, matching the same
F8 precedent as tool_executor.py's tool_lambda_name() duplicating
app/modules/iac_generator/naming.py — with the same kind of cross-service
pinning test (services/agent-runtime/tests/test_config_hash.py) that
tool_lambda_name() already has, so the two copies can't silently drift.
"""

from __future__ import annotations

import hashlib
import json

from app.modules.registry.models import AgentConfiguration


def compute_config_hash(configuration: AgentConfiguration) -> str:
    """Hex-encoded SHA-256 over the configuration's canonical JSON form.
    `mode="json"` first so nested Pydantic models/enums are already plain
    JSON-compatible values before the canonicalising dump — the same
    two-step `model_dump(mode="json")` -> `json.dumps(..., sort_keys=True)`
    shape as versioner.py's own DynamoDB item encoding."""
    canonical = json.dumps(configuration.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
