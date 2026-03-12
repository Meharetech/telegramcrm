from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from app.models.auto_reply import AutoReplySettings
from app.models import TelegramAccount, User
from app.api.auth_utils import get_current_user
from bson import ObjectId
from .schemas import SettingsPayload
from .worker import _activate_worker
from app.services.auto_reply.cache import invalidate_settings_cache

router = APIRouter()

@router.get("/settings/{account_id}")
async def get_settings(account_id: str, current_user: User = Depends(get_current_user)):
    s = await AutoReplySettings.find_one(
        AutoReplySettings.account_id == account_id,
        AutoReplySettings.user_id == str(current_user.id)
    )
    if not s:
        # Check if account exists and belongs to user
        acc = await TelegramAccount.find_one(
            TelegramAccount.id == ObjectId(account_id),
            TelegramAccount.user_id == str(current_user.id)
        )
        if not acc:
                raise HTTPException(status_code=403, detail="Unauthorized account access")
        # Return defaults
        return SettingsPayload().model_dump()
    return s.model_dump(exclude={"id", "revision_id"})

@router.put("/settings/{account_id}")
async def upsert_settings(account_id: str, payload: SettingsPayload, current_user: User = Depends(get_current_user)):
    # Verify account ownership
    acc = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not acc:
        raise HTTPException(status_code=403, detail="Unauthorized account access")

    s = await AutoReplySettings.find_one(
        AutoReplySettings.account_id == account_id,
        AutoReplySettings.user_id == str(current_user.id)
    )
    if s:
        for k, v in payload.model_dump().items():
            setattr(s, k, v)
        s.updated_at = datetime.utcnow()
        await s.save()
    else:
        s = AutoReplySettings(
            account_id=account_id, 
            user_id=str(current_user.id),
            **payload.model_dump()
        )
        await s.insert()

    # ── Invalidate in-memory cache so the engine picks up the new settings immediately ──
    invalidate_settings_cache(account_id)

    # If enabling, attach the handler to the cached client
    if payload.is_enabled:
        await _activate_worker(account_id)

    return {"status": "ok"}
