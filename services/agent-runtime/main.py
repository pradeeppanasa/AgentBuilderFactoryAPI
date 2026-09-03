"""Panasa Generic Agent Runtime — FastAPI entrypoint.

One Docker image, reused by every deployed agent (Generic Agent Runtime
instruction, 2026-09-03). AGENT_ID/TENANT_ID (compute.tf's only
agent-specific env vars) select which agent this specific container IS;
config_loader.py loads everything else from DynamoDB at startup. Never
generate new application code per agent — this file is identical for all
of them.

Auth: AGENT_API_KEY, if set, is compared against the request's `Authorization:
Bearer <key>` header on /chat — a deliberately minimal placeholder (no
Secrets Manager rotation, no per-caller keys). Real API-key management for
generated agents' public endpoints isn't specified anywhere in CLAUDE.md
yet; this keeps /chat usable (and, if AGENT_API_KEY is unset, open — fine
behind an internal ALB reachable only via this agent's own API Gateway
route during local/dev testing) without inventing a larger auth system
this instruction never asked for.
"""

from __future__ import annotations

import hmac
import os

import structlog
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from config_loader import load_agent_config
from orchestrator import AgentOrchestrator

logger = structlog.get_logger()

app = FastAPI(title="Panasa Agent Runtime")

agent_config = load_agent_config()
orchestrator = AgentOrchestrator(agent_config)

logger.info(
    "agent_runtime_started",
    agent_id=agent_config["agent_id"],
    agent_name=agent_config.get("name"),
    model=agent_config.get("model_id"),
    version=agent_config.get("version"),
)


class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    run_id: str
    hitl_pending: bool = False


def _check_auth(authorization: str | None) -> None:
    api_key = os.environ.get("AGENT_API_KEY")
    if not api_key:
        return
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    _check_auth(authorization)
    try:
        result = await orchestrator.run(
            message=request.message, session_id=request.session_id, user_id=request.user_id
        )
        return ChatResponse(**result)
    except Exception as exc:
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
