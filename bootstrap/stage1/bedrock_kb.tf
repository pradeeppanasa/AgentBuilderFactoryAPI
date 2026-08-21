# Bedrock Knowledge Base service role (instructions_kb_api.md / CLAUDE.md
# Section 43). Bedrock assumes this role itself to read source documents
# from the kb_documents bucket and write vectors to OpenSearch Serverless
# during ingestion — distinct from the Runtime's own ambient IAM role
# (app.modules.knowledge_base.provisioner.BedrockKnowledgeBaseProvisioner
# passes this role's ARN as CreateKnowledgeBase's `roleArn`, it never runs
# ingestion under the Runtime's own credentials).

resource "aws_iam_role" "bedrock_kb" {
  name = "${local.name_prefix}-bedrock-kb-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "bedrock_kb_s3" {
  name = "${local.name_prefix}-bedrock-kb-s3"
  role = aws_iam_role.bedrock_kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = [
        aws_s3_bucket.app["kb_documents"].arn,
        "${aws_s3_bucket.app["kb_documents"].arn}/*",
      ]
    }]
  })
}

resource "aws_iam_role_policy" "bedrock_kb_opensearch" {
  name = "${local.name_prefix}-bedrock-kb-opensearch"
  role = aws_iam_role.bedrock_kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["aoss:APIAccessAll"]
      Resource = [var.opensearch_collection_arn]
    }]
  })
}
