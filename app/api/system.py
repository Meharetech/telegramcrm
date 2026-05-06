from fastapi import APIRouter, Depends
from app.api.auth_utils import get_current_user
from app.models import User, TelegramAccount
from app.services.auto_reply.engine import detach_account, attach_handler
from app.services.forwarder.logic import start_forwarder_for_account, stop_forwarder_for_rule
from app.services.terminal_service import terminal_manager
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

        if active_list:
            summary = " | ".join(active_list)
            await terminal_manager.log_event(user_id, f"✅ STARTED for {acc.phone_number}: {summary}", acc_id, "system", "SUCCESS")


    # 3. Resume Reaction monitoring tasks
    if options.reaction_booster:
        from app.models.reaction import ReactionTask
        from app.services.reaction.logic import execute_reaction_boost
        from asyncio import create_task
        tasks = await ReactionTask.find(ReactionTask.user_id == user_id, ReactionTask.status == "paused").to_list()
        for t in tasks:
            t.status = "monitoring"
            await t.save()
            create_task(execute_reaction_boost(str(t.id)))
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
                import asyncio
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
