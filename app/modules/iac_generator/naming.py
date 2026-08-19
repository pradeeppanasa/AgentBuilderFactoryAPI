"""Resource-name helpers shared between the Terraform renderer and the
IaC validator (QA I-02). Kept in one place so both sides compute the exact
same name for a given agent_id — never re-derive the same logic twice.

Bedrock Guardrail names are capped at 50 characters by AWS. Every other
resource this generator names (Lambda 64 chars, API Gateway 128 chars,
DynamoDB 255 chars) has enough headroom that `panasa-{agent_id}-{suffix}`
never gets close to the limit for realistic agent_id lengths — the
guardrail is the one resource type that can genuinely overflow.
"""

from __future__ import annotations

import hashlib

_BEDROCK_GUARDRAIL_NAME_MAX = 50
_GUARDRAIL_SUFFIX = "-guardrail"
_GUARDRAIL_PREFIX = "panasa-"
_ID_HASH_LEN = 6


def bedrock_guardrail_name(agent_id: str) -> str:
    """`panasa-{agent_id}-guardrail`, truncated to fit AWS's 50-character
    Bedrock Guardrail name limit when agent_id is long enough to overflow
    it. Truncation keeps a short hash of the FULL agent_id (not just a
    truncated prefix) so two long agent_ids that happen to share the same
    leading characters can never collide on the same guardrail name."""
    full = f"{_GUARDRAIL_PREFIX}{agent_id}{_GUARDRAIL_SUFFIX}"
    if len(full) <= _BEDROCK_GUARDRAIL_NAME_MAX:
        return full

    # Budget for the truncated agent_id: total - "panasa-" - "-" - hash - "-guardrail"
    budget = (
        _BEDROCK_GUARDRAIL_NAME_MAX
        - len(_GUARDRAIL_PREFIX)
        - 1
        - _ID_HASH_LEN
        - len(_GUARDRAIL_SUFFIX)
    )
    truncated_id = agent_id[:budget]
    id_hash = hashlib.sha1(agent_id.encode("utf-8")).hexdigest()[:_ID_HASH_LEN]
    return f"{_GUARDRAIL_PREFIX}{truncated_id}-{id_hash}{_GUARDRAIL_SUFFIX}"
