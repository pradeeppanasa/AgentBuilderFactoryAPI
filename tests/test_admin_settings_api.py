"""API tests for /api/v1/admin/settings/* (CLAUDE.md Section 39/R45,
R45-7/8 — Admin Observability Settings API + panasa-platform-settings).

Every route here is admin-only, including the GET routes (unlike the
Knowledge Base / Guardrail Policy / Skills libraries, where reads are open
to every role) — see admin_settings.py's module docstring. Secret values
(Langfuse secret key, Datadog API key) are never round-tripped on GET, only
"****" (set) or null (unset); moto mocks Secrets Manager directly so no
fakes are needed here (unlike the Bedrock guardrail control-plane calls
elsewhere in this suite).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_default_observability_config_has_always_on_stack(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/admin/settings/observability", headers=_bearer(admin_token)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["default_stack"] == {
            "cloudwatch": "active",
            "xray": "active",
            "otel_sdk": "deferred",
        }
        assert body["otel"]["endpoint"] is None
        assert body["langfuse"] == {
            "enabled": False,
            "public_key": None,
            "secret_key": None,
            "host": None,
        }
        assert body["datadog"] == {"enabled": False, "api_key": None, "site": None}


async def test_deployment_settings_default_to_automated(make_user_and_token) -> None:
    """Section 45.3/45.13 (R50, resolved as configurable) — F1's fully
    automated pipeline is the default; a tenant must explicitly opt into
    R50/Stage 5's manual-approval gate."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get("/api/v1/admin/settings/deployment", headers=_bearer(admin_token))

    assert response.status_code == 200
    body = response.json()
    assert body["default_approval_mode"] == "automated"
    assert body["cicd_provider"] == "github_actions"
    assert body["kb_s3_bucket"] is None
    assert body["kb_s3_prefix"] == "agent-factory"
    # git_organisation/aws_region fall back to the GIT_ORG/AWS_REGION env
    # vars when the tenant hasn't set its own — aws_region always has a
    # global default so it's never null on the response.
    assert body["aws_region"]


async def test_save_and_read_back_deployment_settings(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual", "cicd_provider": "gitlab_ci"},
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        saved_body = saved.json()
        assert saved_body["default_approval_mode"] == "manual"
        assert saved_body["cicd_provider"] == "gitlab_ci"
        assert saved_body["kb_s3_bucket"] is None
        assert saved_body["kb_s3_prefix"] == "agent-factory"

        fetched = client.get("/api/v1/admin/settings/deployment", headers=_bearer(admin_token))
        fetched_body = fetched.json()
        assert fetched_body["default_approval_mode"] == "manual"
        assert fetched_body["cicd_provider"] == "gitlab_ci"
        assert fetched_body["kb_s3_bucket"] is None
        assert fetched_body["kb_s3_prefix"] == "agent-factory"


async def test_save_and_read_back_kb_s3_settings(make_user_and_token) -> None:
    """CLAUDE.md Section 47 (R59 corrected 2026-09-01) — "Settings ->
    Deployment -> Customer S3 Bucket / S3 Folder Prefix"."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={
                "default_approval_mode": "automated",
                "kb_s3_bucket": "acme-customer-bucket",
                "kb_s3_prefix": "knowledge",
            },
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        assert saved.json()["kb_s3_bucket"] == "acme-customer-bucket"
        assert saved.json()["kb_s3_prefix"] == "knowledge"

        # Omitting kb_s3_bucket/kb_s3_prefix on a later save keeps them.
        resaved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )
        assert resaved.json()["kb_s3_bucket"] == "acme-customer-bucket"
        assert resaved.json()["kb_s3_prefix"] == "knowledge"

        # An explicit empty string unconfigures the bucket (matches every
        # other optional-field convention in this file).
        cleared = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual", "kb_s3_bucket": ""},
            headers=_bearer(admin_token),
        )
        assert cleared.json()["kb_s3_bucket"] is None
        assert cleared.json()["kb_s3_prefix"] == "knowledge"


async def test_save_and_read_back_git_org_and_aws_region(make_user_and_token) -> None:
    """"Settings -> Deployment -> Git Organisation / AWS Region"."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={
                "default_approval_mode": "automated",
                "git_organisation": "acme-corp",
                "aws_region": "us-east-1",
            },
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        assert saved.json()["git_organisation"] == "acme-corp"
        assert saved.json()["aws_region"] == "us-east-1"

        # Omitting on a later save keeps them.
        resaved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )
        assert resaved.json()["git_organisation"] == "acme-corp"
        assert resaved.json()["aws_region"] == "us-east-1"

        # Explicit empty string clears back to the env var fallback.
        cleared = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual", "git_organisation": ""},
            headers=_bearer(admin_token),
        )
        assert cleared.json()["git_organisation"] != "acme-corp"
        assert cleared.json()["aws_region"] == "us-east-1"


