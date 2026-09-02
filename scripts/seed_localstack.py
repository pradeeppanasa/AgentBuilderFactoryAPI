"""Seeds LocalStack with what the Runtime needs before its first boot:
the JWT signing secret, the git token secret, and the S3 buckets it reads/
writes at startup and at generate-iac/audit time.

Pure boto3 — deliberately does NOT shell out to the `aws` CLI the way
scripts/seed_local_secrets.sh does, since the AWS CLI isn't guaranteed to be
installed on a developer's machine (it isn't in this repo's own dev
environment) while boto3 always is (a core app dependency). Superseded by
this script; kept only as a reference for anyone who does have the CLI.

Run from the host venv with LocalStack already up
(scripts/local-setup.sh invokes this as `python -m scripts.seed_localstack`
— module mode, see create_admin.py's docstring for why). Values come from
.env via scripts/_dotenv.py, not os.environ — a plain `source .env` chokes
on .env.example's real-deployment placeholders like `panasa-transcripts-
<account>`, which bash parses as redirection syntax.
Idempotent — safe to run against an already-seeded LocalStack.
"""

from __future__ import annotations

import sys

import boto3
from botocore.config import Config

from scripts._dotenv import load_env

_ENV = load_env()
_ENDPOINT = _ENV.get("SECRETS_MANAGER_ENDPOINT", "http://localhost:4566")
_REGION = _ENV.get("AWS_REGION", "eu-west-2")
_ACCESS_KEY = _ENV.get("AWS_ACCESS_KEY_ID", "local")
_SECRET_KEY = _ENV.get("AWS_SECRET_ACCESS_KEY", "local")

_SECRETS = {
    "jwt-secret": "local-dev-jwt-signing-secret-not-for-production",
    "git-token": "local-dev-git-token-not-for-production",
}
_BUCKETS = [
    _ENV.get("IAC_OUTPUT_BUCKET", "panasa-iac-artifacts-local"),
    _ENV.get("AUDIT_S3_BUCKET", "panasa-audit-local"),
]
_EVENT_BUS = _ENV.get("EVENTBRIDGE_BUS_NAME", "panasa-agent-builder")


def _client(service: str) -> object:
    return boto3.client(
        service,
        region_name=_REGION,
        endpoint_url=_ENDPOINT,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _seed_secrets() -> None:
    client = _client("secretsmanager")
    for name, value in _SECRETS.items():
        try:
            client.create_secret(Name=name, SecretString=value)
            print(f"  created secret {name!r}")
        except client.exceptions.ResourceExistsException:
            print(f"  secret {name!r} already exists")


def _seed_buckets() -> None:
    client = _client("s3")
    for bucket in _BUCKETS:
        try:
            client.create_bucket(
                Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": _REGION}
            )
            print(f"  created bucket {bucket!r}")
        except (client.exceptions.BucketAlreadyOwnedByYou, client.exceptions.BucketAlreadyExists):
            print(f"  bucket {bucket!r} already exists")


def _seed_event_bus() -> None:
    # app.shared.aws_clients.create_eventbridge_client points at this same
    # LocalStack endpoint (EVENTBRIDGE_ENDPOINT) once configured — PutEvents
    # against a custom bus that was never created fails, so it needs the
    # same "ensure it exists" seeding the buckets/secrets above get.
    client = _client("events")
    try:
        client.create_event_bus(Name=_EVENT_BUS)
        print(f"  created event bus {_EVENT_BUS!r}")
    except client.exceptions.ResourceAlreadyExistsException:
        print(f"  event bus {_EVENT_BUS!r} already exists")


def main() -> None:
    print(f"Seeding LocalStack at {_ENDPOINT} ...")
    try:
        _seed_secrets()
        _seed_buckets()
        _seed_event_bus()
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller, not swallowed
        print(f"LocalStack seeding failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print("LocalStack seeding complete.")


if __name__ == "__main__":
    main()
