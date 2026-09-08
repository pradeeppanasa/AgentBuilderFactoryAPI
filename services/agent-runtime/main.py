"""Panasa Generic Agent Runtime — FastAPI entrypoint.

One Docker image, reused by every deployed agent (Generic Agent Runtime
instruction, 2026-09-03). AGENT_ID/TENANT_ID (compute.tf's only
agent-specific env vars) select which agent this specific container IS;
config_loader.py loads everything else from DynamoDB at startup. Never
generate new application code per agent — this file is identical for all
of them.

Auth (Sprint 3 Phase 1, CLAUDE.md Section 64.1, R67): auth_middleware below
replaces the previous AGENT_API_KEY/`_check_auth` placeholder — a single
plaintext value from an environment variable, no rotation, no revocation,
no per-agent secret. Real credentials now live only in Secrets Manager,
validated per-request via auth.auth_chain against the agent's own
panasa-agents record (re-fetched fresh every request, never the
startup-cached agent_config below — see config_loader.get_current_agent_
record's docstring for why).
"""

from __future__ import annotations

import structlog
from audit import write_audit_event
from auth import auth_chain
from config_loader import get_current_agent_record, load_agent_config
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from orchestrator import AgentOrchestrator
from pydantic import BaseModel
from quota import check_monthly_budget, check_rate_limit, get_redis_client
from tool_policy import ApprovalRequiredError, ToolDeniedError

logger = structlog.get_logger()

app = FastAPI(title="Panasa Agent Runtime")

agent_config = load_agent_config()
orchestrator = AgentOrchestrator(agent_config)
redis_client = get_redis_client()

logger.info(
    "agent_runtime_started",
    agent_id=agent_config["agent_id"],
    agent_name=agent_config.get("name"),
    model=agent_config.get("model_id"),
    version=agent_config.get("version"),
)

