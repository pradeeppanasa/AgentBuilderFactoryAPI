"""CI/CD workflow generation (CLAUDE.md Section 45.6, R58) — one test per
provider, plus the approval_mode gating rule shared by all five: Stage 5
(the approval gate) appears only in "manual" mode and is omitted entirely
in "automated" mode (F1's default — POLICY_CHECK is the only gate)."""

from __future__ import annotations

import pytest
import yaml

from app.modules.iac_generator.cicd_templates import generate_cicd_workflow

_ALL_PROVIDERS = ["github_actions", "gitlab_ci", "azure_devops", "codebuild", "bitbucket"]
_AGENT_ID = "faq-agent-9046a4"
_TF_DIR = f"terraform/agents/{_AGENT_ID}"

_EXPECTED_PATHS = {
    "github_actions": ".github/workflows/panasa-deploy.yml",
    "gitlab_ci": ".gitlab-ci.yml",
    "azure_devops": "azure-pipelines.yml",
    "codebuild": "buildspec.yml",
    "bitbucket": "bitbucket-pipelines.yml",
}


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_generates_the_spec_named_file_path(provider: str) -> None:
    path, _content = generate_cicd_workflow(provider, "automated", _AGENT_ID)
    assert path == _EXPECTED_PATHS[provider]


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_all_seven_stages_named_in_automated_mode_except_the_omitted_gate(provider: str) -> None:
    _path, content = generate_cicd_workflow(provider, "automated", _AGENT_ID)
    for stage_name in (
        "Stage 1",
        "Stage 2",
        "Stage 3",
        "Stage 4",
        "Stage 6",
        "Stage 7",
    ):
        assert stage_name in content
    assert "Stage 5" not in content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_stage_5_gate_present_only_in_manual_mode(provider: str) -> None:
    _path, manual_content = generate_cicd_workflow(provider, "manual", _AGENT_ID)
    _path, automated_content = generate_cicd_workflow(provider, "automated", _AGENT_ID)

    assert "Stage 5" in manual_content
    assert "Stage 5" not in automated_content
    assert manual_content != automated_content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_content_mentions_terraform_plan_and_apply(provider: str) -> None:
    _path, content = generate_cicd_workflow(provider, "automated", _AGENT_ID)
    assert "terraform plan" in content
    assert "terraform apply" in content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_terraform_apply_uses_the_saved_plan_not_auto_approve(provider: str) -> None:
    """A real apply must apply exactly what was planned/reviewed, not
    silently re-plan-and-approve everything from scratch."""
    _path, content = generate_cicd_workflow(provider, "automated", _AGENT_ID)
    assert "terraform apply tfplan" in content
    assert "-auto-approve" not in content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_terraform_commands_run_in_the_agents_own_flat_directory(provider: str) -> None:
    """backends/terraform.py generates terraform/agents/{agent_id}/*.tf —
    flat, not nested, and never at the repo root (Generic Agent Runtime
    instruction) — every provider's terraform commands must target that
    exact directory or `terraform init` finds zero .tf files."""
    _path, content = generate_cicd_workflow(provider, "automated", _AGENT_ID)
    assert _TF_DIR in content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_notifies_panasa_deployment_complete_webhook(provider: str) -> None:
    _path, content = generate_cicd_workflow(provider, "automated", _AGENT_ID)
    assert "deployment-metadata.json" in content
    assert "/api/v1/internal/deployment-complete" in content
    assert "PANASA_WEBHOOK_URL" in content
    assert "PANASA_WEBHOOK_SECRET" in content


def test_github_actions_auto_merges_pr_only_in_automated_mode() -> None:
    _path, automated_content = generate_cicd_workflow("github_actions", "automated", _AGENT_ID)
    _path, manual_content = generate_cicd_workflow("github_actions", "manual", _AGENT_ID)

    assert "gh pr merge --auto" in automated_content
    assert "gh pr merge --auto" not in manual_content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
@pytest.mark.parametrize("approval_mode", ["automated", "manual"])
def test_generated_file_is_valid_yaml(provider: str, approval_mode: str) -> None:
    """A shell command embedded in an unquoted YAML scalar (e.g. `curl ...
    -H "Authorization: Bearer $X"`) silently breaks the whole file — YAML
    reads the colon-space inside the header value as a mapping key. Every
    generated file must actually parse, not just "look right"."""
    _path, content = generate_cicd_workflow(provider, approval_mode, _AGENT_ID)
    yaml.safe_load(content)  # raises yaml.YAMLError on malformed content


def test_unknown_provider_raises() -> None:
    with pytest.raises(KeyError):
        generate_cicd_workflow("unknown_provider", "automated", _AGENT_ID)  # type: ignore[arg-type]
