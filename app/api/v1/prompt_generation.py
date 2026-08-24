"""AI Prompt Generation (CLAUDE.md Section 22).

Two factory-internal LLM calls: generate a system prompt from a plain
description of intent, or improve an existing prompt given instructions.
Both always use the Factory Runtime's own Bedrock access
(call_factory_model / PROMPT_GEN_MODEL_ID) — never the model the agent
being configured is set to use (Section 5.11's rule).

Deliberately NOT nested under /agents/{agent_id}/... despite CLAUDE.md's
literal path spec: the wizard calls this from Step 2, before the agent
being drafted has ever been saved (no agent_id exists yet) — same reason
Task Planner's endpoints (app/api/v1/task_planner.py,
app/api/v1/build_with_ai.py) are tenant-scoped rather than agent-scoped.
Editing an existing agent's prompt reuses the same two endpoints; the
request body already carries every field needed either way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.services.model_router import call_factory_model

router = APIRouter(prefix="/agents", tags=["prompt-generation"])

_ROLES = ("developer", "analyst", "auditor")

_GENERATE_SYSTEM_PROMPT = """You are an expert AI agent prompt engineer for an \
enterprise agent-building platform.

Generate a production-quality system prompt for an AI agent. Output ONLY the \
system prompt text — no explanation, no markdown code fences, no preamble.

Use {{company_name}} and {{agent_name}} as placeholders the user will fill in \
at deployment time. The prompt should:
1. Define the agent's role and boundaries clearly.
2. Specify how to use each listed tool, if any are given.
3. Handle edge cases: unknowns, sensitive requests, escalation.
4. Match the requested tone and target audience.
5. Be between 200 and 400 words.
"""

_IMPROVE_SYSTEM_PROMPT = """You are an expert AI agent prompt engineer for an \
enterprise agent-building platform.

You are given an existing system prompt and instructions for how to improve \
it. Output ONLY the revised system prompt text — no explanation, no markdown \
code fences, no preamble, no diff notation. Preserve any {{variable}} \
placeholders already present unless the instructions say to change them.
"""


class GeneratePromptRequest(BaseModel):
    agent_name: str
    business_purpose: str
    agent_type: str = "standard"
    tools: list[str] = Field(default_factory=list)
    tone: str = "professional"
    target_audience: str = "end_users"


class ImprovePromptRequest(BaseModel):
    current_prompt: str
    improvement_instructions: str


class PromptResponse(BaseModel):
    system_prompt: str


def _build_generate_user_message(payload: GeneratePromptRequest) -> str:
    tools_line = ", ".join(payload.tools) if payload.tools else "none"
    return (
        f"Agent name: {payload.agent_name}\n"
        f"Business purpose: {payload.business_purpose}\n"
        f"Agent type: {payload.agent_type}\n"
        f"Available tools: {tools_line}\n"
        f"Tone: {payload.tone}\n"
        f"Target audience: {payload.target_audience}\n\n"
        "Generate the system prompt now."
    )


def _build_improve_user_message(payload: ImprovePromptRequest) -> str:
    return (
        f"Current system prompt:\n{payload.current_prompt}\n\n"
        f"Improvement instructions:\n{payload.improvement_instructions}\n\n"
        "Generate the revised system prompt now."
    )


@router.post("/generate-prompt", response_model=PromptResponse)
async def generate_prompt(
    payload: GeneratePromptRequest,
    _tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_ROLES))],
) -> PromptResponse:
    try:
        text = await call_factory_model(
            settings.prompt_gen_model_id,
            _GENERATE_SYSTEM_PROMPT,
            _build_generate_user_message(payload),
            settings.prompt_gen_max_tokens,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "prompt_generation_failed", "message": str(exc)},
        ) from exc
    return PromptResponse(system_prompt=text.strip())


@router.post("/improve-prompt", response_model=PromptResponse)
async def improve_prompt(
    payload: ImprovePromptRequest,
    _tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_ROLES))],
) -> PromptResponse:
    try:
        text = await call_factory_model(
            settings.prompt_gen_model_id,
            _IMPROVE_SYSTEM_PROMPT,
            _build_improve_user_message(payload),
            settings.prompt_gen_max_tokens,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "prompt_improvement_failed", "message": str(exc)},
        ) from exc
    return PromptResponse(system_prompt=text.strip())