# Routes exempt from auth_middleware. /health carries no Authorization
# header (ECS task health check; the API Gateway GET /health route) and
# gating it would break the deployment pipeline's own HEALTH_CHECK stage.
# /config is deliberately public-safe by design (see the config() handler
# below — non-sensitive fields only, no prompt text, no credentials) and
# was never behind the old AGENT_API_KEY check either; Phase 1 is scoped to
# /chat's auth + tenant isolation, not to locking down /config.
_AUTH_EXEMPT_PATHS = {"/health", "/config"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """R67 tenant isolation + credential check (CLAUDE.md Section 64.1).

    Deviates from the CLAUDE.md skeleton in one respect: that version
    raises HTTPException directly inside the middleware body. Starlette's
    HTTPException -> JSON conversion happens in ExceptionMiddleware, which
    wraps INSIDE user `@app.middleware("http")` functions, not outside — an
    HTTPException raised here would propagate as an unhandled 500, not the
    intended 401/403. Returning a JSONResponse directly avoids that.
    """
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    authorization = request.headers.get("authorization", "")
    source_ip = request.client.host if request.client else ""

    try:
        agent_record = get_current_agent_record()
    except Exception as exc:
        logger.error("auth_middleware_agent_lookup_failed", error=str(exc))
        return JSONResponse(status_code=500, content={"detail": "Agent configuration unavailable"})

    ctx = await auth_chain.authenticate(authorization, agent_record)
    if ctx is None:
        write_audit_event(
            tenant_id=agent_record.get("tenant_id", "unknown"),
            event_type="auth.failed",
            agent_id=agent_record.get("agent_id", "unknown"),
            principal_id="unknown",
            action=f"{request.method} {request.url.path}",
            resource=agent_record.get("agent_id", "unknown"),
            result="denied",
            source_ip=source_ip,
        )
        return JSONResponse(status_code=401, content={"detail": "Authentication failed"})

    # The DB record just fetched is the authority, never the container's
    # own TENANT_ID env var (R67) — deriving both sides of this comparison
    # from the same fetch makes it a no-op today for ApiKeyAuthProvider
    # (its AuthContext.tenant_id IS agent_record["tenant_id"]), but it
    # becomes load-bearing the moment JwtAuthProvider (S-12) starts
    # returning a tenant claim from an externally-issued token that may
    # not match this specific agent's own tenant.
    if agent_record.get("tenant_id") != ctx.tenant_id:
        write_audit_event(
            tenant_id=ctx.tenant_id,
            event_type="auth.tenant_mismatch",
            agent_id=agent_record.get("agent_id", "unknown"),
            principal_id=ctx.principal_id,
            action=f"{request.method} {request.url.path}",
            resource=agent_record.get("agent_id", "unknown"),
            result="denied",
            source_ip=source_ip,
        )
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    # Sprint 3 Phase 9 (S-03, R70, Section 67) — /chat only, deliberately:
    # this middleware runs on every non-exempt route, and rpm/rpd/budget
    # are specifically about business-request cost/abuse, not e.g. a
    # future non-/chat route sharing the same limits unintentionally.
    if request.url.path == "/chat":
        rpm_limit = agent_config.get("rate_limit_rpm")
        rpd_limit = agent_config.get("rate_limit_rpd")
        if rpm_limit is not None or rpd_limit is not None:
            allowed, reason = await check_rate_limit(
                tenant_id=ctx.tenant_id,
                agent_id=agent_record.get("agent_id", "unknown"),
                rpm_limit=rpm_limit,
                rpd_limit=rpd_limit,
                r=redis_client,
            )
            if not allowed:
                write_audit_event(
                    tenant_id=ctx.tenant_id,
                    event_type="rate_limit.exceeded",
                    agent_id=agent_record.get("agent_id", "unknown"),
                    principal_id=ctx.principal_id,
                    action=f"{request.method} {request.url.path}",
                    resource=agent_record.get("agent_id", "unknown"),
                    result="denied",
                    source_ip=source_ip,
                    extra={"reason": reason},
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded ({reason})"},
                )

        monthly_budget_usd = agent_config.get("monthly_budget_usd")
        if monthly_budget_usd is not None and not check_monthly_budget(
            agent_record, monthly_budget_usd
        ):
            write_audit_event(
                tenant_id=ctx.tenant_id,
                event_type="budget.exceeded",
                agent_id=agent_record.get("agent_id", "unknown"),
                principal_id=ctx.principal_id,
                action=f"{request.method} {request.url.path}",
                resource=agent_record.get("agent_id", "unknown"),
                result="denied",
                source_ip=source_ip,
            )
            return JSONResponse(
                status_code=402,
                content={"detail": "Monthly cost budget exceeded for this agent"},
            )

    request.state.auth = ctx
    return await call_next(request)


class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    run_id: str
    hitl_pending: bool = False


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request) -> ChatResponse | JSONResponse:
    # request.state.auth is set by auth_middleware above (this route is
    # never exempt) — principal_id for the agent.invoked event below.
    auth_ctx = http_request.state.auth
    try:
        result = await orchestrator.run(
            message=request.message, session_id=request.session_id, user_id=request.user_id
        )
        # Sprint 4 Phase 2 (S-05, CLAUDE.md Section 61.2) — latency_ms/
        # input_tokens/output_tokens come from orchestrator.run()'s own
        # return dict (Sprint 4 Phase 2 also added those); popped off
        # before constructing ChatResponse so they're never echoed back
        # to the caller (that response model only has the original 4
        # fields).
        write_audit_event(
            tenant_id=auth_ctx.tenant_id,
            event_type="agent.invoked",
            agent_id=agent_config["agent_id"],
            principal_id=auth_ctx.principal_id,
            action="POST /chat",
            resource=agent_config["agent_id"],
            result="success",
            extra={
                "latency_ms": result.get("latency_ms"),
                "input_tokens": result.get("input_tokens"),
                "output_tokens": result.get("output_tokens"),
            },
        )
        return ChatResponse(
            response=result["response"],
            session_id=result["session_id"],
            run_id=result["run_id"],
            hitl_pending=result["hitl_pending"],
        )
    except ToolDeniedError as exc:
        # Sprint 3 Phase 4 (R64) — "403 with a default deny message"
        raise HTTPException(status_code=403, detail=f"Tool denied: {exc.reason}") from exc
    except ApprovalRequiredError:
        # Sprint 3 Phase 4 stub — the full async approval flow (a human
        # actually approving/rejecting, and the tool then running) is
        # S-11 (P1), not built here.
        return JSONResponse(
            status_code=202,
            content={
                "status": "awaiting_approval",
                "message": "This action requires human approval. Check your Panasa Console.",
            },
        )
    except Exception as exc:
        write_audit_event(
            tenant_id=auth_ctx.tenant_id,
            event_type="agent.invoked",
            agent_id=agent_config["agent_id"],
            principal_id=auth_ctx.principal_id,
            action="POST /chat",
            resource=agent_config["agent_id"],
            result="error",
            extra={"error_type": type(exc).__name__},
        )
        logger.error("chat_error", error=str(exc))
        raise HTTPException(status_code=500, detail="Agent execution failed") from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "healthy",
        "agent_id": agent_config["agent_id"],
        "agent_name": agent_config.get("name", agent_config["agent_id"]),
    }


@app.get("/config")
async def config() -> dict[str, object]:
    """Non-sensitive config only — no prompt text, no credentials."""
    memory_config = agent_config.get("memory") or {}
    human_review = agent_config.get("human_review") or {}
    return {
        "agent_id": agent_config["agent_id"],
        "name": agent_config.get("name", agent_config["agent_id"]),
        "version": agent_config.get("version"),
        "model_id": agent_config.get("model_id"),
        "memory_type": memory_config.get("memory_type", "none"),
        "hitl_enabled": bool(human_review.get("enabled", False)),
        "kb_attached": bool((agent_config.get("knowledge_base") or {}).get("enabled")),
        "tools_count": len(agent_config.get("tools") or []),
    }
