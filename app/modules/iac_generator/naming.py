"""Resource-name helpers shared between the Terraform renderer and the
IaC validator (QA I-02). Kept in one place so both sides compute the exact
same name for a given agent_id — never re-derive the same logic twice.

Several AWS resource types cap names well under DynamoDB/API Gateway's
generous 255/128-character limits, tight enough that
`panasa-{agent_id}-{suffix}` can genuinely overflow for a realistic
agent_id: Bedrock Guardrails (50 chars); OpenSearch Serverless collections,
Application Load Balancers and target groups (32 chars each); and, where
the identifier also folds in a per-tool tool_id (tools.tf.j2's Lambda
function names and their execution roles), IAM role names (64 chars) —
all Generic Agent Runtime instruction, 2026-09-03. Every one of these uses
the same truncation strategy: keep a short hash of the FULL identifier
(not just a truncated prefix) so two long identifiers sharing the same
leading characters can never collide on the same name.
"""

from __future__ import annotations

import hashlib

_ID_HASH_LEN = 6


def _truncated_name(prefix: str, identifier: str, suffix: str, max_length: int) -> str:
    full = f"{prefix}{identifier}{suffix}"
    if len(full) <= max_length:
        return full

    # Budget for the truncated identifier: total - prefix - "-" - hash - suffix
    budget = max_length - len(prefix) - 1 - _ID_HASH_LEN - len(suffix)
    truncated = identifier[:budget]
    id_hash = hashlib.sha1(identifier.encode("utf-8")).hexdigest()[:_ID_HASH_LEN]
    return f"{prefix}{truncated}-{id_hash}{suffix}"


def bedrock_guardrail_name(agent_id: str) -> str:
    """`panasa-{agent_id}-guardrail`, truncated to fit AWS's 50-character
    Bedrock Guardrail name limit."""
    return _truncated_name("panasa-", agent_id, "-guardrail", 50)


def alb_name(agent_id: str) -> str:
    """`panasa-{agent_id}-alb`, truncated to fit AWS's 32-character
    Application Load Balancer name limit."""
    return _truncated_name("panasa-", agent_id, "-alb", 32)


def target_group_name(agent_id: str) -> str:
    """`panasa-{agent_id}-tg`, truncated to fit AWS's 32-character target
    group name limit."""
    return _truncated_name("panasa-", agent_id, "-tg", 32)


def opensearch_collection_name(agent_id: str) -> str:
    """`panasa-{agent_id}-kb`, truncated to fit AWS's 32-character
    OpenSearch Serverless collection name limit."""
    return _truncated_name("panasa-", agent_id, "-kb", 32)


def _tool_identifier(agent_id: str, tool_id: str) -> str:
    return f"{agent_id}-tool-{tool_id}"


def tool_lambda_name(agent_id: str, tool_id: str) -> str:
    """`panasa-{agent_id}-tool-{tool_id}`, truncated to fit AWS Lambda's
    64-character function-name limit. Unlike the single-identifier helpers
    above, the IaC validator's naming_convention check does NOT special-case
    this one (or tool_role_name below) — deriving which tool a given
    Terraform resource block belongs to from its already-transformed
    resource name is significantly more involved than the other checks'
    1:1 agent_id -> expected-name mapping. A pathologically long
    agent_id+tool_id combination that actually triggers truncation here can
    produce a naming_convention false-positive in that static check; that's
    a narrow, cosmetic gap, not a correctness one — this function's actual
    job (keeping terraform_validate/apply from hard-failing on a real
    64-character AWS limit) still holds regardless."""
    return _truncated_name("panasa-", _tool_identifier(agent_id, tool_id), "", 64)


def tool_role_name(agent_id: str, tool_id: str) -> str:
    """`panasa-{agent_id}-tool-{tool_id}-role`, truncated to fit AWS IAM's
    64-character role-name limit. See tool_lambda_name's docstring — same
    validator caveat applies."""
    return _truncated_name("panasa-", _tool_identifier(agent_id, tool_id), "-role", 64)
