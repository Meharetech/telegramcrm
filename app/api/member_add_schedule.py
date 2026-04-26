from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from app.api.auth_utils import get_current_user
from app.models import User, MemberAddSchedule
from datetime import datetime, timezone
from bson import ObjectId

router = APIRouter()

class AccountConfig(BaseModel):
    id: str
    count: int

class ScheduleRequest(BaseModel):
    id: Optional[str] = None
    label: str = "Daily Mission"
    destination_group: str
    destination_group_name: Optional[str] = None
    account_configs: List[AccountConfig]
    scheduled_time: str # "HH:MM"
    min_delay: int = 30
    max_delay: int = 60
    is_active: bool = True
    source_type: str = "contacts"
    member_list: List[str] = []

@router.get("/")
async def get_schedules(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    schedules = await MemberAddSchedule.find(MemberAddSchedule.user_id == user_id).to_list()
    return schedules

@router.post("/")
async def create_or_update_schedule(req: ScheduleRequest, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    
    # Restrict to one schedule per user as requested
    schedule = await MemberAddSchedule.find_one(MemberAddSchedule.user_id == user_id)
    
    if not schedule:
        schedule = MemberAddSchedule(
            user_id=user_id,
            label=req.label,
            destination_group=req.destination_group,
            destination_group_name=req.destination_group_name,
            account_configs=[c.model_dump() for c in req.account_configs],
            scheduled_time=req.scheduled_time,
            min_delay=req.min_delay,
            max_delay=req.max_delay,
            is_active=req.is_active,
            source_type=req.source_type,
            member_list=req.member_list
        )
        await schedule.insert()
    else:
        schedule.label = req.label
        schedule.destination_group = req.destination_group
        schedule.destination_group_name = req.destination_group_name
        schedule.account_configs = [c.model_dump() for c in req.account_configs]
        schedule.scheduled_time = req.scheduled_time
        schedule.min_delay = req.min_delay
        schedule.max_delay = req.max_delay
        schedule.is_active = req.is_active
        schedule.source_type = req.source_type
        schedule.member_list = req.member_list
        schedule.updated_at = datetime.now(timezone.utc)
        await schedule.save()
    
    return {"status": "success", "message": "Automation mission saved.", "schedule": schedule}

@router.post("/toggle")
async def toggle_schedule(current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    schedule = await MemberAddSchedule.find_one(MemberAddSchedule.user_id == user_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="No automation schedule found.")
    
    schedule.is_active = not schedule.is_active
    schedule.updated_at = datetime.now(timezone.utc)
    await schedule.save()
    
    status_str = "ENABLED" if schedule.is_active else "DISABLED"
    from app.services.terminal_service import terminal_manager
    await terminal_manager.log_event(user_id, f"📅 Automation {status_str}: {schedule.label}", module="member_adder", level="SUCCESS")
    
    return {"status": "success", "is_active": schedule.is_active}

@router.delete("/{id}")
async def delete_schedule(id: str, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    
    # We delete ALL schedules for this user to ensure its clean
    # as we now only support one per user.
    await MemberAddSchedule.find(MemberAddSchedule.user_id == user_id).delete()
    
    return {"status": "success", "message": "Automation cleared."}
