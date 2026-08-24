"""CI/CD workflow file generation (CLAUDE.md Section 45.6, R58).

R58: "Panasa generates a CI/CD workflow file for the customer's selected
provider. The file is committed to the IaC repo. The customer's pipeline
executes it. Panasa never executes CI/CD on the customer's behalf."

Every provider template implements the same Section 45.5 6-stage pipeline
in provider-native YAML — the logic is identical, only the syntax differs:

    1. Source & security scanning (SAST, secret scan, IaC scan, dependency
       scan, SBOM, container scan)
    2. Terraform checks (fmt, init, validate)
    3. Panasa policy checks (IAM least privilege, public exposure,
       encryption at rest, network rules, tags, naming, custom policies)
    4. Terraform plan (posted as a PR/MR comment)
    5. Gate — see below
    6. Terraform apply

Stage 5 is the one place a workflow's shape actually depends on tenant
config (Section 45.3/R50, resolved as configurable — see
app/modules/deployment/models.py's module docstring): a "manual"-mode
tenant gets a real provider-native manual-approval step here; an
"automated"-mode tenant (F1's default, unchanged) has POLICY_CHECK decide
PASS/BLOCK entirely on its own, so Stage 5 is omitted from the generated
workflow file entirely rather than rendered as a no-op — there is nothing
for a human to click, and a placeholder step would misleadingly suggest
otherwise.

Committed once, when an agent's repo is first created (Section 45.2's v1
case) — not rewritten on every subsequent deploy, and not rewritten if the
tenant's cicd_provider setting changes after the fact (see
PlatformSettingsRecord.cicd_provider's docstring).
"""

from __future__ import annotations

from app.modules.deployment.models import ApprovalMode, CICDProvider

# Stage 1 commands are illustrative of the five scan types R58/45.5 name,
# not a specific vendor endorsement — a customer is free to swap tools in
# their own copy of the generated file; Panasa never re-reads it afterwards.
_SCAN_COMMANDS = [
    ("SAST", "semgrep --config auto ."),
    ("Secret scan", "trufflehog filesystem . --fail"),
    ("IaC scan", "checkov -d . && tfsec ."),
    ("Dependency scan", "safety check --full-report"),
    ("SBOM", "syft . -o spdx-json=sbom.json"),
    ("Container scan", "trivy fs --exit-code 1 --severity HIGH,CRITICAL ."),
]

_POLICY_CHECK_COMMAND = "panasa-policy-check ."
"""IAM least privilege, public exposure, encryption at rest, network rules
(no 0.0.0.0/0 except 443), required tags, naming convention, and any
customer-added custom policies — Section 45.5 Stage 3. A single vendored
script/binary the customer's pipeline calls; this Runtime never runs it
itself (R57)."""


def generate_cicd_workflow(provider: CICDProvider, approval_mode: ApprovalMode) -> tuple[str, str]:
    """Returns (repo-relative file path, file content) for `provider`,
    with Stage 5 rendered only when `approval_mode == "manual"`."""
    generators = {
        "github_actions": _github_actions,
        "gitlab_ci": _gitlab_ci,
        "azure_devops": _azure_devops,
        "codebuild": _codebuild,
        "bitbucket": _bitbucket,
    }
    return generators[provider](approval_mode)


def _github_actions(approval_mode: ApprovalMode) -> tuple[str, str]:
    scan_steps = "\n".join(
        f"""      - name: {name}
        run: {command}"""
        for name, command in _SCAN_COMMANDS
    )
    gate_job = (
        """
  gate:
    name: "Stage 5 — Approval gate"
    needs: terraform_plan
    runs-on: ubuntu-latest
    environment: production  # a required reviewer on this environment is the approval gate
    steps:
      - run: echo "Awaiting manual approval via the 'production' environment's protection rule."
"""
        if approval_mode == "manual"
        else ""
    )
    apply_needs = "gate" if approval_mode == "manual" else "terraform_plan"
    content = f"""name: Panasa Deploy

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

jobs:
  security_scan:
    name: "Stage 1 — Source & security scanning"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
{scan_steps}

  terraform_checks:
    name: "Stage 2 — Terraform checks"
    needs: security_scan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform fmt -check
      - run: terraform init
      - run: terraform validate

  policy_check:
    name: "Stage 3 — Panasa policy checks"
    needs: terraform_checks
    runs-on: ubuntu-latest
    steps:
      - run: {_POLICY_CHECK_COMMAND}

  terraform_plan:
    name: "Stage 4 — Terraform plan"
    needs: policy_check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init
      - run: terraform plan -out=tfplan
      - name: Post plan as PR comment
        if: github.event_name == 'pull_request'
        run: gh pr comment ${{{{ github.event.pull_request.number }}}} --body-file tfplan.txt
        env:
          GH_TOKEN: ${{{{ github.token }}}}
{gate_job}
  terraform_apply:
    name: "Stage 6 — Terraform apply"
    needs: {apply_needs}
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init
      - run: terraform apply -auto-approve
"""
    return ".github/workflows/panasa-deploy.yml", content


