from fastapi import APIRouter

from app.api.v1 import (
    agents,
    auth,
    connectors,
    deployments,
    guardrail_policies,
    knowledge_bases,
    platform,
    playground,
)

router = APIRouter(prefix="/api/v1")
router.include_router(platform.router)
router.include_router(auth.router)
router.include_router(agents.router)
router.include_router(deployments.router)
router.include_router(connectors.router)
router.include_router(knowledge_bases.router)
router.include_router(guardrail_policies.router)
router.include_router(playground.router)
