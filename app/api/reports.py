from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Dict
from app.api.auth_utils import get_current_user
from app.models import User, TelegramAccount
from app.models.report_job import ReportJob
from app.services.report_service import REPORT_TASKS, ActiveReportCampaign
from bson import ObjectId
import asyncio
import json
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

class AccountConfig(BaseModel):
    id: str
    phone: str

class ReportStartRequest(BaseModel):
    target: str
    reason: str
    account_configs: List[AccountConfig]
    messages: List[str] = []
    min_delay: int = 15
    max_delay: int = 20
    batch_size: int = 1

@router.post("/start")
async def start_report_campaign(req: ReportStartRequest, current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    # Plan limit check
    await check_plan_limit(current_user, "access_reports")

    user_id = str(current_user.id)

    # Check if a task is already running
    if user_id in REPORT_TASKS:
        existing = REPORT_TASKS[user_id]
        if not existing.is_done:
            raise HTTPException(status_code=400, detail="A report campaign is already running for your account.")

    if not req.target:
        raise HTTPException(status_code=400, detail="Target username or group link is required.")

    if not req.account_configs:
        raise HTTPException(status_code=400, detail="Select at least one account to perform reporting.")

    if req.batch_size < 1 or req.batch_size > 10:
        raise HTTPException(status_code=400, detail="Batch size must be between 1 and 10.")

    # IDOR check: Verify account ownership
    provided_ids = [ObjectId(acc.id) for acc in req.account_configs]
    owned_accounts = await TelegramAccount.find(
        {"_id": {"$in": provided_ids}, "user_id": user_id}
    ).to_list()

    if len(owned_accounts) != len(req.account_configs):
        raise HTTPException(
            status_code=403,
            detail="One or more selected accounts do not belong to you or do not exist."
        )

    # 1. Create DB entry
    db_job = ReportJob(
        user_id=user_id,
        target=req.target,
        reason=req.reason,
        account_configs=[{"id": acc.id, "phone": acc.phone} for acc in req.account_configs],
        messages=req.messages,
        min_delay=req.min_delay,
        max_delay=req.max_delay,
        batch_size=req.batch_size,
        status="running",
        done_count=0,
        total_count=len(req.account_configs),
        logs=[]
    )
    await db_job.insert()

    # 2. Setup active campaign task
    task = ActiveReportCampaign(
        user_id=user_id,
        target=req.target,
        reason=req.reason,
        account_configs=[{"id": acc.id, "phone": acc.phone} for acc in req.account_configs],
        messages=req.messages,
        min_delay=req.min_delay,
        max_delay=req.max_delay,
        batch_size=req.batch_size
    )
    task.job_id = str(db_job.id)
    REPORT_TASKS[user_id] = task

    # 3. Trigger in background
    asyncio.create_task(task.run())

    return {"status": "success", "message": "Reporting campaign initiated successfully.", "job_id": str(db_job.id)}

@router.post("/stop")
async def stop_report_campaign(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    if user_id in REPORT_TASKS:
        task = REPORT_TASKS[user_id]
        task.stop_requested = True
        return {"status": "success", "message": "Stop signal sent to active campaign."}
    return {"status": "error", "message": "No active campaign running."}

@router.get("/active-task")
async def get_active_task(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    
    # Check memory first
    if user_id in REPORT_TASKS:
        task = REPORT_TASKS[user_id]
        if not task.is_done:
            return {
                "active": True,
                "status": task.status,
                "done": task.done_count,
                "total": task.total_count,
                "errors": task.errors_count,
                "logs": task.logs[-50:]
            }

    # Otherwise fetch latest running from DB (e.g. after backend restart)
    job = await ReportJob.find_one(
        ReportJob.user_id == user_id,
        ReportJob.status == "running"
    )
    if job:
        return {
            "active": True,
            "status": job.status,
            "done": job.done_count,
            "total": job.total_count,
            "errors": job.errors_count,
            "logs": job.logs[-50:]
        }

    return {"active": False}

@router.get("/stream")
async def stream_report_logs(token: str = Query(...)):
    from app.api.auth_utils import get_user_from_token
    user = await get_user_from_token(token)
    if not user:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "Unauthorized"})}])
        
    user_id = str(user.id)

    async def event_generator():
        # Yield initial status if exists
        task = REPORT_TASKS.get(user_id)
        if task:
            yield {
                "event": "info",
                "data": json.dumps({
                    "status": task.status,
                    "done": task.done_count,
                    "total": task.total_count,
                    "errors": task.errors_count,
                    "logs": task.logs[-30:]
                })
            }

            # Listen to new logs queue
            while not task.is_done or not task._queue.empty():
                try:
                    log_item = await asyncio.wait_for(task.get_log_event(), timeout=1.0)
                    yield {
                        "event": "log",
                        "data": json.dumps({
                            "log": log_item,
                            "done": task.done_count,
                            "errors": task.errors_count,
                            "status": task.status
                        })
                    }
                except asyncio.TimeoutError:
                    if task.is_done:
                        break
                    continue
        
        yield {
            "event": "end",
            "data": json.dumps({"status": "done"})
        }

    return EventSourceResponse(event_generator())

@router.get("/history")
async def get_report_history(
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=100)
):
    user_id = str(current_user.id)
    jobs = await ReportJob.find(ReportJob.user_id == user_id).sort(-ReportJob.created_at).limit(limit).to_list()
    return jobs

@router.delete("/history-all")
async def delete_all_report_history(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    await ReportJob.find(ReportJob.user_id == user_id).delete()
    return {"status": "success", "message": "All report history cleared"}

@router.delete("/history/{job_id}")
async def delete_report_history(job_id: str, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    job = await ReportJob.find_one(
        ReportJob.id == ObjectId(job_id),
        ReportJob.user_id == user_id
    )
    if not job:
        raise HTTPException(status_code=404, detail="History entry not found")
    await job.delete()
    return {"status": "success", "message": "History entry deleted"}
