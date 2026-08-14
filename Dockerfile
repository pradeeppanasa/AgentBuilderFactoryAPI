FROM python:3.12-slim

# Section 32.3 / R31: pinned to a specific release, never :latest — a
# floating tag would make this build non-reproducible (the same Dockerfile
# could silently pull a different uv version, and therefore resolve
# dependencies differently, on different days). Check
# https://github.com/astral-sh/uv/releases for newer stable tags; bump
# deliberately, not automatically.
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

RUN groupadd --system panasa && useradd --system --gid panasa --create-home panasa

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip \
    && rm -rf /var/lib/apt/lists/*

# R43: Terraform CLI pinned to a specific version everywhere it's installed
# (this image, scripts/local-setup.sh, codebuild/terraform-*-buildspec.yml)
# — never :latest, and the version must match across all environments.
# This does NOT contradict F0/R03 ("Panasa Runtime never touches customer
# AWS/state") — the IaC validation suite (app/modules/iac_generator/
# validator.py) only ever runs `terraform fmt` and `terraform validate
# -backend=false`, both purely local syntax/schema checks with no AWS
# credentials or customer state involved. `terraform plan`/`apply` against
# real infrastructure remain exclusively the customer's own CI/CD's job.
ARG TERRAFORM_VERSION=1.9.8
RUN curl -fsSL -o /tmp/terraform.zip \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    && unzip -q /tmp/terraform.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/terraform \
    && rm /tmp/terraform.zip \
    && terraform version

# `uv sync` always creates a project .venv regardless of UV_SYSTEM_PYTHON —
# that flag only means "build it on top of the system interpreter instead of
# downloading a separate managed Python", not "skip the venv". Console
# scripts (uvicorn, etc.) therefore land in /app/.venv/bin, which needs to be
# on PATH explicitly — verified by exec'ing into the built image and finding
# uvicorn there, not in /usr/local/bin, when this line was missing.
ENV UV_SYSTEM_PYTHON=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY app ./app

RUN chown -R panasa:panasa /app
USER panasa

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/platform/health || exit 1

# --loop asyncio, not uvicorn's default uvloop: aws-xray-sdk's AsyncContext
# (app/main.py) installs a 2-argument task_factory(loop, coro). Standard
# asyncio.BaseEventLoop.create_task() only forwards its context= kwarg to a
# custom task_factory when context is not None; uvloop's Cython
# implementation forwards it unconditionally, so uvicorn's own
# `loop.create_task(self.main())` call (context defaults to None) crashes
# uvloop with "task_factory() got an unexpected keyword argument 'context'"
# before the app even starts — reproduced by running this image directly
# (docker-compose up), invisible under TestClient's in-process ASGI
# transport since that path never touches uvicorn/uvloop at all.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio"]
