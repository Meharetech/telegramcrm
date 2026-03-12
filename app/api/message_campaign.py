from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
from app.api.auth_utils import get_current_user
from app.models import User, MessageCampaignJob
from app.services.message_campaign import MESSAGE_CAMPAIGN_TASKS, ActiveMessageCampaign
import asyncio
import json
import uuid
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

class AccountConfig(BaseModel):
    id: str
    count: int

class MessageCampaignRequest(BaseModel):
    method: str # 'contact' or 'username'
    username_list: List[str] = []
    message_text: str
    account_configs: List[AccountConfig]
    min_delay: int = 30
    max_delay: int = 60

@router.post("/start")
async def start_message_campaign(req: MessageCampaignRequest, current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_message_sender")
    user_id = str(current_user.id)
    if user_id in MESSAGE_CAMPAIGN_TASKS:
        existing = MESSAGE_CAMPAIGN_TASKS[user_id]
        if not existing.is_done:
            raise HTTPException(status_code=400, detail="A message campaign is already running for your account.")

    # ── SECURITY FIX: Verify account ownership (IDOR Protection) ────────────────
    from bson import ObjectId
    from app.models.account import TelegramAccount
    provided_ids = [ObjectId(acc.id) for acc in req.account_configs]
    owned_accounts = await TelegramAccount.find(
        {"_id": {"$in": provided_ids}, "user_id": user_id}
    ).to_list()
    
    if len(owned_accounts) != len(req.account_configs):
        raise HTTPException(
            status_code=403, 
            detail="One or more provided account IDs do not belong to your account or do not exist."
        )

    task = ActiveMessageCampaign(
        user_id=user_id,
        method=req.method,
        message_text=req.message_text,
        account_configs=req.account_configs,
        min_delay=req.min_delay,
        max_delay=req.max_delay,
        username_list=req.username_list
    )
    MESSAGE_CAMPAIGN_TASKS[user_id] = task
    asyncio.create_task(task.run())
    return {"status": "success", "message": "Message campaign started."}

@router.post("/stop")
async def stop_message_campaign(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    if user_id in MESSAGE_CAMPAIGN_TASKS:
        task = MESSAGE_CAMPAIGN_TASKS[user_id]
        task.stop_requested = True
        return {"status": "success", "message": "Stop signal sent."}
    return {"status": "error", "message": "No active campaign found."}

@router.get("/active-task")
async def get_active_task(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    if user_id in MESSAGE_CAMPAIGN_TASKS:
        task = MESSAGE_CAMPAIGN_TASKS[user_id]
        if not task.is_done:
            return {
                "active": True,
                "status": task.status,
                "done": task.done_count,
                "total": task.total_targets,
                "errors": task.errors_count,
                "logs": task.logs[-50:]
            }
    
    # Check DB for running tasks (e.g. after restart)
    job = await MessageCampaignJob.find_one(
        MessageCampaignJob.user_id == user_id, 
        MessageCampaignJob.status == "running"
    )
    if job:
        return {
            "active": True,
            "resumed": True,
            "status": job.status,
            "done": job.done_count,
            "total": job.total_targets,
            "errors": job.errors_count,
            "logs": job.logs[-50:]
        }
    return {"active": False}

@router.get("/stream")
async def stream_message_campaign(token: str = Query(...)):
    from app.api.auth_utils import get_user_from_token
    user = await get_user_from_token(token)
    if not user:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "Unauthorized"})}])
    
    user_id = str(user.id)
    task = MESSAGE_CAMPAIGN_TASKS.get(user_id)
    if not task:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "No active campaign found"})}])

    async def event_generator():
        async with task.lock:
            for log in task.logs:
                yield {"event": "log", "data": json.dumps(log)}

        queue = asyncio.Queue()
        async with task.lock:
            task.queues.append(queue)
        try:
            while True:
                msg = await queue.get()
                yield msg
                if msg["event"] == "done" or task.is_done:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            async with task.lock:
                if queue in task.queues:
                    task.queues.remove(queue)

    return EventSourceResponse(event_generator())

@router.get("/history")
async def get_message_campaign_history(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    jobs = await MessageCampaignJob.find(
        MessageCampaignJob.user_id == user_id
    ).sort(-MessageCampaignJob.updated_at).limit(20).to_list()
    return jobs