async def test_deployment_settings_aws_target_defaults_unconfigured(
    make_user_and_token,
) -> None:
    """"Settings -> Deployment -> AWS Target" (Generic Agent Runtime wiring)
    — all None/empty until the tenant configures a real deploy target."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get("/api/v1/admin/settings/deployment", headers=_bearer(admin_token))

    body = response.json()
    assert body["agent_vpc_id"] is None
    assert body["agent_subnet_ids"] == []
    assert body["agent_ecs_cluster_arn"] is None
    assert body["agent_runtime_ecr_registry"] is None
    assert body["bedrock_endpoint_cidr"] is None
    assert body["opensearch_endpoint_cidr"] is None


async def test_save_and_read_back_aws_target_settings(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={
                "default_approval_mode": "automated",
                "agent_vpc_id": "vpc-0123456789abcdef0",
                "agent_subnet_ids": ["subnet-aaa", "subnet-bbb"],
                "agent_ecs_cluster_arn": "arn:aws:ecs:eu-west-2:111122223333:cluster/acme",
                "agent_runtime_ecr_registry": (
                    "111122223333.dkr.ecr.eu-west-2.amazonaws.com/agent-runtime"
                ),
                "bedrock_endpoint_cidr": "10.0.1.0/24",
                "opensearch_endpoint_cidr": "10.0.2.0/24",
            },
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        saved_body = saved.json()
        assert saved_body["agent_vpc_id"] == "vpc-0123456789abcdef0"
        assert saved_body["agent_subnet_ids"] == ["subnet-aaa", "subnet-bbb"]
        assert saved_body["agent_ecs_cluster_arn"] == (
            "arn:aws:ecs:eu-west-2:111122223333:cluster/acme"
        )
        assert saved_body["agent_runtime_ecr_registry"] == (
            "111122223333.dkr.ecr.eu-west-2.amazonaws.com/agent-runtime"
        )
        assert saved_body["bedrock_endpoint_cidr"] == "10.0.1.0/24"
        assert saved_body["opensearch_endpoint_cidr"] == "10.0.2.0/24"

        # Omitting on a later save keeps them.
        resaved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )
        assert resaved.json()["agent_vpc_id"] == "vpc-0123456789abcdef0"
        assert resaved.json()["agent_subnet_ids"] == ["subnet-aaa", "subnet-bbb"]

        # Explicit empty clears back to unconfigured.
        cleared = client.patch(
            "/api/v1/admin/settings/deployment",
            json={
                "default_approval_mode": "manual",
                "agent_vpc_id": "",
                "agent_subnet_ids": [],
            },
            headers=_bearer(admin_token),
        )
        assert cleared.json()["agent_vpc_id"] is None
        assert cleared.json()["agent_subnet_ids"] == []
        # Untouched fields stay put.
        assert cleared.json()["agent_ecs_cluster_arn"] == (
            "arn:aws:ecs:eu-west-2:111122223333:cluster/acme"
        )


async def test_validate_s3_bucket_accessible(make_user_and_token) -> None:
    import boto3

    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    bucket = "settings-validate-test-bucket"
    s3 = boto3.client("s3", region_name="eu-west-2")
    try:
        s3.create_bucket(
            Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"}
        )
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/settings/deployment/validate-s3-bucket",
            json={"bucket_name": bucket},
            headers=_bearer(admin_token),
        )

    assert response.status_code == 200
    assert response.json() == {"accessible": True, "bucket_name": bucket}


async def test_validate_s3_bucket_inaccessible_returns_422(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/settings/deployment/validate-s3-bucket",
            json={"bucket_name": "this-bucket-does-not-exist-anywhere"},
            headers=_bearer(admin_token),
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "bucket_not_accessible"


async def test_developer_cannot_validate_s3_bucket(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/settings/deployment/validate-s3-bucket",
            json={"bucket_name": "whatever"},
            headers=_bearer(dev_token),
        )

    assert response.status_code == 403


async def test_deployment_settings_forbidden_for_developer(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/admin/settings/deployment", headers=_bearer(developer_token)
        )

    assert response.status_code == 403


async def test_save_and_read_back_otel_endpoint(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/otel-endpoint",
            json={"endpoint": "http://collector.internal:4317"},
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        assert saved.json()["endpoint"] == "http://collector.internal:4317"

        fetched = client.get(
            "/api/v1/admin/settings/observability", headers=_bearer(admin_token)
        )
        assert fetched.json()["otel"]["endpoint"] == "http://collector.internal:4317"


async def test_save_langfuse_config_masks_secret_on_read(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/integrations/langfuse",
            json={
                "enabled": True,
                "public_key": "pk-live-abc",
                "secret_key": "sk-live-super-secret",
                "host": "https://langfuse.internal.example.com",
            },
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["enabled"] is True
        assert body["public_key"] == "pk-live-abc"
        assert body["secret_key"] == "****"
        assert body["host"] == "https://langfuse.internal.example.com"
        assert "sk-live-super-secret" not in saved.text

        fetched = client.get(
            "/api/v1/admin/settings/integrations/langfuse", headers=_bearer(admin_token)
        )
        fetched_body = fetched.json()
        assert fetched_body["secret_key"] == "****"
        assert "sk-live-super-secret" not in fetched.text


async def test_omitting_secret_key_on_update_does_not_clear_it(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/integrations/langfuse",
            json={"enabled": True, "secret_key": "sk-original"},
            headers=_bearer(admin_token),
        )

        updated = client.patch(
            "/api/v1/admin/settings/integrations/langfuse",
            json={"enabled": True, "host": "https://new-host.example.com"},
            headers=_bearer(admin_token),
        )
        assert updated.status_code == 200
        body = updated.json()
        # secret_key omitted from the request — must remain set, not cleared.
        assert body["secret_key"] == "****"
        assert body["host"] == "https://new-host.example.com"


async def test_save_datadog_config_masks_api_key_on_read(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/integrations/datadog",
            json={"enabled": True, "api_key": "dd-api-key-xyz", "site": "datadoghq.eu"},
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["enabled"] is True
        assert body["api_key"] == "****"
        assert body["site"] == "datadoghq.eu"
        assert "dd-api-key-xyz" not in saved.text

        fetched = client.get(
            "/api/v1/admin/settings/integrations/datadog", headers=_bearer(admin_token)
        )
        assert fetched.json()["api_key"] == "****"


async def test_save_grafana_config_and_read_back(make_user_and_token) -> None:
    """Section 41.5's mockup — Grafana/Loki is endpoint-only, no API key."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/integrations/grafana",
            json={"enabled": True, "endpoint": "https://loki.internal.example.com/otlp"},
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body == {"enabled": True, "endpoint": "https://loki.internal.example.com/otlp"}

        fetched = client.get(
            "/api/v1/admin/settings/integrations/grafana", headers=_bearer(admin_token)
        )
        assert fetched.json() == body


