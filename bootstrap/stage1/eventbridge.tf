# EventBridge bus (CLAUDE.md Section 6.1) — the Runtime's
# modules.deployment.orchestrator.trigger_deployment() publishes
# AgentDeploymentRequested here; a rule below starts the Step Functions
# execution (Section 6.1/6.2 — no inbound webhooks required, per that
# section's closing note).

resource "aws_cloudwatch_event_bus" "deployment" {
  name = "${var.resource_prefix}-agent-builder"
}

resource "aws_cloudwatch_event_rule" "deployment_requested" {
  name           = "${local.name_prefix}-deployment-requested"
  event_bus_name = aws_cloudwatch_event_bus.deployment.name

  event_pattern = jsonencode({
    source      = ["panasa.agent-builder"]
    detail-type = ["AgentDeploymentRequested"]
  })
}

resource "aws_iam_role" "eventbridge_sfn_trigger" {
  name = "${local.name_prefix}-eventbridge-sfn-trigger"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_sfn_trigger" {
  name = "${local.name_prefix}-eventbridge-sfn-trigger"
  role = aws_iam_role.eventbridge_sfn_trigger.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [aws_sfn_state_machine.deployment.arn]
    }]
  })
}

resource "aws_cloudwatch_event_target" "start_deployment_workflow" {
  event_bus_name = aws_cloudwatch_event_bus.deployment.name
  rule           = aws_cloudwatch_event_rule.deployment_requested.name
  arn            = aws_sfn_state_machine.deployment.arn
  role_arn       = aws_iam_role.eventbridge_sfn_trigger.arn

  # Step Functions input needs top-level deploymentId/agentId/tenantId
  # (matching deployment_workflow.json's $.deploymentId etc.) — the actual
  # EventBridge detail payload nests these one level down (see
  # orchestrator.trigger_deployment's Detail JSON), so this reshapes it.
  input_transformer {
    input_paths = {
      deploymentId = "$.detail.deploymentId"
      agentId      = "$.detail.agentId"
      version      = "$.detail.version"
      tenantId     = "$.detail.tenantId"
    }
    input_template = "{\"deploymentId\": <deploymentId>, \"agentId\": <agentId>, \"version\": <version>, \"tenantId\": <tenantId>}"
  }
}
