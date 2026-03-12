from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
from app.api.auth_utils import get_current_user
from app.models import User, MemberAddSettings
from app.services.member_adder import MEMBER_ADDER_TASKS, ActiveMemberAdder
import asyncio
import json
import uuid
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

class AccountConfig(BaseModel):
    id: str
    count: int

class MemberAddRequest(BaseModel):
    group_link: str
    account_configs: List[AccountConfig]
    min_delay: int = 30
    max_delay: int = 60

@router.post("/start")
async def start_member_adder(req: MemberAddRequest, current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_member_adding")
    
    user_id = str(current_user.id)
    
    # Check if a task is already running
    if user_id in MEMBER_ADDER_TASKS:
        existing = MEMBER_ADDER_TASKS[user_id]
        if not existing.is_done:
            raise HTTPException(status_code=400, detail="A member adding task is already running for your account.")

    # ── SECURITY FIX: Verify account ownership (IDOR Protection) ────────────────
    provided_ids = [ObjectId(acc.id) for acc in req.account_configs]
    owned_accounts = await TelegramAccount.find(
        {"_id": {"$in": provided_ids}, "user_id": user_id}
    ).to_list()
    
    if len(owned_accounts) != len(req.account_configs):
        raise HTTPException(
            status_code=403, 
            detail="One or more provided account IDs do not belong to your account or do not exist."
        )

    # Create new task
    task = ActiveMemberAdder(
        user_id=user_id,
        group_link=req.group_link,
        account_configs=req.account_configs,
        min_delay=req.min_delay,
        max_delay=req.max_delay
    )
    MEMBER_ADDER_TASKS[user_id] = task
    
    # Run in background
    asyncio.create_task(task.run())
    
    return {"status": "success", "message": "Member adding task initialized."}

@router.post("/stop")
async def stop_member_adder(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    if user_id in MEMBER_ADDER_TASKS:
        task = MEMBER_ADDER_TASKS[user_id]
        task.stop_requested = True
        return {"status": "success", "message": "Stop signal sent to active task."}
    return {"status": "error", "message": "No active task found."}

@router.get("/active-task")
async def get_active_task(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    
    # 1. Check Memory first
    if user_id in MEMBER_ADDER_TASKS:
        task = MEMBER_ADDER_TASKS[user_id]
        if not task.is_done:
            return {
                "active": True,
                "status": task.status,
                "done": task.done_count,
                "total": task.total_count,
                "errors": task.errors_count,
                "logs": task.logs[-50:]
            }
            
    # 2. Check Database for running tasks (e.g. after restart)
    from app.models.member_add_job import MemberAddJob
    job = await MemberAddJob.find_one(
        MemberAddJob.user_id == user_id, 
        MemberAddJob.status == "running"
    )
    if job:
        return {
            "active": True,
            "resumed": True, # Flag for frontend
            "status": job.status,
            "done": job.done_count,
            "total": job.total_count,
            "errors": job.errors_count,
            "logs": job.logs
        }
        
    return {"active": False}

@router.get("/stream")
async def stream_member_adder(
    token: str = Query(...),
):
    from app.api.auth_utils import get_user_from_token
    user = await get_user_from_token(token)
    if not user:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "Unauthorized"})}])
    
    user_id = str(user.id)
    task = MEMBER_ADDER_TASKS.get(user_id)
    
    if not task:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "No active task found"})}])

    async def event_generator():
        # 1. Send historical logs
        async with task.lock:
            for log in task.logs:
                # We need to guess the event type or just use 'log'
                # For compatibility, we'll use 'log' for all history
                yield {"event": "log", "data": json.dumps(log)}

        # 2. Subscribe to new logs
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

@router.get("/status")
async def get_member_adder_status(current_user: User = Depends(get_current_user)):
    # Legacy endpoint for compatibility if needed, but we'll use SSE
    user_id = str(current_user.id)
    task = MEMBER_ADDER_TASKS.get(user_id)
    if not task:
        return {"running": False, "done": 0, "total": 0, "errors": 0, "logs": []}
    
    return {
        "running": not task.is_done,
        "done": task.done_count,
        "total": task.total_count,
        "errors": task.errors_count,
        "logs": task.logs,
        "status": task.status
    }

@router.get("/settings")
async def get_mission_settings(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    settings = await MemberAddSettings.find_one(MemberAddSettings.user_id == user_id)
    if not settings:
        # Create default
        settings = MemberAddSettings(user_id=user_id)
        await settings.insert()
    return settings

class UpdateSettingsRequest(BaseModel):
    consecutive_privacy_threshold: int
    max_flood_sleep_threshold: int
    account_limit_cap: int
    cooldown_24h: int

@router.post("/settings")
async def update_mission_settings(req: UpdateSettingsRequest, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    settings = await MemberAddSettings.find_one(MemberAddSettings.user_id == user_id)
    if not settings:
        settings = MemberAddSettings(user_id=user_id)
    
    settings.consecutive_privacy_threshold = req.consecutive_privacy_threshold
    settings.max_flood_sleep_threshold = req.max_flood_sleep_threshold
    settings.account_limit_cap = req.account_limit_cap
    settings.cooldown_24h = req.cooldown_24h
    
    await settings.save()
    return {"status": "success", "message": "Mission settings updated."}

@router.get("/history")
async def get_mission_history(current_user: User = Depends(get_current_user)):
    from app.models.member_add_job import MemberAddJob
    user_id = str(current_user.id)
    
    # Fetch last 20 jobs for this user
    jobs = await MemberAddJob.find(
        MemberAddJob.user_id == user_id
    ).sort(-MemberAddJob.updated_at).limit(20).to_list()
    
    return jobs
