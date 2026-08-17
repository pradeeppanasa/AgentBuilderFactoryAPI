"""Bedrock credential library (CLAUDE.md Section 37.14: "Credential —
dropdown of saved Bedrock credentials from platform credentials store").

STS AssumeRole design (chosen 2026-08-16, over static access keys): stores
only a `role_arn` — no long-lived secret material anywhere in this
Runtime. `app.modules.guardrails.provisioner.BedrockGuardrailProvisioner`
assumes this role at guardrail-provisioning time to get short-lived
temporary credentials. Matches R03/R04's "no runtime dependency on Panasa"
posture and Section 11's "never store secret values, ARNs/identifiers
only" rule more closely than storing an access key/secret key pair would
— the customer must pre-configure a trust policy on the role allowing this
Runtime's own execution role to assume it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BedrockCredentialRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: str
    tenant_id: str
    name: str
    role_arn: str
    created_by: str
    created_at: str
    updated_at: str
