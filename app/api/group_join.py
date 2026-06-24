from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from bson import ObjectId
from typing import List, Optional
from app.api.auth_utils import get_current_user
from app.models import User, GroupJoinJob
from app.services.group_join import GROUP_JOIN_TASKS, GROUP_JOIN_MANAGERS, ActiveGroupJoiner, GroupJoinRotationManager
import asyncio
import json
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

@router.post("/start")
async def start_group_join(
    accounts_json: str = Form(...), # List of {id, phone}
    interval: int = Form(...),
    file: Optional[UploadFile] = File(None),
    links_text: Optional[str] = Form(None),
    task_type: str = Form("join"), # join, leave
    join_mode: str = Form("rotation"), # rotation, mass
    current_user: User = Depends(get_current_user)
):
    user_id = str(current_user.id)
    selected_accounts = json.loads(accounts_json)
    
    if not selected_accounts:
        raise HTTPException(status_code=400, detail="No accounts selected.")
        
    # Plan limit check
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_group_joiner")

    links = []
    if file:
        content = await file.read()
        links = [line.strip() for line in content.decode('utf-8', errors='ignore').splitlines() if line.strip()]
    elif links_text:
        links = [line.strip() for line in links_text.splitlines() if line.strip()]
    
    if not links:
        raise HTTPException(status_code=400, detail="No group links provided. Upload a file or enter manually.")

    if len(links) > 30:
        raise HTTPException(status_code=400, detail="Maximum 30 links allowed per operation for safety.")


    if user_id not in GROUP_JOIN_TASKS:
        GROUP_JOIN_TASKS[user_id] = {}

    import uuid
    batch_id = str(uuid.uuid4())[:8] # Small unique ID

    manager = GroupJoinRotationManager(
        user_id=user_id,
        accounts_info=selected_accounts,
        links=links,
        interval=interval,
        batch_id=batch_id,
        task_type=task_type,
        join_mode=join_mode
    )


    GROUP_JOIN_MANAGERS[user_id] = manager
    asyncio.create_task(manager.run())
        
    return {"status": "success", "message": f"Group joiner started in rotation mode on {len(selected_accounts)} accounts."}


@router.post("/stop")
async def stop_group_join(account_id: str = Query(...), current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    
    # Stop the manager if it exists
    if user_id in GROUP_JOIN_MANAGERS:
        GROUP_JOIN_MANAGERS[user_id].stop_requested = True
    
    # Also stop individual tasks (for safety and legacy compatibility)
    if user_id in GROUP_JOIN_TASKS:
        if account_id == "all":
            for task in GROUP_JOIN_TASKS[user_id].values():
                task.stop_requested = True
        elif account_id in GROUP_JOIN_TASKS[user_id]:
            task = GROUP_JOIN_TASKS[user_id][account_id]
            task.stop_requested = True
            
    return {"status": "success", "message": "Stop signal sent."}


@router.get("/history")
async def get_join_history(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    return await GroupJoinJob.find(GroupJoinJob.user_id == user_id).sort(-GroupJoinJob.created_at).to_list()

@router.delete("/history/{job_id}")
async def delete_join_job(job_id: str, current_user: User = Depends(get_current_user)):
    job = await GroupJoinJob.get(ObjectId(job_id))
    if job and job.user_id == str(current_user.id):
        await job.delete()
        return {"status": "success"}
    raise HTTPException(status_code=404)

@router.delete("/history/batch/{batch_id}")
async def delete_join_batch(batch_id: str, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    await GroupJoinJob.find(GroupJoinJob.user_id == user_id, GroupJoinJob.batch_id == batch_id).delete()
    return {"status": "success"}

@router.get("/history/{job_id}/download")
async def download_join_log(
    job_id: str, 
    token: Optional[str] = Query(None),
    current_user: Optional[User] = Depends(get_current_user)
):
    # Handle window.open cases where token is in query
    if not current_user and token:
        from app.api.auth_utils import get_user_from_token
        current_user = await get_user_from_token(token)
    
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job = await GroupJoinJob.get(ObjectId(job_id))
    if not job or job.user_id != str(current_user.id):
        raise HTTPException(status_code=404)
    
    content = f"Group Joiner Report for {job.phone_number}\n"
    content += f"Status: {job.status}\n"
    content += f"Started At: {job.created_at}\n"
    content += f"Groups Joined: {job.done_count}/{job.total_count}\n"
    content += "-"*40 + "\n\n"
    
    for log in job.logs:
        content += f"[{log.get('time', '')}] {log.get('msg', '')}\n"
        
    from fastapi.responses import Response
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename=join_report_{job.phone_number}.txt"}
    )

@router.get("/stream")
async def stream_group_join(account_id: str = Query(...), token: str = Query(...)):
    from app.api.auth_utils import get_user_from_token
    user = await get_user_from_token(token)
    if not user:
        return EventSourceResponse([{"event": "error", "data": "Unauthorized"}])
    
    user_id = str(user.id)
    task = GROUP_JOIN_TASKS.get(user_id, {}).get(account_id)
    if not task:
        return EventSourceResponse([{"event": "error", "data": "No active task"}])

    async def event_generator():
        # Send history first
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
        except:
            if queue in task.queues:
                task.queues.remove(queue)

    return EventSourceResponse(event_generator())

@router.get("/stream-batch")
async def stream_group_join_batch(token: str = Query(...)):
    from app.api.auth_utils import get_user_from_token
    user = await get_user_from_token(token)
    if not user:
        return EventSourceResponse([{"event": "error", "data": "Unauthorized"}])
    
    user_id = str(user.id)
    manager = GROUP_JOIN_MANAGERS.get(user_id)
    if not manager:
        return EventSourceResponse([{"event": "error", "data": "No active batch process"}])

    async def event_generator():
        queue = asyncio.Queue()
        async with manager.lock:
            manager.batch_queues.append(queue)
        
        try:
            while True:
                msg = await queue.get()
                yield msg
        except:
            if queue in manager.batch_queues:
                manager.batch_queues.remove(queue)

    return EventSourceResponse(event_generator())

