from fastapi import APIRouter, Depends, HTTPException
from app.models import User, AccountAgingTask, TelegramAccount
from app.api.auth_utils import get_current_user
from app.services.account_aging import start_aging, stop_aging
from pydantic import BaseModel
from typing import List

router = APIRouter()

class UpdateAgingRequest(BaseModel):
    selected_account_ids: List[str]
    min_delay: int = 10
    max_delay: int = 14
    parallel_sessions: int = 3
    use_max_parallelism: bool = False

@router.get("/")
async def get_aging_status(current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_account_aging")
    
    task = await AccountAgingTask.find_one(AccountAgingTask.user_id == str(current_user.id))
    if not task:
        task = AccountAgingTask(user_id=str(current_user.id))
        await task.insert()
    
    return task

@router.post("/update")
async def update_aging_config(req: UpdateAgingRequest, current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_account_aging")
    
    task = await AccountAgingTask.find_one(AccountAgingTask.user_id == str(current_user.id))
    if not task:
        task = AccountAgingTask(user_id=str(current_user.id))
        await task.insert()
    
    task.selected_account_ids = req.selected_account_ids
    task.min_delay = req.min_delay
    task.max_delay = req.max_delay
    task.parallel_sessions = req.parallel_sessions
    task.use_max_parallelism = req.use_max_parallelism
    await task.save()
    
    return {"status": "success", "message": "Aging configuration updated."}

@router.post("/start")
async def start_aging_api(current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_account_aging")
    
    await start_aging(str(current_user.id))
    return {"status": "success", "message": "Aging service started."}

@router.post("/stop")
async def stop_aging_api(current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_account_aging")
    
    await stop_aging(str(current_user.id))
    return {"status": "success", "message": "Aging service stopped."}
