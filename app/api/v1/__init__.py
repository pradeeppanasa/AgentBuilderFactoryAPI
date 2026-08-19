from fastapi import APIRouter

from app.api.v1 import (
    admin_settings,
    agents,
    auth,
    bedrock_credentials,
    connectors,
    deployments,
    guardrail_policies,
    hitl,
    knowledge_bases,
    platform,
    playground,
    projects,
    runs,
    skills,
    task_planner,
)

router = APIRouter(prefix="/api/v1")
router.include_router(platform.router)
router.include_router(auth.router)
router.include_router(agents.router)
router.include_router(deployments.router)
router.include_router(connectors.router)
router.include_router(knowledge_bases.router)
router.include_router(bedrock_credentials.router)
router.include_router(guardrail_policies.router)
router.include_router(playground.router)
router.include_router(runs.router)
router.include_router(projects.router)
router.include_router(skills.router)
router.include_router(hitl.router)
router.include_router(task_planner.router)
router.include_router(admin_settings.router)