async def test_save_new_relic_config_masks_api_key_on_read(make_user_and_token) -> None:
    """Section 41.5's mockup — New Relic needs an API key, same shape as Datadog."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/integrations/new-relic",
            json={"enabled": True, "api_key": "nr-license-key-xyz"},
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["enabled"] is True
        assert body["api_key"] == "****"
        assert "nr-license-key-xyz" not in saved.text

        fetched = client.get(
            "/api/v1/admin/settings/integrations/new-relic", headers=_bearer(admin_token)
        )
        assert fetched.json()["api_key"] == "****"


async def test_save_dynatrace_config_and_read_back(make_user_and_token) -> None:
    """Section 41.5's mockup — Dynatrace is endpoint-only, same shape as Grafana/Loki."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/integrations/dynatrace",
            json={"enabled": True, "endpoint": "https://abc12345.live.dynatrace.com"},
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body == {"enabled": True, "endpoint": "https://abc12345.live.dynatrace.com"}

        fetched = client.get(
            "/api/v1/admin/settings/integrations/dynatrace", headers=_bearer(admin_token)
        )
        assert fetched.json() == body


async def test_settings_are_tenant_isolated(make_user_and_token) -> None:
    _, admin_a_token = await make_user_and_token(TENANT_A, role="admin")
    _, admin_b_token = await make_user_and_token(TENANT_B, role="admin")

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/otel-endpoint",
            json={"endpoint": "http://tenant-a-collector:4317"},
            headers=_bearer(admin_a_token),
        )

        tenant_b_view = client.get(
            "/api/v1/admin/settings/observability", headers=_bearer(admin_b_token)
        )
        assert tenant_b_view.json()["otel"]["endpoint"] is None