def _gitlab_ci(approval_mode: ApprovalMode) -> tuple[str, str]:
    scan_script = "\n".join(f"    - {command}" for _name, command in _SCAN_COMMANDS)
    gate_stage = "  - gate\n" if approval_mode == "manual" else ""
    gate_job = (
        """
gate:  # Stage 5 — Approval gate
  stage: gate
  script:
    - echo "Awaiting manual approval."
  when: manual
  needs: ["terraform_plan"]
"""
        if approval_mode == "manual"
        else ""
    )
    apply_needs = '["gate"]' if approval_mode == "manual" else '["terraform_plan"]'
    content = f"""stages:
  - security_scan
  - terraform_checks
  - policy_check
  - terraform_plan
{gate_stage}  - terraform_apply

security_scan:  # Stage 1 — Source & security scanning
  stage: security_scan
  script:
{scan_script}

terraform_checks:  # Stage 2 — Terraform checks
  stage: terraform_checks
  needs: ["security_scan"]
  script:
    - terraform fmt -check
    - terraform init
    - terraform validate

policy_check:  # Stage 3 — Panasa policy checks
  stage: policy_check
  needs: ["terraform_checks"]
  script:
    - {_POLICY_CHECK_COMMAND}

terraform_plan:  # Stage 4 — Terraform plan
  stage: terraform_plan
  needs: ["policy_check"]
  script:
    - terraform init
    - terraform plan -out=tfplan
  artifacts:
    paths:
      - tfplan
{gate_job}
terraform_apply:  # Stage 6 — Terraform apply
  stage: terraform_apply
  needs: {apply_needs}
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
  script:
    - terraform init
    - terraform apply -auto-approve
"""
    return ".gitlab-ci.yml", content


def _azure_devops(approval_mode: ApprovalMode) -> tuple[str, str]:
    scan_steps = "\n".join(
        f"""      - script: {command}
        displayName: "{name}\""""
        for name, command in _SCAN_COMMANDS
    )
    apply_env = (
        "production"  # a required approver on this Azure environment is the approval gate
        if approval_mode == "manual"
        else "production-auto"
    )
    gate_note = (
        "    # 'production' environment has a required-approver check configured "
        "in Azure DevOps — that check IS Stage 5.\n"
        if approval_mode == "manual"
        else "    # approval_mode=automated: no environment approval check configured; "
        "POLICY_CHECK below is the only gate.\n"
    )
    content = f"""trigger:
  branches:
    include: [main]

stages:
  - stage: SecurityScan
    displayName: "Stage 1 — Source & security scanning"
    jobs:
      - job: Scan
        steps:
{scan_steps}

  - stage: TerraformChecks
    displayName: "Stage 2 — Terraform checks"
    dependsOn: SecurityScan
    jobs:
      - job: Checks
        steps:
          - script: terraform fmt -check
          - script: terraform init
          - script: terraform validate

  - stage: PolicyCheck
    displayName: "Stage 3 — Panasa policy checks"
    dependsOn: TerraformChecks
    jobs:
      - job: Policy
        steps:
          - script: {_POLICY_CHECK_COMMAND}

  - stage: TerraformPlan
    displayName: "Stage 4 — Terraform plan"
    dependsOn: PolicyCheck
    jobs:
      - job: Plan
        steps:
          - script: terraform init
          - script: terraform plan -out=tfplan
          - script: az repos pr comment --content "$(cat tfplan.txt)" || true

  - stage: TerraformApply
    displayName: "Stage 6 — Terraform apply"
    dependsOn: TerraformPlan
    condition: eq(variables['Build.SourceBranch'], 'refs/heads/main')
    jobs:
      - deployment: Apply
        environment: {apply_env}
{gate_note}        strategy:
          runOnce:
            deploy:
              steps:
                - script: terraform init
                - script: terraform apply -auto-approve
"""
    return "azure-pipelines.yml", content


def _codebuild(approval_mode: ApprovalMode) -> tuple[str, str]:
    scan_commands = "\n".join(f"      - {command}" for _name, command in _SCAN_COMMANDS)
    gate_note = (
        "  # Stage 5 (manual approval): configure a Manual Approval action "
        "between the Plan and Apply stages in the CodePipeline definition — "
        "not expressible inside buildspec.yml itself.\n"
        if approval_mode == "manual"
        else "  # approval_mode=automated: no Manual Approval action in the "
        "CodePipeline definition; POLICY_CHECK below is the only gate.\n"
    )
    content = f"""version: 0.2

# Section 45.5's 6 stages map onto CodeBuild's phases below; the approval
# gate (manual mode only) is a CodePipeline-level Manual Approval action
# around this build project, not something buildspec.yml expresses on its
# own — see the note ahead of the apply phase.

phases:
  install:
    commands:
      - echo "Stage 1 — Source & security scanning"
{scan_commands}
  pre_build:
    commands:
      - echo "Stage 2 — Terraform checks"
      - terraform fmt -check
      - terraform init
      - terraform validate
      - echo "Stage 3 — Panasa policy checks"
      - {_POLICY_CHECK_COMMAND}
  build:
    commands:
      - echo "Stage 4 — Terraform plan"
      - terraform plan -out=tfplan
{gate_note}      - echo "Stage 6 — Terraform apply"
      - terraform apply -auto-approve
"""
    return "buildspec.yml", content


def _bitbucket(approval_mode: ApprovalMode) -> tuple[str, str]:
    scan_steps = "\n".join(f"          - {command}" for _name, command in _SCAN_COMMANDS)
    gate_step = (
        """      - step:
          name: "Stage 5 — Approval gate"
          trigger: manual
          script:
            - echo "Manually triggered — this step itself is the approval gate."
"""
        if approval_mode == "manual"
        else ""
    )
    content = f"""pipelines:
  branches:
    main:
      - step:
          name: "Stage 1 — Source & security scanning"
          script:
{scan_steps}
      - step:
          name: "Stage 2 — Terraform checks"
          script:
            - terraform fmt -check
            - terraform init
            - terraform validate
      - step:
          name: "Stage 3 — Panasa policy checks"
          script:
            - {_POLICY_CHECK_COMMAND}
      - step:
          name: "Stage 4 — Terraform plan"
          script:
            - terraform init
            - terraform plan -out=tfplan
          artifacts:
            - tfplan
{gate_step}      - step:
          name: "Stage 6 — Terraform apply"
          script:
            - terraform init
            - terraform apply -auto-approve
"""
    return "bitbucket-pipelines.yml", content
