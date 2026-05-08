from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId
from pydantic import BaseModel
from typing import List, Optional, Dict
from app.api.auth_utils import get_current_user
from app.models import User
from app.services.folder_campaign import FOLDER_CAMPAIGN_TASKS, ActiveFolderCampaign
import asyncio
import json
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

from app.models.folder_campaign import FolderCampaignJob

class FolderCampaignRequest(BaseModel):
    account_id: str
    phone_number: str
    folder_id: str
    folder_name: str
    selected_group_ids: List[str]
    message_text: str
    min_delay: int = 30
    max_delay: int = 60
    repeat_interval: Optional[int] = None # in minutes
    group_metadata: Optional[Dict[str, dict]] = {}

@router.post("/start")
async def start_folder_campaign(req: FolderCampaignRequest, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_folder_campaign")
    
    # Initialize user dict if not exists
    if user_id not in FOLDER_CAMPAIGN_TASKS:
        FOLDER_CAMPAIGN_TASKS[user_id] = {}

    # Check if this account already has a campaign running
    if req.account_id in FOLDER_CAMPAIGN_TASKS[user_id]:
        if not FOLDER_CAMPAIGN_TASKS[user_id][req.account_id].is_done:
            raise HTTPException(status_code=400, detail="This account already has a campaign running.")

    # Check total active campaigns for this user against plan limit
    # We also check DB for 'running' jobs that aren't in memory yet
    from app.models.folder_campaign import FolderCampaignJob
    active_jobs_in_db = await FolderCampaignJob.find(
        FolderCampaignJob.user_id == user_id,
        FolderCampaignJob.status == "running"
    ).count()
    
    active_in_mem = len([t for t in FOLDER_CAMPAIGN_TASKS[user_id].values() if not t.is_done])
    total_active = max(active_jobs_in_db, active_in_mem)
    
    await check_plan_limit(current_user, "max_folder_accounts", total_active)

    task = ActiveFolderCampaign(
        user_id=user_id,
        account_id=req.account_id,
        phone_number=req.phone_number,
        folder_id=req.folder_id,
        folder_name=req.folder_name,
        selected_group_ids=req.selected_group_ids,
        message_text=req.message_text,
        min_delay=req.min_delay,
        max_delay=req.max_delay,
        repeat_interval=req.repeat_interval,
        group_metadata=req.group_metadata
    )
    
    # ── Handle Offline Mode ──
    if not current_user.services_active:
        task.status = "running" # Mark as running so resumption logic picks it up
        await task.sync_to_db()
        return {
            "status": "success", 
            "message": "Campaign SAVED and QUEUED. It will start automatically once you 'LAUNCH SYSTEM' in the Terminal."
        }

    # ── Handle Online Mode ──
    FOLDER_CAMPAIGN_TASKS[user_id][req.account_id] = task
    asyncio.create_task(task.run())
    return {"status": "success", "message": "Folder campaign started and LIVE."}

@router.post("/stop")
async def stop_folder_campaign(account_id: str = Query(...), current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    if user_id in FOLDER_CAMPAIGN_TASKS and account_id in FOLDER_CAMPAIGN_TASKS[user_id]:
        task = FOLDER_CAMPAIGN_TASKS[user_id][account_id]
        task.is_manual_stop = True
        task.stop_requested = True
        task.status = "stopped"
        await task.sync_to_db()
        return {"status": "success", "message": "Stop signal sent."}
    return {"status": "error", "message": "No active campaign found for this account."}

@router.get("/active-phones")
async def get_active_folder_campaign_phones(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    active_phones = set()
    
    # 1. Check memory tasks
    if user_id in FOLDER_CAMPAIGN_TASKS:
        for task in FOLDER_CAMPAIGN_TASKS[user_id].values():
            if not task.is_done:
                active_phones.add(task.phone_number)
                
    # 2. Check DB jobs (for offline/paused tasks)
    db_jobs = await FolderCampaignJob.find(
        FolderCampaignJob.user_id == user_id,
        FolderCampaignJob.status == "running"
    ).to_list()
    for job in db_jobs:
        active_phones.add(job.phone_number)
        
    return list(active_phones)

@router.get("/history")
async def get_folder_campaign_history(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    return await FolderCampaignJob.find(FolderCampaignJob.user_id == user_id).sort(-FolderCampaignJob.created_at).to_list()

@router.delete("/history/{job_id}")
async def delete_folder_campaign_history(job_id: str, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    job = await FolderCampaignJob.find_one(FolderCampaignJob.id == ObjectId(job_id), FolderCampaignJob.user_id == user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    await job.delete()
    return {"status": "success", "message": "Job deleted."}

@router.get("/stream")
async def stream_folder_campaign(account_id: str = Query(...), token: str = Query(...)):
    from app.api.auth_utils import get_user_from_token
    user = await get_user_from_token(token)
    if not user:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "Unauthorized"})}])
    
    user_id = str(user.id)
    task = FOLDER_CAMPAIGN_TASKS.get(user_id, {}).get(account_id)
    if not task:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "No active campaign found for this account"})}])

    async def event_generator():
        # First send existing logs
        async with task.lock:
            for log in task.logs:
                yield {"event": "log", "data": json.dumps(log)}

        queue = asyncio.Queue()
        async with task.lock:
            task.queues.append(queue)
        try:
            while True:
                msg = await queue.get()
                try:
                    yield msg
                except (ConnectionResetError, BrokenPipeError, anyio.EndOfStream if 'anyio' in globals() else Exception):
                    # Handle browser disconnection gracefully on Windows
                    break

                if msg["event"] == "done" or task.is_done:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Silence noisy 10054 errors common on Windows
            if "10054" not in str(e):
                logger.error(f"SSE Stream error: {e}")
        finally:
            async with task.lock:
                if queue in task.queues:
                    task.queues.remove(queue)

    return EventSourceResponse(event_generator())
