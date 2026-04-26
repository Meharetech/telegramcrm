import asyncio
import logging
import json
from datetime import datetime, timezone
from app.models import MemberAddSchedule, User
from app.services.member_adder import ActiveMemberAdder, MEMBER_ADDER_TASKS
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

async def start_member_adder_scheduler():
    """Background loop: check for due daily member adding missions every 60 seconds."""
    logger.info("[MA Scheduler] Worker started")
    while True:
        try:
            await check_and_trigger_schedules()
        except asyncio.CancelledError:
            logger.info("[MA Scheduler] Worker cancelled, shutting down")
            break
        except Exception as e:
            logger.error(f"[MA Scheduler] Worker error: {e}")
        await asyncio.sleep(60)

async def check_and_trigger_schedules():
    """Find all active schedules that are due to run at this exact time."""
    now = datetime.now()
    current_time_str = now.strftime("%H:%M")
    current_date_str = now.strftime("%Y-%m-%d")
    
    # Find active schedules for the current time that haven't run today
    due_schedules = await MemberAddSchedule.find(
        MemberAddSchedule.is_active == True,
        MemberAddSchedule.scheduled_time == current_time_str,
        MemberAddSchedule.last_run_date != current_date_str
    ).to_list()
    
    if not due_schedules:
        return

    for schedule in due_schedules:
        try:
            user_id = schedule.user_id
            
            # 1. Check if user services are active
            user = await User.get(user_id)
            if not user or not user.services_active:
                logger.info(f"[MA Scheduler] Skipping schedule for user {user_id}: Services STOPPED.")
                continue
                
            # 2. Check if a task is already running (Manual task takes priority)
            if user_id in MEMBER_ADDER_TASKS:
                task = MEMBER_ADDER_TASKS[user_id]
                if not task.is_done:
                    logger.info(f"[MA Scheduler] Skipping schedule for user {user_id}: Task already running.")
                    continue

            logger.info(f"[MA Scheduler] Triggering daily mission for user {user_id}...")
            
            # 3. Create and start the task
            task = ActiveMemberAdder(
                user_id=user_id,
                group_link=schedule.destination_group,
                account_configs=schedule.account_configs,
                min_delay=schedule.min_delay,
                max_delay=schedule.max_delay
            )
            task.source_type = schedule.source_type or "contacts"
            task.member_list = schedule.member_list or []

            MEMBER_ADDER_TASKS[user_id] = task
            asyncio.create_task(task.run())
            
            # 4. Mark as run for today
            schedule.last_run_date = current_date_str
            await schedule.save()
            
            await terminal_manager.log_event(user_id, f"📅 DAILY SCHEDULER: Initiating automated member adding mission.", module="member_adder", level="INFO")
            
        except Exception as e:
            logger.error(f"[MA Scheduler] Failed to trigger schedule for {schedule.id}: {e}")
