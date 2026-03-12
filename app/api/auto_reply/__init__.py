from fastapi import APIRouter
from .settings import router as settings_router
from .rules import router as rules_router
from .media import router as media_router
from .worker import router as worker_router, _activate_worker

router = APIRouter()

router.include_router(settings_router)
router.include_router(rules_router)
router.include_router(media_router)
router.include_router(worker_router)
