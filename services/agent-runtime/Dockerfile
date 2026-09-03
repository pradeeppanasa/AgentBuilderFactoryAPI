# Generic Agent Runtime — one image, reused by every deployed agent
# (compute.tf.j2 pulls this exact image for every agent's ECS task).
#
# Plain pip + requirements.txt rather than the Factory Runtime's own
# uv/pyproject.toml build (Section 32.3/R31) — a deliberate, pragmatic
# choice for this first working version of a genuinely separate deployable
# (F8), not a statement that uv wouldn't also be a reasonable choice here.
FROM python:3.12-slim

RUN groupadd --system panasa && useradd --system --gid panasa --create-home panasa

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R panasa:panasa /app
USER panasa

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