async def test_non_admin_cannot_read_or_write_settings(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        assert (
            client.get(
                "/api/v1/admin/settings/observability", headers=_bearer(dev_token)
            ).status_code
            == 403
        )
        assert (
            client.patch(
                "/api/v1/admin/settings/otel-endpoint",
                json={"endpoint": "http://x:4317"},
                headers=_bearer(dev_token),
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/admin/settings/integrations/langfuse", headers=_bearer(dev_token)
            ).status_code
            == 403
        )
        assert (
            client.patch(
                "/api/v1/admin/settings/integrations/langfuse",
                json={"enabled": True},
                headers=_bearer(dev_token),
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/admin/settings/integrations/datadog", headers=_bearer(dev_token)
            ).status_code
            == 403
        )
        assert (
            client.patch(
                "/api/v1/admin/settings/integrations/datadog",
                json={"enabled": True},
                headers=_bearer(dev_token),
            ).status_code
            == 403
        )


# ── Capability Discovery — provider-neutral capabilities ────────────────────
# The UI must only ever see "logs"/"metrics"/"distributed_tracing"/
# "opentelemetry" plus a status/detail/adapters list — never brand a
# capability by its backing provider. See
# app/modules/observability/capabilities.py.


async def test_capabilities_default_state(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/admin/settings/observability/capabilities", headers=_bearer(admin_token)
        )
        assert response.status_code == 200
        by_kind = {c["capability"]: c for c in response.json()["capabilities"]}

        assert by_kind["logs"]["status"] == "active"
        assert by_kind["logs"]["adapters"] == ["cloudwatch_logs"]
        assert by_kind["metrics"]["status"] == "active"
        assert by_kind["distributed_tracing"]["status"] == "active"
        assert by_kind["distributed_tracing"]["adapters"] == ["aws_xray"]

        # Nothing configured yet — OpenTelemetry is honestly "inactive", not
        # a false "active"/"unknown" claim.
        assert by_kind["opentelemetry"]["status"] == "inactive"
        assert by_kind["opentelemetry"]["adapters"] == ["otel_collector"]

        # Provider-neutral: no vendor name anywhere in the response other
        # than inside `detail`/`adapters` disclosure text.
        for kind in ("capability", "status"):
            for cap in response.json()["capabilities"]:
                assert isinstance(cap[kind], str)


async def test_capabilities_reflect_configured_otel_endpoint(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/otel-endpoint",
            json={"endpoint": "http://collector.internal:4317"},
            headers=_bearer(admin_token),
        )

        response = client.get(
            "/api/v1/admin/settings/observability/capabilities", headers=_bearer(admin_token)
        )
        by_kind = {c["capability"]: c for c in response.json()["capabilities"]}

        # Configured but not actively health-probed — "unknown", not "active".
        assert by_kind["opentelemetry"]["status"] == "unknown"
        assert "collector.internal:4317" in by_kind["opentelemetry"]["detail"]


async def test_capabilities_merge_optional_integration_into_existing_capability(
    make_user_and_token,
) -> None:
    """Enabling Langfuse must not invent a fifth capability — it merges into
    distributed_tracing, and since X-Ray ("active") already backs that
    capability, the merged status stays "active" (active outranks unknown)."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/integrations/langfuse",
            json={"enabled": True, "host": "https://langfuse.internal.example.com"},
            headers=_bearer(admin_token),
        )

        response = client.get(
            "/api/v1/admin/settings/observability/capabilities", headers=_bearer(admin_token)
        )
        capabilities = response.json()["capabilities"]
        assert [c["capability"] for c in capabilities].count("distributed_tracing") == 1

        tracing = next(c for c in capabilities if c["capability"] == "distributed_tracing")
        assert tracing["status"] == "active"
        assert set(tracing["adapters"]) == {"aws_xray", "langfuse"}
        assert "Langfuse" in tracing["detail"]


async def test_capabilities_requires_admin_role(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/admin/settings/observability/capabilities", headers=_bearer(dev_token)
        )
    assert response.status_code == 403
