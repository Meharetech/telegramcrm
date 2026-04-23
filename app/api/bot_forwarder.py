import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime

from app.models.bot_forwarder import BotForwarder
from app.models.user import User
from app.api.auth_utils import get_current_user
from app.services.bot_forwarder.bot_service import start_bot, stop_bot

router = APIRouter()
logger = logging.getLogger(__name__)

class BotPayload(BaseModel):
    name: str
    bot_token: str
    admin_usernames: List[str] = []
    target_chat_ids: List[str] = []
    forward_mode: Optional[str] = "forward"
    proxy_id: Optional[str] = None
    keyword_filters: Optional[List[str]] = []
    is_enabled: Optional[bool] = True

def _bot_to_dict(bot: BotForwarder):
    d = bot.model_dump()
    d["id"] = str(bot.id)
    # Mask bot token for security in list views? No, user needs to see it?
    # Usually we don't return the token in full, but since it's their own, it's fine.
    return d

@router.get("/")
async def get_bots(current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_bot_hub")
    
    bots = await BotForwarder.find(BotForwarder.user_id == str(current_user.id)).to_list()
    return [_bot_to_dict(b) for b in bots]

@router.post("/")
async def create_bot(payload: BotPayload, current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_bot_hub")
    
    bot_count = await BotForwarder.find(BotForwarder.user_id == str(current_user.id)).count()
    await check_plan_limit(current_user, "max_bots", bot_count)

    bot = BotForwarder(
        user_id=str(current_user.id),
        **payload.model_dump()
    )
    await bot.insert()
    return _bot_to_dict(bot)

@router.put("/{bot_id}")
async def update_bot(bot_id: str, payload: BotPayload, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(bot_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    
    bot = await BotForwarder.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")

    # Update fields
    for k, v in payload.model_dump().items():
        setattr(bot, k, v)
    bot.updated_at = datetime.utcnow()
    await bot.save()

    await bot.save()
    return _bot_to_dict(bot)

@router.delete("/{bot_id}")
async def delete_bot(bot_id: str, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(bot_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    
    bot = await BotForwarder.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")

    await stop_bot(bot_id)
    await bot.delete()
    return {"status": "success"}

@router.post("/{bot_id}/send")
async def send_msg(bot_id: str, payload: dict, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(bot_id): raise HTTPException(400, "Invalid ID")
    from app.services.bot_forwarder.bot_service import send_direct_message
    
    text = payload.get("text")
    if not text: raise HTTPException(400, "Text required")
    
    try:
        await send_direct_message(bot_id, text)
        return {"status": "sent"}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/{bot_id}/toggle")
async def toggle_bot(bot_id: str, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(bot_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    
    bot = await BotForwarder.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")

    bot.is_enabled = not bot.is_enabled
    await bot.save()

    # RECURSIVE REACTION: Start/Stop the client if Terminal is active
    if bot.is_enabled:
        if current_user.services_active:
            try:
                await start_bot(bot)
                logger.info(f"[api] Reactive start triggered for bot {bot.name}")
            except Exception as e:
                logger.error(f"[api] Failed reactive start for {bot.name}: {e}")
    else:
        # Stop immediately if disabled
        await stop_bot(bot_id)
        logger.info(f"[api] Reactive stop triggered for bot {bot_id}")

    return {"status": "updated", "is_enabled": bot.is_enabled}
