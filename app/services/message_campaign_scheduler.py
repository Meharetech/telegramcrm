import asyncio
import logging
from datetime import datetime, timezone
from app.models.message_campaign import MessageCampaignSchedule
from app.models import User
from app.services.message_campaign import ActiveMessageCampaign, MESSAGE_CAMPAIGN_TASKS
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

async def start_message_campaign_scheduler():
    """Background loop: check for due message campaigns every 60 seconds."""
    logger.info("[Msg Scheduler] Worker started")
    while True:
        try:
            await check_and_trigger_campaigns()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Msg Scheduler] Worker error: {e}")
        await asyncio.sleep(60)

class DictWrapper:
    """Wraps a dict so that .id and .count work like attribute access."""
    def __init__(self, d):
        self._d = d
    @property
    def id(self):
        return self._d.get("id", "")
    @property
    def count(self):
        return self._d.get("count", 25)

async def check_and_trigger_campaigns():
    now = datetime.now(timezone.utc)
    
    # Find pending schedules that are due
    due_schedules = await MessageCampaignSchedule.find(
        MessageCampaignSchedule.status == "pending",
        MessageCampaignSchedule.scheduled_for <= now
    ).to_list()
    
    if due_schedules:
        logger.info(f"[Msg Scheduler] Found {len(due_schedules)} due missions.")

    for schedule in due_schedules:
        try:
            user_id = schedule.user_id
            
            # 1. Check if user exists
            user = await User.get(user_id)
            if not user:
                logger.warning(f"[Msg Scheduler] User {user_id} not found for schedule {schedule.id}")
                continue
                
            # 2. Check if a task is already running
            if user_id in MESSAGE_CAMPAIGN_TASKS:
                existing_task = MESSAGE_CAMPAIGN_TASKS[user_id]
                if not existing_task.is_done:
                    logger.info(f"[Msg Scheduler] User {user_id} already has an active campaign. Skipping schedule.")
                    continue

            logger.info(f"[Msg Scheduler] TRIGGERING: Mission for user {user_id} (Scheduled: {schedule.scheduled_for})")
            
            # 3. Wrap account_configs dicts as objects so .id / .count work
            wrapped_configs = [DictWrapper(c) if isinstance(c, dict) else c for c in schedule.account_configs]
            
            # 4. Create and start the task
            task = ActiveMessageCampaign(
                user_id=user_id,
                method=schedule.method,
                message_text=schedule.message_text,
                account_configs=wrapped_configs,
                min_delay=schedule.min_delay,
                max_delay=schedule.max_delay,
                username_list=schedule.username_list
            )
            # Mark as scheduled so it bypasses the services_active guard
            task.is_scheduled = True
            MESSAGE_CAMPAIGN_TASKS[user_id] = task
            asyncio.create_task(task.run())
            
            # 5. Update status to completed so it disappears from pending
            schedule.status = "completed" 
            await schedule.save()
            
            await terminal_manager.log_event(
                user_id, 
                f"📅 SCHEDULER: Automated campaign launched successfully.", 
                module="msg_campaign", 
                level="INFO"
            )
            
        except Exception as e:
            logger.error(f"[Msg Scheduler] Failed to trigger schedule {schedule.id}: {e}", exc_info=True)
