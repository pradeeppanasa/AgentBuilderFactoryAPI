#!/usr/bin/env bash
# Builds the one shared deployment package for all 9 Step Functions Lambda
# handlers (lambda_handlers/*.py + app/) — "fat Lambda" packaging: one zip,
# 9 aws_lambda_function resources differing only by their `handler` value
# (bootstrap/stage1/lambda.tf). Run before `terraform apply` in stage1 (and
# again any time app/ or lambda_handlers/ change); Terraform's archive_file
# data source hashes the resulting zip and updates the functions on the
# next apply.
#
# Not run *by* Terraform (a local-exec provisioner shelling out to pip
# would make every plan/apply dependent on network access and this
# project's Python toolchain being present on whatever machine runs
# Terraform — CI runners usually aren't set up for that). Kept as an
# explicit, separate step instead, same spirit as F13/A13's
# mirror_images.sh.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

BUILD_DIR="build/lambda_package"
ZIP_PATH="build/lambda_package.zip"

rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"

pip install --quiet --target "$BUILD_DIR" -r requirements.txt
cp -r app "$BUILD_DIR/"
cp -r lambda_handlers "$BUILD_DIR/"

# Dev/test-only deps bloat the package and aren't needed at runtime.
rm -rf "$BUILD_DIR"/{pytest,moto,black,ruff,mypy,fakeredis}* 2>/dev/null || true

(cd "$BUILD_DIR" && zip -r -q "../../$ZIP_PATH" .)

echo "Wrote $ZIP_PATH ($(du -h "$ZIP_PATH" | cut -f1))"
