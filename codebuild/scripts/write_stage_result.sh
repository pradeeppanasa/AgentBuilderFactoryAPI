#!/usr/bin/env bash
# Shared by every Panasa deployment CodeBuild job (terraform-validate,
# terraform-plan, terraform-apply, ...). Writes one stage's result into the
# panasa-deployments item this build is running for.
#
# `stages` is stored as a single JSON-string attribute, not a native
# DynamoDB map (see app/modules/deployment/status_store.py) — so updating
# one stage is a read-modify-write of that whole string. That's safe here
# because the Step Functions workflow runs stages strictly sequentially
# (step_functions/deployment_workflow.json) — there is never a concurrent
# writer to race against.
#
# Usage: write_stage_result.sh <STAGE_NAME> <PASSED|FAILED|BLOCKED> [summary] [blocking_issue]
set -euo pipefail

STAGE_NAME="$1"
STAGE_STATUS="$2"
OUTPUT_SUMMARY="${3:-}"
BLOCKING_ISSUE="${4:-}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

: "${DYNAMODB_DEPLOYMENTS_TABLE:?required}"
: "${AGENT_ID:?required}"
: "${DEPLOYMENT_ID:?required}"

KEY="{\"agent_id\": {\"S\": \"${AGENT_ID}\"}, \"deployment_id\": {\"S\": \"${DEPLOYMENT_ID}\"}}"

ITEM=$(aws dynamodb get-item --table-name "$DYNAMODB_DEPLOYMENTS_TABLE" --key "$KEY")
STAGES_JSON=$(echo "$ITEM" | jq -r '.Item.stages.S')

UPDATED_STAGES=$(echo "$STAGES_JSON" | jq \
  --arg stage "$STAGE_NAME" \
  --arg status "$STAGE_STATUS" \
  --arg summary "$OUTPUT_SUMMARY" \
  --arg blocking "$BLOCKING_ISSUE" \
  --arg now "$NOW" \
  '.[$stage].status = $status
   | .[$stage].output_summary = ($summary | if . == "" then null else . end)
   | .[$stage].blocking_issue = ($blocking | if . == "" then null else . end)
   | .[$stage].completed_at = $now')

UPDATED_STAGES_ESCAPED=$(echo "$UPDATED_STAGES" | jq -Rs .)

aws dynamodb update-item \
  --table-name "$DYNAMODB_DEPLOYMENTS_TABLE" \
  --key "$KEY" \
  --update-expression "SET stages = :stages, current_stage = :stage, #st = :top_status, updated_at = :now" \
  --expression-attribute-names '{"#st": "status"}' \
  --expression-attribute-values "{
    \":stages\": {\"S\": ${UPDATED_STAGES_ESCAPED}},
    \":stage\": {\"S\": \"${STAGE_NAME}\"},
    \":top_status\": {\"S\": \"${STAGE_NAME}\"},
    \":now\": {\"S\": \"${NOW}\"}
  }"

echo "Wrote ${STAGE_NAME}=${STAGE_STATUS} for deployment ${DEPLOYMENT_ID}"
