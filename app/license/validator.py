"""License validation (CLAUDE.md F11 / R26).

Enforced only in DEPLOYMENT_MODE=enterprise, where the Runtime must prove it is
running in a Panasa-licensed AWS account before serving requests. Prototype mode
is Panasa-owned infrastructure and is not license-gated.

Validation is entirely local: Secrets Manager (customer-owned) + STS. No network
call to Panasa is ever made. The Panasa private signing key never enters this
codebase or any container — only the public verification key is embedded at
image build time.

Phase 1 note: bootstrap (which mirrors images, embeds the public key, and
provisions the license secret) has not run yet, so validation is skipped with a
warning log when its prerequisites (public key file, license secret ARN) are
not yet present. This keeps local/dev boot working while the real enforcement
path is fully implemented for when bootstrap wires it up.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import jwt
import structlog
from packaging.version import Version

from app.config import Settings

log = structlog.get_logger()

_PUBLIC_KEY_PATH = Path(__file__).parent / "panasa-public.pem"


class LicenseError(Exception):
    """Raised when the runtime is not validly licensed for this environment."""


class LicenseValidator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate(self) -> None:
        """Call at application startup. Raises LicenseError if invalid."""
        if self._settings.deployment_mode == "prototype":
            log.info("license.skipped", reason="prototype_mode")
            return

        if not self._settings.license_token_secret_arn:
            log.warning("license.skipped", reason="license_token_secret_arn_not_configured")
            return

        if not _PUBLIC_KEY_PATH.exists():
            log.warning("license.skipped", reason="public_key_not_embedded")
            return

        token = self._read_token(self._settings.license_token_secret_arn)
        try:
            claims = jwt.decode(
                token,
                _PUBLIC_KEY_PATH.read_text(),
                algorithms=["RS256"],
                options={"verify_exp": True},
            )
        except jwt.ExpiredSignatureError as exc:
            raise LicenseError("License token has expired. Contact Panasa to renew.") from exc
        except jwt.InvalidSignatureError as exc:
            raise LicenseError("License token signature invalid. Token may be tampered.") from exc
        except jwt.DecodeError as exc:
            raise LicenseError(f"License token malformed: {exc}") from exc

        actual_account_id = self._get_account_id()
        if claims["aws_account_id"] != actual_account_id:
            raise LicenseError(
                f"License is bound to account {claims['aws_account_id']}. "
                f"Running in account {actual_account_id}. "
                f"Contact Panasa to license this account."
            )

        if not self._version_in_range(
            self._settings.platform_version, claims["version_min"], claims["version_max"]
        ):
            raise LicenseError(
                f"Runtime version {self._settings.platform_version} is outside licensed "
                f"range {claims['version_min']}–{claims['version_max']}."
            )

        log.info("license.validated")

    def _read_token(self, secret_arn: str) -> str:
        sm = boto3.client("secretsmanager", region_name=self._settings.aws_region)
        response = sm.get_secret_value(SecretId=secret_arn)
        secret_string: str = response["SecretString"]
        return secret_string

    def _get_account_id(self) -> str:
        sts = boto3.client("sts", region_name=self._settings.aws_region)
        account_id: str = sts.get_caller_identity()["Account"]
        return account_id

    @staticmethod
    def _version_in_range(version: str, min_v: str, max_v: str) -> bool:
        return Version(min_v) <= Version(version) <= Version(max_v)
