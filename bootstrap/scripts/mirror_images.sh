#!/usr/bin/env bash
# Mirrors Panasa's published images into this account's ECR (F11/F13/A13) —
# the last Panasa-controlled operation. After this, RUNTIME_IMAGE points at
# this account's own ECR and the Panasa public registry is never read again.
#
# Usage:
#   PANASA_REGISTRY=public.ecr.aws/panasa \
#   VERSION=1.0.0 LANGFUSE_VERSION=3.0.0 \
#   ./bootstrap/scripts/mirror_images.sh <customer-account-id> <region>
set -euo pipefail

CUSTOMER_ACCOUNT="${1:?Usage: mirror_images.sh <customer-account-id> <region>}"
REGION="${2:?Usage: mirror_images.sh <customer-account-id> <region>}"
PANASA_REGISTRY="${PANASA_REGISTRY:?Set PANASA_REGISTRY, e.g. public.ecr.aws/panasa}"
VERSION="${VERSION:?Set VERSION, e.g. 1.0.0}"
LANGFUSE_VERSION="${LANGFUSE_VERSION:-3.0.0}"

CUSTOMER_REGISTRY="${CUSTOMER_ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$CUSTOMER_REGISTRY"

IMAGES=(
  "agent-factory-runtime:${VERSION}"
  "agent-factory-console:${VERSION}"
  "langfuse-web:${LANGFUSE_VERSION}"
  "langfuse-worker:${LANGFUSE_VERSION}"
  "code-sandbox:${VERSION}"
)

for IMAGE in "${IMAGES[@]}"; do
  echo "Mirroring ${IMAGE}..."
  docker pull "${PANASA_REGISTRY}/${IMAGE}"
  docker tag "${PANASA_REGISTRY}/${IMAGE}" "${CUSTOMER_REGISTRY}/panasa/${IMAGE}"
  docker push "${CUSTOMER_REGISTRY}/panasa/${IMAGE}"
done

echo "Done. Set runtime_image/console_image in stage1 to the ${CUSTOMER_REGISTRY}/panasa/... tags above (or leave unset to default to :latest)."
