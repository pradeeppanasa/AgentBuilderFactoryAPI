from fastapi import APIRouter

from app.api.v1 import (
    admin_settings,
    agents,
    audit_log,
    auth,
    bedrock_credentials,
    build_with_ai,
    connectors,
    deployments,
    guardrail_policies,
    hitl,
    knowledge_bases,
    platform,
    playground,
    projects,
    prompt_generation,
    prompts,
    runs,
    skills,
    task_planner,
)

router = APIRouter(prefix="/api/v1")
router.include_router(platform.router)
router.include_router(auth.router)
router.include_router(agents.router)
router.include_router(build_with_ai.router)
router.include_router(prompt_generation.router)
router.include_router(deployments.router)
router.include_router(connectors.router)
router.include_router(knowledge_bases.router)
router.include_router(bedrock_credentials.router)
router.include_router(guardrail_policies.router)
router.include_router(playground.router)
router.include_router(runs.router)
router.include_router(projects.router)
router.include_router(skills.router)
router.include_router(prompts.router)
router.include_router(hitl.router)
router.include_router(task_planner.router)
router.include_router(admin_settings.router)
router.include_router(audit_log.router)
