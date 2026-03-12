from fastapi import APIRouter
from .auth import router as auth_router
from .chats import router as chats_router
from .messages import router as messages_router
from .profile import router as profile_router
from .scrape import router as scrape_router
from .creative import router as creative_router

router = APIRouter()

# Combine all sub-routers into one main accounts router
router.include_router(auth_router)
router.include_router(chats_router)
router.include_router(messages_router)
router.include_router(profile_router)
router.include_router(scrape_router)
router.include_router(creative_router)
