# Local Development

## Quick start

```bash
git clone <repo>
cd panasa-agent-builder-runtime
make dev
```

`make dev` runs [`scripts/local-setup.sh`](../scripts/local-setup.sh), which bootstraps the entire
stack from nothing: Python venv, `.env`, Docker services, LocalStack seeding, database migrations,
and a default admin user. On a machine with a warm Docker layer cache it finishes in under two
minutes; a genuinely cold clone (no cached base images, no `.venv`) will take longer, dominated by
image pulls and `pip install`, not by anything this script does.

If you don't have `make` installed, run the script directly:

```bash
bash scripts/local-setup.sh
```

Other targets: `make down` (stop containers), `make logs` (follow the runtime's logs), `make clean`
(stop containers, wipe the Postgres volume, and force a full re-bootstrap next time).

### What it does, step by step

| Step | What | Why |
|---|---|---|
| 0 | Create `.venv` (if missing), `pip install -r requirements.txt` | Alembic and the admin-bootstrap script run on the host, not in the container |
| 1 | `cp .env.example .env`, patch in local values | See [".env" below](#env) — skipped if `.env` already exists, so it never clobbers your own edits |
| 2 | `docker-compose up -d dynamodb-local localstack postgres redis` | Dependencies first, deliberately *not* the runtime — it needs LocalStack seeded before it can start (see [bug 2 below](#bug-3-localstack-latest-now-requires-a-paid-account)) |
| 3 | Poll each service until healthy | Redis/Postgres via their own health probes; LocalStack via `/_localstack/health`; DynamoDB Local (no health endpoint) via a real `list_tables()` call |
| 4 | `scripts/seed_localstack.py` | Creates the `jwt-secret` / `git-token` secrets and the `panasa-iac-artifacts-local` / `panasa-audit-local` buckets the runtime reads at startup |
| 5 | `docker-compose up -d --build agent-builder-runtime`, wait healthy, `alembic upgrade head` | Runtime needs the secrets from step 4 to exist *before* it starts — its lifespan fetches them once, with no retry |
| 6 | `scripts/create_admin.py` | Creates `DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD` (from `.env`) if that user doesn't already exist |
| 7 | `curl /api/v1/platform/health` | Printed so you can see the stack is actually up, not just that containers started |

### .env

`local-setup.sh` copies `.env.example` → `.env` and then patches a handful of keys so a fresh clone
works against the local Docker stack out of the box. `.env.example` itself stays a
real-deployment-shaped template (Section 3 of the master spec) — these overrides only ever land in
your local `.env`, never in the example file:

| Key | `.env.example` (real deployment) | Local `.env` (this script) | Why |
|---|---|---|---|
| `JWT_SECRET_ARN` | `arn:aws:secretsmanager:...:secret:jwt-secret` | `jwt-secret` | Secrets Manager's `GetSecretValue` accepts a plain name — sidesteps LocalStack assigning its own ARN suffix that would never match a hardcoded placeholder ARN |
| `GIT_CREDENTIALS_SECRET` | `arn:aws:secretsmanager:...:secret:git-token` | `git-token` | same reason |
| `DATABASE_URL` | `...@localhost:5432/...` | `...@localhost:5433/...` | see [bug 3](#port-5433-not-5432) below |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | *(not present)* | `local` / `local` | boto3 refuses to sign **any** request, even to a local emulator, with no credentials at all — there's no IAM role to fall back to outside real AWS. **Never add these to a real deployment's `.env`** — a real deployment must rely on its task/instance IAM role; static credentials would silently take priority over it. |
| `IAC_OUTPUT_BUCKET` / `AUDIT_S3_BUCKET` | `panasa-iac-artifacts-<account>` / `panasa-audit-<account>` | `...-local` | matches what step 4 actually creates in LocalStack |

`DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD` / `DEFAULT_ADMIN_TENANT_ID` are new keys, read only
by `scripts/create_admin.py` — the running app never reads them at any point after bootstrap.

---

## Three Docker bugs found getting this stack running

None of these were visible from the test suite — `TestClient`-based tests never spin up a real
`uvicorn`/`uvloop` process or a real container, so all three only surfaced once the stack was
actually run with `docker-compose up`.

### Bug 1 — `uvicorn` not on `$PATH`

**Symptom:** `OCI runtime create failed: ... exec: "uvicorn": executable file not found in $PATH`,
immediately on container start, before any application code runs.

**Cause:** `uv sync` always creates a project `.venv`, regardless of `UV_SYSTEM_PYTHON=1` — that
setting only controls which Python interpreter the venv is built on top of (skip downloading a
managed one, use the image's system Python instead), not whether a venv exists at all. Console
scripts like `uvicorn` land in `/app/.venv/bin/`, which was never added to `PATH`.

**Fix** ([`Dockerfile`](../Dockerfile)):

```dockerfile
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
```

### Bug 2 — uvloop crashes on startup with `aws-xray-sdk`

**Symptom:** container starts, then crashes immediately with:

```
TypeError: task_factory() got an unexpected keyword argument 'context'
```

...raised from inside `uvicorn/lifespan/on.py`'s own `loop.create_task(self.main())` call, before
any app code runs.

**Cause:** `app/main.py` configures `aws_xray_sdk`'s `AsyncContext()` for request tracing, which
installs a 2-argument `task_factory(loop, coro)` on the event loop. Standard `asyncio`'s
`BaseEventLoop.create_task()` only forwards its own `context=` keyword to a custom task factory
when `context` is not `None`; `uvicorn`'s default loop implementation, **uvloop**, forwards it
unconditionally in its Cython implementation. Since `uvicorn`'s own internal lifespan call never
passes an explicit context, `context` is `None` — asyncio's stdlib path would silently omit the
kwarg and work fine, but uvloop's doesn't, and the aws-xray-sdk task factory was never written to
accept it.

**Fix** ([`Dockerfile`](../Dockerfile)): run uvicorn with the stdlib asyncio loop instead of the
default uvloop:

```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio"]
```

### Bug 3 — `localstack:latest` now requires a paid account

**Symptom:** the `localstack` container exits immediately (code 55) with:

```
License activation failed! ... LocalStack pro features can only be used with a valid license.
```

...even though `docker-compose.yml` only requests community-tier services (`s3`, `secretsmanager`,
`sts`, `events`, `stepfunctions`).

**Cause:** the `:latest` tag now resolves to a build that enforces a license check at startup
regardless of which services are actually used.

**Fix** ([`docker-compose.yml`](../docker-compose.yml)): pin to a known-community-edition tag —

```yaml
localstack:
  image: localstack/localstack:3.8   # not :latest — see docs/local-dev.md
```

Confirmed via `curl http://localhost:4566/_localstack/health` → `"edition": "community"`.

### Port 5433, not 5432

Not a bug in this codebase, but worth documenting alongside the above since it affects the same
`docker-compose.yml`: this development machine has a **native Windows PostgreSQL service** already
bound to port 5432, which silently intercepts host connections meant for the `postgres` container
(container-to-container traffic on the Docker network is unaffected — only `localhost:5432` from
the host is hijacked). The Postgres service's host port mapping was moved to `5433:5432`
accordingly; `DATABASE_URL` values that connect from the *host* (this script, Alembic, an IDE's DB
client) must use `localhost:5433`. Values used *inside* the Docker network (the running container's
own `DATABASE_URL`) are unaffected and still say `postgres:5432`.

---

## Terraform CLI — pinned to 1.9.8 everywhere (R43)

`POST /agents/{id}/generate-iac`'s validation report includes `terraform_fmt` and
`terraform_validate` checks (`app/modules/iac_generator/validator.py`). Both need the real
Terraform CLI to run for real rather than reporting `passed: true` with a `"Skipped — terraform CLI
not installed"` detail.

This does **not** contradict F0/R03 ("Panasa Runtime never touches customer AWS/state") — the
validator only ever runs `terraform fmt` and `terraform validate -backend=false`, both purely local
syntax/schema checks with no AWS credentials or customer state involved. `terraform plan`/`apply`
against real infrastructure remain exclusively the customer's own CI/CD's job (see the buildspecs
below).

Per R43, the CLI is pinned to the exact same version (currently **1.9.8**) in every place it's
installed — never `:latest`, never whatever version a base image happens to ship:

| Where | How |
|---|---|
| `Dockerfile` (baked into the runtime image) | `ARG TERRAFORM_VERSION=1.9.8`, downloaded + unzipped into `/usr/local/bin` |
| `codebuild/terraform-validate-buildspec.yml`, `terraform-plan-buildspec.yml`, `terraform-apply-buildspec.yml` | same pinned download in each buildspec's `install` phase, replacing whatever the CodeBuild build image happened to ship |

Verify inside the running container:

```bash
docker exec agent-builder-runtime terraform version
# Terraform v1.9.8
```

And confirm the checks actually run (not skipped) via a real `generate-iac` call — `checks[].detail`
for `terraform_fmt`/`terraform_validate` should read `"All files correctly formatted"` /
`"Configuration is syntactically valid"`, not `"Skipped"`.

### R40 — a failed validation report is HTTP 422, not 200

If any of the 8 checks fails, `POST /agents/{id}/generate-iac` returns **422** with the full
`IaCValidationReport` as the error `detail` — never a 200 wrapping a partially-validated bundle. The
report (pass or fail) is always persisted to the version record either way, so a failure is still
inspectable later via `GET /agents/{id}/versions/{version}`; it just isn't handed back as a usable
success response. See `tests/test_generate_iac_api.py::test_generate_iac_422_when_validation_fails`.

---

## A fourth bug: a real `.env` silently breaks the test suite

Not a Docker bug, but adjacent to bug 3 above and worth knowing before you run `pytest` on a machine
where `make dev` has also been run: **a real `.env` file in the repo root used to make the test
suite fail with `401 Unauthorized`** on every authenticated endpoint.

**Cause:** `app.config.settings` reads `.env` directly via `pydantic-settings`, independent of
whatever `tests/conftest.py` sets in `os.environ`. Before this fix, conftest overrode `DATABASE_URL`
and the secret names but never touched `DYNAMODB_ENDPOINT`/`SECRETS_MANAGER_ENDPOINT`/`S3_ENDPOINT`
— so with a real `.env` present (from `make dev`), the app under test silently pointed boto3 at the
*real* running LocalStack/DynamoDB Local instead of moto's mocks. The real LocalStack's seeded
`jwt-secret` has a different value than `conftest.TEST_JWT_SECRET`, so every JWT signature check
failed.

**Fix** (`tests/conftest.py`): force those three endpoint overrides to `""` (falsy, same as unset)
unconditionally, the same way `DATABASE_URL` was already forced:

```python
os.environ["DYNAMODB_ENDPOINT"] = ""
os.environ["SECRETS_MANAGER_ENDPOINT"] = ""
os.environ["S3_ENDPOINT"] = ""
```

The test suite is now hermetic regardless of whether `make dev` has ever been run in this checkout.
