# RDS Postgres — not itemized anywhere in CLAUDE.md Section 9/A13's Stage 1
# resource list, added here because Phase 3 (JWT Auth + RBAC) requires a
# real Postgres via SQLAlchemy/Alembic for user accounts, and nothing else
# in the spec provisions one. Single small instance, private subnets only.
#
# When var.deploy_langfuse is true, Langfuse's own Postgres database lives
# on this same instance (a second `langfuse` database, not a second
# instance) — reasonable resource-sharing at prototype scale; split it out
# before this needs to carry real production trace volume.

resource "aws_db_subnet_group" "main" {
  name       = "${local.name_prefix}-db"
  subnet_ids = [for s in aws_subnet.private : s.id]
}

resource "random_password" "db_master" {
  length  = 32
  special = false # avoid characters Postgres connection strings would need escaping
}

resource "aws_secretsmanager_secret" "db_master_password" {
  name       = "${var.resource_prefix}/rds-master-password"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "db_master_password" {
  secret_id     = aws_secretsmanager_secret.db_master_password.id
  secret_string = random_password.db_master.result
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name_prefix}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage_gb
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn
  db_subnet_group_name  = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]

  db_name  = "panasa_agent_builder"
  username = "panasa"
  password = random_password.db_master.result

  backup_retention_period = 7
  multi_az                = var.environment == "enterprise"
  skip_final_snapshot     = var.environment != "enterprise"
  deletion_protection     = var.environment == "enterprise"

  tags = { Name = "${local.name_prefix}-db" }
}

resource "aws_secretsmanager_secret" "database_url" {
  name       = "${var.resource_prefix}/database-url"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+asyncpg://${aws_db_instance.main.username}:${random_password.db_master.result}@${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}"
}

resource "aws_secretsmanager_secret" "langfuse_database_url" {
  count      = var.deploy_langfuse ? 1 : 0
  name       = "${var.resource_prefix}/langfuse-database-url"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "langfuse_database_url" {
  count     = var.deploy_langfuse ? 1 : 0
  secret_id = aws_secretsmanager_secret.langfuse_database_url[0].id
  # Points at the SAME logical database as the Runtime's own Postgres
  # (aws_db_instance.main.db_name), not a separate "langfuse" database —
  # Terraform's aws_db_instance can only create the one database named by
  # db_name at instance creation; provisioning a second one needs either a
  # migration step with network access to the (private-subnet-only) RDS
  # instance or a manually-run `CREATE DATABASE`, neither of which belongs
  # in this bootstrap apply. Langfuse's own tables (traces, observations,
  # ...) don't collide with Phase 3's `users` table, so sharing is safe at
  # prototype scale; split this onto its own database/instance before real
  # production trace volume.
  secret_string = "postgresql://${aws_db_instance.main.username}:${random_password.db_master.result}@${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}"
}
