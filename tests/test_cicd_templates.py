"""CI/CD workflow generation (CLAUDE.md Section 45.6, R58) — one test per
provider, plus the approval_mode gating rule shared by all five: Stage 5
(the approval gate) appears only in "manual" mode and is omitted entirely
in "automated" mode (F1's default — POLICY_CHECK is the only gate)."""

from __future__ import annotations

import pytest

from app.modules.iac_generator.cicd_templates import generate_cicd_workflow

_ALL_PROVIDERS = ["github_actions", "gitlab_ci", "azure_devops", "codebuild", "bitbucket"]

_EXPECTED_PATHS = {
    "github_actions": ".github/workflows/panasa-deploy.yml",
    "gitlab_ci": ".gitlab-ci.yml",
    "azure_devops": "azure-pipelines.yml",
    "codebuild": "buildspec.yml",
    "bitbucket": "bitbucket-pipelines.yml",
}


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_generates_the_spec_named_file_path(provider: str) -> None:
    path, _content = generate_cicd_workflow(provider, "automated")
    assert path == _EXPECTED_PATHS[provider]


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_all_six_stages_named_in_automated_mode_except_the_omitted_gate(provider: str) -> None:
    _path, content = generate_cicd_workflow(provider, "automated")
    for stage_name in (
        "Stage 1",
        "Stage 2",
        "Stage 3",
        "Stage 4",
        "Stage 6",
    ):
        assert stage_name in content
    assert "Stage 5" not in content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_stage_5_gate_present_only_in_manual_mode(provider: str) -> None:
    _path, manual_content = generate_cicd_workflow(provider, "manual")
    _path, automated_content = generate_cicd_workflow(provider, "automated")

    assert "Stage 5" in manual_content
    assert "Stage 5" not in automated_content
    assert manual_content != automated_content


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_content_mentions_terraform_plan_and_apply(provider: str) -> None:
    _path, content = generate_cicd_workflow(provider, "automated")
    assert "terraform plan" in content
    assert "terraform apply" in content


def test_unknown_provider_raises() -> None:
    with pytest.raises(KeyError):
        generate_cicd_workflow("unknown_provider", "automated")  # type: ignore[arg-type]
