import asyncio
import random
from typing import List, Optional
from fastapi import APIRouter, Depends, Body, HTTPException
from app.api.auth_utils import get_current_user, check_plan_limit
from app.services.voice_chat import join_live_stream, leave_live_stream
from app.models.voice_chat import VoiceChatHistory
from app.models.account import TelegramAccount
from bson import ObjectId

router = APIRouter()

@router.get("/history")
async def get_voice_chat_history(user=Depends(get_current_user)):
    """Fetches the history of voice chat operations for the user."""
    return await VoiceChatHistory.find(VoiceChatHistory.user_id == str(user.id)).sort("-created_at").to_list()

@router.delete("/history/{history_id}")
async def delete_voice_chat_history(history_id: str, user=Depends(get_current_user)):
    """Deletes a specific history record."""
    record = await VoiceChatHistory.find_one(
        VoiceChatHistory.id == ObjectId(history_id),
        VoiceChatHistory.user_id == str(user.id)
    )
    if not record:
        raise HTTPException(status_code=404, detail="History record not found")
    await record.delete()
    return {"status": "success"}

async def save_to_history(user_id: str, group_link: str, task_type: str, raw_results: List[dict]):
    """Helper to save results to history."""
    # Fetch account phones for better history display
    account_ids = [r.get("account_id") for r in raw_results if r.get("account_id")]
    if not account_ids: return
    
    accounts = await TelegramAccount.find({"_id": {"$in": [ObjectId(aid) for aid in account_ids]}}).to_list()
    phone_map = {str(a.id): a.phone_number for a in accounts}

    history_results = []
    success_count = 0
    for res in raw_results:
        aid = res.get("account_id")
        is_success = res.get("status") == "success"
        if is_success: success_count += 1
        
        history_results.append({
            "account_id": aid,
            "phone": phone_map.get(aid, "Unknown"),
            "status": res.get("status"),
            "message": res.get("message") or ("Joined" if is_success else "Failed")
        })

    history = VoiceChatHistory(
        user_id=user_id,
        group_link=group_link,
        task_type=task_type,
        total_accounts=len(raw_results),
        success_count=success_count,
        failed_count=len(raw_results) - success_count,
        results=history_results
    )
    await history.insert()

async def staggered_join(aid: str, group_link: str, skip_join: bool, index: int, duration_minutes: int = 0):
    """Joins a single account with an ultra-safe staggered delay."""
    # Ultra-Safe Delay: 6.5s to 10.0s per account
    wait_time = (index * 6.5) + random.uniform(1.0, 4.0)
    await asyncio.sleep(wait_time) 
    return await join_live_stream(aid, group_link, skip_join=skip_join, duration_minutes=duration_minutes)

async def staggered_leave(aid: str, group_link: str, index: int):
    """Leaves a single account with a safer staggered delay."""
    await asyncio.sleep(index * 1.5)
    return await leave_live_stream(aid, group_link)

@router.post("/join")
async def api_join_voice_chat(
    group_link: str = Body(...),
    account_ids: List[str] = Body(...),
    skip_join: Optional[bool] = Body(False),
    duration_minutes: Optional[int] = Body(0),
    user=Depends(get_current_user)
):
    if not account_ids:
        raise HTTPException(status_code=400, detail="No accounts selected.")

    # Launch tasks with ultra-conservative spacing and custom duration
    tasks = [staggered_join(aid, group_link, skip_join, i, duration_minutes) for i, aid in enumerate(account_ids)]
    results = await asyncio.gather(*tasks)
    
    # Save to history in background
    asyncio.create_task(save_to_history(str(user.id), group_link, "join", results))
    
    return {
        "status": "completed",
        "results": results
    }

@router.post("/leave")
async def api_leave_voice_chat(
    group_link: str = Body(...),
    account_ids: List[str] = Body(...),
    user=Depends(get_current_user)
):
    if not account_ids:
        raise HTTPException(status_code=400, detail="No accounts selected.")

    tasks = [staggered_leave(aid, group_link, i) for i, aid in enumerate(account_ids)]
    results = await asyncio.gather(*tasks)
    
    # Save to history in background
    asyncio.create_task(save_to_history(str(user.id), group_link, "leave", results))
    
    return {
        "status": "completed",
        "results": results
    }
