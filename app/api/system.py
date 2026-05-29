from fastapi import APIRouter, Depends
import asyncio
from app.api.auth_utils import get_current_user
from app.models import User, TelegramAccount
from app.services.auto_reply.engine import detach_account, attach_handler
from app.services.forwarder.logic import start_forwarder_for_account, stop_forwarder_for_rule
from app.services.terminal_service import terminal_manager
from app.services.account_aging import start_aging, stop_aging
from app.client_cache import get_client, invalidate
from app.models.auto_reply import AutoReplySettings
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/stop-all")
async def stop_all_services(current_user: User = Depends(get_current_user)):
    # ── Universal Stop Logic ──────────────────────────────────────────────────
    user_id = str(current_user.id)
    from app.client_cache import refresh_user_status_cache
    await refresh_user_status_cache(user_id)
    
    # 1. Clear Terminal Logs for fresh view
    from app.models.system_log import SystemLog
    await SystemLog.find(SystemLog.user_id == user_id).delete()
    
    # 2. Stop Accounts & Handlers (Auto-Reply & Forwarder)
    from app.client_cache import get_cached_client
    accounts = await TelegramAccount.find(TelegramAccount.user_id == user_id).to_list()
    for acc in accounts:
        acc_id = str(acc.id)
        client = await get_cached_client(acc_id)
        
        # Stop Auto-Reply
        from app.services.auto_reply.engine import detach_account
        await detach_account(client, acc_id)
        
        # Stop Forwarder
        from app.services.forwarder.logic import stop_all_forwarders_for_account
        await stop_all_forwarders_for_account(acc_id)

        # ── NEW: CLEAR MEMORY CACHE ──
        # This disconnects the client and removes it from RAM pool
        from app.client_cache import invalidate
        await invalidate(acc_id)

    # 3. Stop Reaction Tasks (Mark monitoring ones as cancelled/paused)
    from app.models.reaction import ReactionTask
    from app.services.reaction.logic import _reaction_handlers
    tasks = await ReactionTask.find(ReactionTask.user_id == user_id).to_list()
    for task in tasks:
        if task.status in ["monitoring", "running"]:
            task.status = "paused" # Use paused so start-all knows what to resume
            await task.save()
            # If it's a monitoring task, the loop in logic.py will exit in ~10s

    # 4. Stop Bot Hub Agents
    from app.models.bot_forwarder import BotForwarder
    from app.services.bot_forwarder.bot_service import stop_bot
    bots = await BotForwarder.find(BotForwarder.user_id == user_id).to_list()
    for bot in bots:
        await stop_bot(str(bot.id))

    # 5. Stop Folder Campaigns
    from app.services.folder_campaign import FOLDER_CAMPAIGN_TASKS
    if user_id in FOLDER_CAMPAIGN_TASKS:
        for acc_id, task in list(FOLDER_CAMPAIGN_TASKS[user_id].items()):
            task.stop_requested = True
            # Mark as running in DB so resumption logic knows to pick it up later
            # The run() loop will exit gracefully
            
    # 6. Stop Group Joiners
    from app.services.group_join import GROUP_JOIN_TASKS
    if user_id in GROUP_JOIN_TASKS:
        for acc_id, task in list(GROUP_JOIN_TASKS[user_id].items()):
            task.stop_requested = True
            # Keeps status as 'running' for resumption

    # 7. Stop Member Adder & Message Campaign
    from app.services.member_adder import MEMBER_ADDER_TASKS
    if user_id in MEMBER_ADDER_TASKS:
        MEMBER_ADDER_TASKS[user_id].stop_requested = True
        del MEMBER_ADDER_TASKS[user_id]
        
    from app.services.message_campaign import MESSAGE_CAMPAIGN_TASKS
    if user_id in MESSAGE_CAMPAIGN_TASKS:
        MESSAGE_CAMPAIGN_TASKS[user_id].stop_requested = True
        del MESSAGE_CAMPAIGN_TASKS[user_id]

    # 8. Stop Account Aging
    await stop_aging(user_id)

    await terminal_manager.log_event(user_id, "⏹️ GLOBAL STOP: Auto-Reply, Forwarders, Bots, and Boosters PAUSED.", "system", "system", "WARNING")

    # Update User session stats
    current_user.services_active = False
    current_user.last_stop_at = datetime.now(timezone.utc)
    await current_user.save()
    
    # ── REAL-TIME SYNC ──
    from app.api.ws import manager
    await manager.send_to_user(user_id, {"type": "system_status_updated", "services_active": False})

    return {"status": "success", "message": "All backend services paused."}

