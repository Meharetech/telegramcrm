from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from typing import List, Optional
from pydantic import BaseModel
from app.models.reaction import ReactionTask
from app.models.account import TelegramAccount
from app.services.reaction.logic import execute_reaction_boost
from app.api.auth_utils import get_current_user

router = APIRouter()

class ReactionRequest(BaseModel):
    target_link: str
    message_id: Optional[int] = None
    emojis: List[str]
    task_type: str = "one_time"
    account_ids: List[str]
    min_delay: int = 5
    max_delay: int = 15

@router.post("/start")
async def start_reaction_task(req: ReactionRequest, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    """
    Starts a new reaction boosting task.
    """
    if not req.account_ids:
        raise HTTPException(status_code=400, detail="No accounts selected")
    if not req.emojis:
        raise HTTPException(status_code=400, detail="No emojis selected")

    from app.api.auth_utils import check_plan_limit
    task_count = await ReactionTask.find(ReactionTask.user_id == str(user.id), ReactionTask.status != "cancelled", ReactionTask.status != "done").count()
    await check_plan_limit(user, "max_reaction_channels", task_count)

    task = ReactionTask(
        user_id=str(user.id),
        target_link=req.target_link,
        message_id=req.message_id,
        emojis=req.emojis,
        task_type=req.task_type,
        account_ids=req.account_ids,
        min_delay=req.min_delay,
        max_delay=req.max_delay,
        status="pending"
    )
    await task.insert()
    
    background_tasks.add_task(execute_reaction_boost, str(task.id))
    
    return {"status": "success", "task_id": str(task.id)}

@router.get("/tasks")
async def list_reaction_tasks(user=Depends(get_current_user)):
    return await ReactionTask.find(ReactionTask.user_id == str(user.id)).sort("-created_at").to_list()

@router.get("/task/{task_id}")
async def get_task_status(task_id: str, user=Depends(get_current_user)):
    task = await ReactionTask.get(task_id)
    if not task or task.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@router.put("/task/{task_id}")
async def update_reaction_task(task_id: str, req: ReactionRequest, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    task = await ReactionTask.get(task_id)
    if not task or task.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Task not found")

    # Halt current listeners
    if task.status in ["running", "monitoring"]:
        task.status = "cancelled"
        await task.save()
        
    # Update properties
    task.target_link = req.target_link
    task.emojis = req.emojis
    task.account_ids = req.account_ids
    task.min_delay = req.min_delay
    task.max_delay = req.max_delay
    task.status = "pending"
    task.is_active = True
    await task.save()

    # Create a delayed restart task so the old loop clearly exits before the new one binds
    import asyncio
    async def delayed_restart(tid: str):
        await asyncio.sleep(2)
        await execute_reaction_boost(tid)

    background_tasks.add_task(delayed_restart, str(task.id))
    return {"status": "updated", "task_id": str(task.id)}

@router.delete("/task/{task_id}")
async def delete_task(task_id: str, user=Depends(get_current_user)):
    task = await ReactionTask.get(task_id)
    if not task or task.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Task not found")
    
    # If running or monitoring, mark as cancelled so the background loop stops
    if task.status in ["running", "monitoring"]:
        task.status = "cancelled"
        await task.save()
    
    await task.delete()
    return {"status": "deleted"}
