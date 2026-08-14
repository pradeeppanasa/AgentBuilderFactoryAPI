from fastapi import APIRouter

from app.api.v1 import agents, auth, connectors, deployments, platform

router = APIRouter(prefix="/api/v1")
router.include_router(platform.router)
router.include_router(auth.router)
router.include_router(agents.router)
router.include_router(deployments.router)
router.include_router(connectors.router)
