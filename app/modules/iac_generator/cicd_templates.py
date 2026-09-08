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
    7. Notify Panasa — Generic Agent Runtime instruction (2026-09-03,
       Part 3/4/6): POSTs deployment-metadata.json (committed alongside
       the Terraform every deploy, app/api/v1/agents.py) to
       POST /api/v1/internal/deployment-complete once apply succeeds —
       the one thing the Runtime genuinely cannot observe on its own
       (F0/R03: it never touches Terraform state or the customer's AWS
       account directly).

Stage 5 is the one place a workflow's shape actually depends on tenant
config (Section 45.3/R50, resolved as configurable — see
app/modules/deployment/models.py's module docstring): a "manual"-mode
tenant gets a real provider-native manual-approval step here; an
"automated"-mode tenant (F1's default, unchanged) has POLICY_CHECK decide
PASS/BLOCK entirely on its own, so Stage 5 is omitted from the generated
workflow file entirely rather than rendered as a no-op — there is nothing
for a human to click, and a placeholder step would misleadingly suggest
otherwise. In automated mode, the PR that Stage 3/POLICY_CHECK passed on
is auto-merged by the workflow itself (GitHub Actions only, today — see
_github_actions's auto_merge_step) rather than requiring a round trip back
to the Factory Runtime (F5's "Runtime polls DynamoDB, then merges" describes
the Step-Functions-pipeline path this codebase doesn't run yet; a
plain-CI-native auto-merge is what's real today, mirroring the
deployment-complete webhook's same "the real pipeline is CI-native, not a
Panasa-orchestrated one" reasoning).

Every terraform command runs from terraform/agents/{agent_id} — the FLAT
directory backends/terraform.py actually generates (Generic Agent Runtime
instruction) — never the repo root, which has no .tf files directly in it.

Committed once, the first time it's genuinely absent from the repo's default
branch (normally Section 45.2's v1 case) — not rewritten on every subsequent
deploy, and not rewritten if the tenant's cicd_provider setting changes after
the fact (see PlatformSettingsRecord.cicd_provider's docstring). The caller
(app/api/v1/agents.py's _trigger_deployment) decides "genuinely absent" via
GitProvider.file_exists() rather than repo existence alone, since a repo can
exist without ever having received this file — e.g. create_repository()
succeeded but that same attempt's commit_files() call then failed.
"""

from __future__ import annotations

from app.modules.deployment.models import ApprovalMode, CICDProvider
from app.modules.iac_generator.policy_check_script import generate_policy_check_script

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

_NOTIFY_CURL = (
    'curl -sS -f -X POST "$PANASA_WEBHOOK_URL/api/v1/internal/deployment-complete" '
    '-H "Authorization: Bearer $PANASA_WEBHOOK_SECRET" '
    '-H "Content-Type: application/json" '
    "--data-binary @deployment-metadata.json"
)
"""Run FROM the terraform/agents/{agent_id} working directory —
deployment-metadata.json (agent_id/tenant_id/deployment_id/version/status,
regenerated every deploy) sits right next to the .tf files, same as
terraform.auto.tfvars.json."""

_DEPLOY_ROLE_NAME = "panasa-deploy-role"
"""Must match bootstrap/stage1/github_oidc.tf's aws_iam_role name exactly
— that Terraform creates the role this workflow assumes; there is no
Terraform output wired to this Python constant (bootstrap is a one-time,
human-run step, not something the Runtime reads from — R03/F0), so the
two are kept in sync by convention, both using the default
var.resource_prefix ("panasa"). If a customer ever changes resource_prefix
away from the default, this constant must be updated to match."""

_AWS_CREDENTIALS_STEP = f"""      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::${{{{ secrets.AWS_ACCOUNT_ID }}}}:role/{_DEPLOY_ROLE_NAME}
          aws-region: ${{{{ secrets.AWS_REGION }}}}
"""
"""Sprint 3 Phase 6 (S-04) — GitHub OIDC, not static long-lived keys.
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are gone from GitHub Secrets
entirely; only AWS_ACCOUNT_ID (not sensitive on its own, but kept in
Secrets for consistency with the other two) and AWS_REGION remain. The
`permissions: id-token: write` block this needs is set once at the
workflow level (see content= below), not per-step — GitHub's own
requirement for the OIDC token exchange configure-aws-credentials@v4
performs.

GitHub-only for now (other providers' own credential-injection idioms —
GitLab CI/CD variables, Azure service connections, CodeBuild's environment
role, Bitbucket repository variables — differ enough not to share one
string; extend per-provider if/when those need the same treatment)."""

# GitHub Actions-specific — install steps interleaved directly before the
# scan command that needs them (some job setup already covers a tool no
# install step is listed for: checkov/tfsec ship via existing base image
# tooling assumptions the other 4 providers' _SCAN_COMMANDS share; not
# duplicated here to keep this list's shape a strict superset of that one).
_GITHUB_SCAN_STEPS = [
    ("SAST", "pip install semgrep safety", "semgrep --config auto ."),
    (
        "Secret scan",
        # One chained command, not two separate `run:` lines — a plain
        # YAML scalar spanning multiple lines folds them together with a
        # single space, which would silently mangle two shell commands
        # into one broken line (curl's own args swallowing apt-get's).
        "sudo apt-get update -qq && sudo apt-get install -y curl && "
        "curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/"
        "main/scripts/install.sh | sh -s -- -b /usr/local/bin",
        "trufflehog filesystem . --fail",
    ),
    ("IaC scan", None, "checkov -d . && tfsec ."),
    ("Dependency scan", "pip install semgrep safety", "safety check --full-report"),
    (
        "SBOM",
        "curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh",
        "syft . -o spdx-json=sbom.json",
    ),
    (
        "Container scan",
        "curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/"
        "install.sh | sh -s -- -b /usr/local/bin",
        "trivy fs --exit-code 1 --severity HIGH,CRITICAL .",
    ),
]


def generate_cicd_workflow(
    provider: CICDProvider, approval_mode: ApprovalMode, agent_id: str
) -> tuple[str, str]:
    """Returns (repo-relative file path, file content) for `provider`,
    with Stage 5 rendered only when `approval_mode == "manual"`.

    `agent_id` is baked into the working-directory paths below — Terraform
    lives at terraform/agents/{agent_id}, never the repo root, and that
    subdirectory can't be derived from anything GitHub/GitLab/etc. expose
    to a running workflow (the repo is named panasa-iac-{agent_id}, not
    {agent_id} — ${{ github.event.repository.name }} would resolve to the
    wrong path)."""
    tf_dir = f"terraform/agents/{agent_id}"
    generators = {
        "github_actions": _github_actions,
        "gitlab_ci": _gitlab_ci,
        "azure_devops": _azure_devops,
        "codebuild": _codebuild,
        "bitbucket": _bitbucket,
    }
    return generators[provider](approval_mode, tf_dir)


def _github_actions(_approval_mode: ApprovalMode, tf_dir: str) -> tuple[str, str]:
    """_approval_mode is unused here — deliberately, not an oversight: this
    provider's terraform_apply job always carries `environment: production`
    (a required-reviewer check on that GitHub Environment IS the R50
    approval gate — CLAUDE.md instruction, 2026-09-07), and the automated-
    mode-only auto-merge step this used to have is gone too. Every GitHub
    Actions deploy now goes through the same one shape; the other 4
    providers still branch on approval_mode for their own Stage 5 (see
    each's own gate_job/gate_note/gate_step)."""
    scan_steps = "\n".join(
        (
            f"""      - name: {name}
        run: {command}"""
            if install is None
            else f"""      - name: Install tooling for {name}
        run: {install}
      - name: {name}
        run: {command}"""
        )
        for name, install, command in _GITHUB_SCAN_STEPS
    )
    policy_check_script_path, _policy_check_script_content = generate_policy_check_script()
    content = f"""name: Panasa Deploy

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

# Sprint 3 Phase 6 (S-04) — required once, at workflow level, for
# aws-actions/configure-aws-credentials@v4's OIDC token exchange
# (bootstrap/stage1/github_oidc.tf's IAM OIDC provider trusts this
# token). Applies to every job below by default; a job would need its
# own `permissions:` block to narrow this further, none currently do.
permissions:
  id-token: write
  contents: read

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
    defaults:
      run:
        working-directory: {tf_dir}
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
{_AWS_CREDENTIALS_STEP}      - run: terraform fmt -check
      - run: terraform init
      - run: terraform validate

  policy_check:
    name: "Stage 3 — Panasa policy checks"
    needs: terraform_checks
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: hashicorp/setup-terraform@v3
      - run: pip install python-hcl2
      - name: Run Panasa policy checks
        run: python {policy_check_script_path} {tf_dir}

  terraform_plan:
    name: "Stage 4 — Terraform plan"
    needs: policy_check
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: {tf_dir}
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
{_AWS_CREDENTIALS_STEP}      - run: terraform init
      - run: terraform plan -out=tfplan
      - name: Post plan as PR comment
        if: github.event_name == 'pull_request'
        run: gh pr comment ${{{{ github.event.pull_request.number }}}} --body-file tfplan.txt
        env:
          GH_TOKEN: ${{{{ github.token }}}}
      - uses: actions/upload-artifact@v4
        with:
          name: tfplan
          path: {tf_dir}/tfplan

  terraform_apply:
    name: "Stage 6 — Terraform apply"
    needs: terraform_plan
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    # R50 human approval gate — set a required reviewer on this GitHub Environment
    environment: production
    defaults:
      run:
        working-directory: {tf_dir}
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
{_AWS_CREDENTIALS_STEP}      - uses: actions/download-artifact@v4
        with:
          name: tfplan
          path: {tf_dir}
      - run: terraform init
      - run: terraform apply tfplan

  notify_panasa:
    name: "Stage 7 — Notify Panasa (deployment complete)"
    needs: terraform_apply
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: {tf_dir}
    steps:
      - uses: actions/checkout@v4
      - run: |
          {_NOTIFY_CURL}
        env:
          PANASA_WEBHOOK_URL: ${{{{ secrets.PANASA_WEBHOOK_URL }}}}
          PANASA_WEBHOOK_SECRET: ${{{{ secrets.PANASA_WEBHOOK_SECRET }}}}
"""
    return ".github/workflows/panasa-deploy.yml", content


def _gitlab_ci(approval_mode: ApprovalMode, tf_dir: str) -> tuple[str, str]:
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
  - notify_panasa

security_scan:  # Stage 1 — Source & security scanning
  stage: security_scan
  script:
{scan_script}

terraform_checks:  # Stage 2 — Terraform checks
  stage: terraform_checks
  needs: ["security_scan"]
  script:
    - cd {tf_dir}
    - terraform fmt -check
    - terraform init
    - terraform validate

policy_check:  # Stage 3 — Panasa policy checks
  stage: policy_check
  needs: ["terraform_checks"]
  script:
    - {_POLICY_CHECK_COMMAND}
  # approval_mode=automated: GitLab auto-merges an MR that passed every
  # required pipeline stage when "merge when pipeline succeeds" (or an
  # equivalent merge-train setting) is enabled on the project — configure
  # that once per project rather than scripting a merge here; no
  # equivalent of GitHub CLI's `gh pr merge --auto` step is needed.

terraform_plan:  # Stage 4 — Terraform plan
  stage: terraform_plan
  needs: ["policy_check"]
  script:
    - cd {tf_dir}
    - terraform init
    - terraform plan -out=tfplan
  artifacts:
    paths:
      - {tf_dir}/tfplan
{gate_job}
terraform_apply:  # Stage 6 — Terraform apply
  stage: terraform_apply
  needs: {apply_needs}
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
  script:
    - cd {tf_dir}
    - terraform init
    - terraform apply tfplan

notify_panasa:  # Stage 7 — Notify Panasa (deployment complete)
  stage: notify_panasa
  needs: ["terraform_apply"]
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
  script:
    - cd {tf_dir}
    - '{_NOTIFY_CURL}'
"""
    return ".gitlab-ci.yml", content


def _azure_devops(approval_mode: ApprovalMode, tf_dir: str) -> tuple[str, str]:
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
        "POLICY_CHECK below is the only gate. Configure branch policy 'Build must "
        "succeed' + auto-complete on the PR to replicate GitHub Actions' auto-merge "
        "step — not expressible inside this YAML file itself.\n"
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
            workingDirectory: {tf_dir}
          - script: terraform init
            workingDirectory: {tf_dir}
          - script: terraform validate
            workingDirectory: {tf_dir}

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
            workingDirectory: {tf_dir}
          - script: terraform plan -out=tfplan
            workingDirectory: {tf_dir}
          - script: az repos pr comment --content "$(cat tfplan.txt)" || true
          - publish: {tf_dir}/tfplan
            artifact: tfplan

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
                - download: current
                  artifact: tfplan
                - script: cp $(Pipeline.Workspace)/tfplan/tfplan {tf_dir}/tfplan
                - script: terraform init
                  workingDirectory: {tf_dir}
                - script: terraform apply tfplan
                  workingDirectory: {tf_dir}

  - stage: NotifyPanasa
    displayName: "Stage 7 — Notify Panasa (deployment complete)"
    dependsOn: TerraformApply
    jobs:
      - job: Notify
        steps:
          - script: '{_NOTIFY_CURL}'
            workingDirectory: {tf_dir}
            env:
              PANASA_WEBHOOK_URL: $(PANASA_WEBHOOK_URL)
              PANASA_WEBHOOK_SECRET: $(PANASA_WEBHOOK_SECRET)
"""
    return "azure-pipelines.yml", content


def _codebuild(approval_mode: ApprovalMode, tf_dir: str) -> tuple[str, str]:
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

# Section 45.5's 6 stages (+ Stage 7 notify) map onto CodeBuild's phases
# below; the approval gate (manual mode only) is a CodePipeline-level
# Manual Approval action around this build project, not something
# buildspec.yml expresses on its own — see the note ahead of the apply
# phase. All terraform/tfplan commands run from {tf_dir} — a single
# CodeBuild job's phases share one filesystem, so the plan file left
# there by the plan phase is still there for the apply phase with no
# artifact-passing step needed (unlike GitHub Actions' separate jobs).

phases:
  install:
    commands:
      - echo "Stage 1 — Source & security scanning"
{scan_commands}
  pre_build:
    commands:
      - echo "Stage 2 — Terraform checks"
      - cd {tf_dir} && terraform fmt -check && cd -
      - cd {tf_dir} && terraform init && cd -
      - cd {tf_dir} && terraform validate && cd -
      - echo "Stage 3 — Panasa policy checks"
      - {_POLICY_CHECK_COMMAND}
  build:
    commands:
      - echo "Stage 4 — Terraform plan"
      - cd {tf_dir} && terraform plan -out=tfplan && cd -
{gate_note}      - echo "Stage 6 — Terraform apply"
      - cd {tf_dir} && terraform apply tfplan && cd -
      - echo "Stage 7 — Notify Panasa (deployment complete)"
      - 'cd {tf_dir} && {_NOTIFY_CURL} && cd -'
"""
    return "buildspec.yml", content


def _bitbucket(approval_mode: ApprovalMode, tf_dir: str) -> tuple[str, str]:
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
            - cd {tf_dir}
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
            - cd {tf_dir}
            - terraform init
            - terraform plan -out=tfplan
          artifacts:
            - {tf_dir}/tfplan
{gate_step}      - step:
          name: "Stage 6 — Terraform apply"
          script:
            - cd {tf_dir}
            - terraform init
            - terraform apply tfplan
      - step:
          name: "Stage 7 — Notify Panasa (deployment complete)"
          script:
            - cd {tf_dir}
            - '{_NOTIFY_CURL}'
"""
    return "bitbucket-pipelines.yml", content
