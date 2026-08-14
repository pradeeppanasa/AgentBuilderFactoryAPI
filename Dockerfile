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
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Installs into the image's system Python rather than creating a second
# .venv inside the container — there's nothing else competing for that
# interpreter in here, so the usual venv-isolation reason for `uv sync`
# doesn't apply.
ENV UV_SYSTEM_PYTHON=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

RUN chown -R panasa:panasa /app
USER panasa

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/platform/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