from pydantic import BaseModel

class StartOptions(BaseModel):
    auto_reply: bool = True
    forwarder: bool = True
    bot_hub: bool = True
    reaction_booster: bool = True
    reminders: bool = True
    member_adder: bool = True
    folder_campaign: bool = True
    group_join: bool = True

@router.post("/start-all")
async def start_all_services(options: StartOptions, current_user: User = Depends(get_current_user)):
    user_id = str(current_user.id)
    from app.client_cache import refresh_user_status_cache
    await refresh_user_status_cache(user_id)
    
    # ── Universal Start Logic ─────────────────────────────────────────────────
    accounts = await TelegramAccount.find(TelegramAccount.user_id == user_id).to_list()
    
    for acc in accounts:
        acc_id = str(acc.id)
        active_list = []
        
        # 1. Start Auto-Reply
        if options.auto_reply:
            try:
                settings = await AutoReplySettings.find_one(AutoReplySettings.account_id == acc_id)
                if settings and settings.is_enabled:
                    client = await get_client(acc_id, acc.session_string, acc.api_id, acc.api_hash)
                    await attach_handler(client, acc_id)
                    active_list.append("Auto-Reply")
            except Exception: pass

        # 2. Start Forwarder
        if options.forwarder:
            try:
                from app.models.forwarder import ForwarderRule
                rules = await ForwarderRule.find(ForwarderRule.account_id == acc_id, ForwarderRule.is_enabled == True).to_list()
                if rules:
                    await start_forwarder_for_account(acc_id)
                    active_list.append(f"Forwarder ({len(rules)} Rules)")
            except Exception: pass

        # 3. Start AI Agent (independent — always active if configured)
        try:
            from app.models.ai_agent import AiAgent as AiAgentModel
            from app.services.auto_reply.engine import _attached_handlers
            ai_agent = await AiAgentModel.find_one(AiAgentModel.account_id == acc_id, AiAgentModel.is_active == True)
            if ai_agent:
                if acc_id not in _attached_handlers:
                    client = await get_client(acc_id, acc.session_string, acc.api_id, acc.api_hash)
                    await attach_handler(client, acc_id)
                active_list.append("AI Agent 🤖")
        except Exception: pass

        if active_list:
            summary = " | ".join(active_list)
            await terminal_manager.log_event(user_id, f"✅ STARTED for {acc.phone_number}: {summary}", acc_id, "system", "SUCCESS")


    # 3. Resume Reaction monitoring tasks
    if options.reaction_booster:
        from app.models.reaction import ReactionTask
        from app.services.reaction.logic import execute_reaction_boost
        tasks = await ReactionTask.find(ReactionTask.user_id == user_id, ReactionTask.status == "paused").to_list()
        for t in tasks:
            t.status = "monitoring"
            await t.save()
            asyncio.create_task(execute_reaction_boost(str(t.id), skip_join=True))
            await terminal_manager.log_event(user_id, f"🚀 Reaction Booster resumed: {t.target_link}", str(t.id), "reaction", "SUCCESS")

    # 4. Start Bot Hub Agents
    if options.bot_hub:
        from app.models.bot_forwarder import BotForwarder
        from app.services.bot_forwarder.bot_service import start_bot
        from app.api.auth_utils import check_plan_limit
        
        try:
            await check_plan_limit(current_user, "access_bot_hub")
            enabled_bots = await BotForwarder.find(BotForwarder.user_id == user_id, BotForwarder.is_enabled == True).to_list()
            if enabled_bots:
                import random
                for b_idx, bot in enumerate(enabled_bots):
                    # Stagger jitter to prevent FloodWait login storm
                    if b_idx > 0: await asyncio.sleep(random.uniform(1.5, 3.0))
                    try:
                        await start_bot(bot)
                    except Exception as b_err:
                        await terminal_manager.log_event(user_id, f"❌ Failed to start Bot Hub agent {bot.name}: {str(b_err)}", str(bot.id), "bot_hub", "ERROR")
                
                await terminal_manager.log_event(user_id, f"🤖 Bot Hub: {len(enabled_bots)} agents brought ONLINE.", "system", "system", "SUCCESS")
        except Exception as plan_err:
            await terminal_manager.log_event(user_id, f"⚠️ Bot Hub skipped: {str(plan_err)}", "system", "system", "WARNING")

    # 5. Global Reminder check
    if options.reminders:
        await terminal_manager.log_event(user_id, "🔔 Scheduled Reminders engine ACTIVATED.", "system", "system", "SUCCESS")
    
    # 6. Member Adder Scheduler Check
    if options.member_adder:
        from app.models import MemberAddSchedule
        schedule = await MemberAddSchedule.find_one(MemberAddSchedule.user_id == user_id, MemberAddSchedule.is_active == True)
        if schedule:
            await terminal_manager.log_event(user_id, f"📅 Member Adding Scheduler ACTIVATED ({schedule.label}).", "system", "system", "SUCCESS")

    # 7. Folder Campaign Engine Status & Resumption
    if options.folder_campaign:
        from app.models.folder_campaign import FolderCampaignJob
        from app.services.folder_campaign import ActiveFolderCampaign, FOLDER_CAMPAIGN_TASKS
        
        # Resume any tasks that were marked as 'running' but didn't finish
        running_folder_jobs = await FolderCampaignJob.find(
            FolderCampaignJob.user_id == user_id, 
            FolderCampaignJob.status == "running"
        ).to_list()
        
        for job in running_folder_jobs:
            # Check if already in memory
            if user_id in FOLDER_CAMPAIGN_TASKS and job.account_id in FOLDER_CAMPAIGN_TASKS[user_id]:
                continue
                
            await terminal_manager.log_event(user_id, f"📂 Resuming Folder Campaign: {job.folder_name} ({job.phone_number})", job.account_id, "folder_campaign", "INFO")
            
            task = ActiveFolderCampaign(
                user_id=job.user_id,
                account_id=job.account_id,
                phone_number=job.phone_number,
                folder_id=job.folder_id,
                folder_name=job.folder_name,
                selected_group_ids=job.selected_group_ids,
                message_text=job.message_text,
                min_delay=job.min_delay,
                max_delay=job.max_delay,
                repeat_interval=job.repeat_interval,
                group_metadata=getattr(job, 'group_metadata', {})
            )
            task.job_id = str(job.id)
            
            if user_id not in FOLDER_CAMPAIGN_TASKS:
                FOLDER_CAMPAIGN_TASKS[user_id] = {}
            FOLDER_CAMPAIGN_TASKS[user_id][job.account_id] = task
            asyncio.create_task(task.run())

        await terminal_manager.log_event(user_id, "📂 Folder Campaign module READY and ENABLED.", "system", "folder_campaign", "SUCCESS")

    # 8. Group Joiner Status & Resumption
    if options.group_join:
        from app.models import GroupJoinJob
        from app.services.group_join import ActiveGroupJoiner, GROUP_JOIN_TASKS
        
        running_join_jobs = await GroupJoinJob.find(
            GroupJoinJob.user_id == user_id,
            GroupJoinJob.status == "running"
        ).to_list()
        
        for job in running_join_jobs:
            if user_id in GROUP_JOIN_TASKS and job.account_id in GROUP_JOIN_TASKS[user_id]:
                continue
            
            await terminal_manager.log_event(user_id, f"🔗 Resuming Group Joiner: {len(job.links)} links ({job.phone_number})", job.account_id, "group_join", "INFO")
            
            task = ActiveGroupJoiner(
                user_id=job.user_id,
                account_id=job.account_id,
                phone_number=job.phone_number,
                links=job.links,
                interval=job.interval,
                batch_id=job.batch_id,
                task_type=job.task_type
            )
            task.job_id = str(job.id)
            task.done_count = job.done_count
            
            if user_id not in GROUP_JOIN_TASKS:
                GROUP_JOIN_TASKS[user_id] = {}
            GROUP_JOIN_TASKS[user_id][job.account_id] = task
            asyncio.create_task(task.run())
            
        await terminal_manager.log_event(user_id, "🔗 Group Joiner module READY and ENABLED.", "system", "group_join", "SUCCESS")

    # 9. Start Account Aging
    await start_aging(user_id)

    # Update User session stats
    current_user.services_active = True
    current_user.last_start_at = datetime.now(timezone.utc)
    await current_user.save()

    # ── REAL-TIME SYNC ──
    from app.api.ws import manager
    await manager.send_to_user(user_id, {"type": "system_status_updated", "services_active": True})

    return {"status": "success", "message": "All backend services re-activated."}

@router.get("/status")
async def get_system_status(current_user: User = Depends(get_current_user)):
    return {
        "services_active": current_user.services_active,
        "last_start_at": current_user.last_start_at,
        "last_stop_at": current_user.last_stop_at
    }
