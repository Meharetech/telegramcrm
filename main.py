import asyncio
from datetime import datetime, timezone, timedelta
from asyncio import create_task
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from app.models import (
    User, TelegramAccount, ForwarderRule, TelegramAPI, Reminder,
    Proxy, SystemLog, ReactionTask, MemberAddSettings, MemberAddJob,
    MessageCampaignJob, Plan, Payment, SystemSettings
)
from app.models.auto_reply import AutoReplyRule, AutoReplySettings
from app.api.accounts import router as account_router
from app.api.auto_reply import router as auto_reply_router
from app.api.forwarder import router as forwarder_router
from app.api.ws import router as ws_router
from app.api.users import router as user_router
from app.api.contacts import router as contacts_router
from app.api.proxies import router as proxies_router
from app.api.plans import router as plans_router
from contextlib import asynccontextmanager
from app.client_cache import shutdown_all, start_maintenance
from app.config import settings
import logging
import gc
import os

# ── HIGH PERFORMANCE EVENT LOOP (uvloop) ──────────────────────────────────
if os.name != 'nt': # Only on Linux/Ubuntu
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logging.getLogger(__name__).info("[perf] uvloop event loop policy installed.")
    except ImportError:
        pass # Handle in main logger below

logger = logging.getLogger(__name__)

async def run_system_maintenance():
    """
    Background worker for both RAM health and Security (Session Cleanup).
    Runs every 5 minutes.
    """
    logger.info("[system] Maintenance worker active (RAM + Session Cleanup).")
    while True:
        await asyncio.sleep(300) # Every 5 minutes
        try:
            # 1. Security: Cleanup Expired Login Sessions (Auth.py)
            from app.api.accounts.auth import _cleanup_expired_pending
            await _cleanup_expired_pending()
            
            # 2. RAM: Force Garbage Collection
            collected = gc.collect()
            
            # 3. RAM: Clear Telethon Internal Entity Caches
            from app.client_cache import _cache
            for client in _cache.values():
                if client and client.is_connected():
                    client._entity_cache.clear()
            
            logger.info(f"[system] Maintenance complete. GC collected {collected} objects.")
        except Exception as e:
            logger.error(f"[system] Maintenance error: {e}")


async def resume_background_services():
    """
    Search for all enabled auto-replies and forwarder rules and (re-)attach their handlers.
    This ensures that background processing resumes automatically after a server restart.
    """
    from app.api.auto_reply import _activate_worker
    from app.services.forwarder.logic import start_forwarder_for_account
    from app.services.member_adder import ActiveMemberAdder, MEMBER_ADDER_TASKS
    from app.services.message_campaign import ActiveMessageCampaign, MESSAGE_CAMPAIGN_TASKS
    from app.client_cache import is_user_active

    logger.info("[startup] Scanning for services to resume...")
    
    # 1. Start Auto-Reply Workers for active accounts
    active_settings = await AutoReplySettings.find(AutoReplySettings.is_enabled == True).to_list()
    auto_tasks = []
    for s in active_settings:
        if await is_user_active(s.user_id):
            auto_tasks.append(_activate_worker(s.account_id))
    
    # 2. Start Forwarders for accounts with enabled rules
    enabled_rules = await ForwarderRule.find(ForwarderRule.is_enabled == True).to_list()
    acc_ids = list(set([r.account_id for r in enabled_rules]))
    fwd_tasks = []
    for aid in acc_ids:
        # We need to find the user_id for this account to check if they are active
        acc = await TelegramAccount.get(aid)
        if acc and await is_user_active(str(acc.user_id)):
            fwd_tasks.append(start_forwarder_for_account(aid))
    
    # 3. Resume Member Adding Tasks
    active_member_jobs = await MemberAddJob.find(MemberAddJob.status == "running").to_list()
    for job in active_member_jobs:
        if await is_user_active(job.user_id):
            logger.info(f"[startup] Resuming MemberAddJob for user {job.user_id}")
            task = ActiveMemberAdder(
                user_id=job.user_id,
                group_link=job.group_link,
                account_configs=job.account_configs,
                min_delay=job.min_delay,
                max_delay=job.max_delay
            )
            task.job_id = str(job.id)
            MEMBER_ADDER_TASKS[job.user_id] = task
            asyncio.create_task(task.run())

    # 4. Resume Message Campaigns
    active_campaign_jobs = await MessageCampaignJob.find(MessageCampaignJob.status == "running").to_list()
    for job in active_campaign_jobs:
        if await is_user_active(job.user_id):
            logger.info(f"[startup] Resuming CampaignJob for user {job.user_id}")
            task = ActiveMessageCampaign(
                user_id=job.user_id,
                method=job.method,
                message_text=job.message_text,
                account_configs=job.account_configs,
                min_delay=job.min_delay,
                max_delay=job.max_delay,
                username_list=job.username_list
            )
            task.job_id = str(job.id)
            MESSAGE_CAMPAIGN_TASKS[job.user_id] = task
            asyncio.create_task(task.run())
    
    all_resumes = auto_tasks + fwd_tasks
    if all_resumes:
        logger.info(f"[lifespan] Resuming {len(all_resumes)} background service nodes in batches...")
        # Use more conservative batching for background resume to look organic
        await _staggered_launch(all_resumes, batch_size=3, delay_between_batches=3.0)
    else:
        logger.info("[lifespan] No active background services found to resume.")


async def _staggered_launch(coros, batch_size: int = 10, delay_between_batches: float = 1.5):
    """
    FIX: Launch coroutines in batches to avoid slamming Telegram with 500
    simultaneous connections at startup (causes flood bans + MongoDB exhaustion).
    """
    for i in range(0, len(coros), batch_size):
        batch = coros[i : i + batch_size]
        await asyncio.gather(*batch, return_exceptions=True)
        if i + batch_size < len(coros):
            await asyncio.sleep(delay_between_batches)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Imports
        from app.services.reminder.logic import start_reminder_worker
        from app.api.auto_reply import _activate_worker
        from app.services.forwarder.logic import start_forwarder_for_account
        from app.services.reaction.logic import execute_reaction_boost
        from app.services.terminal_service import terminal_manager

        # ── Database ──────────────────────────────────────────────────────────
        client = AsyncIOMotorClient(
            settings.MONGODB_URL,
            maxPoolSize=500,
            minPoolSize=10,
            serverSelectionTimeoutMS=5000,
        )
        await init_beanie(
            database=client[settings.DATABASE_NAME],
            document_models=[
                User, TelegramAccount, AutoReplyRule, AutoReplySettings,
                ForwarderRule, TelegramAPI, ReactionTask, Reminder, Proxy, SystemLog,
                MemberAddSettings, MemberAddJob, MessageCampaignJob, Plan, Payment,
                SystemSettings
            ]
        )

        # ── Start Background Tasks (System Health) ───────────────────────────
        create_task(run_system_maintenance())
        start_maintenance() # Added from client_cache
        
        # ── Start Reminder Worker ─────────────────────────────────────────────
        try:
            create_task(start_reminder_worker())
            logger.info("[startup] Reminder Worker started")
        except Exception as e:
            logger.error(f"[startup] Reminder Worker failed: {e}")

        # ── Migration & Resilience ──────────────────────────────────────────
        try:
            # 1. One-time Migration: Ensure all users have services_active field
            await User.find({"services_active": {"$exists": False}}).update({"$set": {"services_active": True}})
            
            # 2. Resume Services (Background Task)
            # We don't await this directly so we don't block the API startup
            create_task(resume_background_services())
            
            logger.info("[startup] System initialized and resume task started.")
        except Exception as e:
            logger.error(f"[startup] Post-init failure: {e}")

        yield
    except Exception as e:
        import traceback
        with open("startup_error.txt", "w") as f:
            f.write(traceback.format_exc())
        raise e
    finally:
        await shutdown_all()

app = FastAPI(
    title="Telegram CRM API",
    description="SaaS-level Telegram CRM Backend",
    version="0.1.0",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

from app.api.reactions import router as reaction_router
from app.api.reminders import router as reminder_router
from app.api.logs import router as logs_router
from app.api.system import router as system_router
from app.api.member_adder import router as member_adder_router
from app.api.message_campaign import router as message_campaign_router

app.include_router(account_router,    prefix="/api/accounts",    tags=["Accounts"])
app.include_router(auto_reply_router, prefix="/api/auto-reply",  tags=["AutoReply"])
app.include_router(forwarder_router, prefix="/api/forwarder",   tags=["Forwarder"])
app.include_router(reaction_router, prefix="/api/reactions",    tags=["Reactions"])
app.include_router(contacts_router,  prefix="/api/contacts",    tags=["Contacts"])
app.include_router(user_router,      prefix="/api/users",       tags=["Users"])
app.include_router(plans_router,     prefix="/api/plans",       tags=["Plans"])
app.include_router(reminder_router,   prefix="/api/reminders",   tags=["Reminders"])
app.include_router(proxies_router,    prefix="/api/proxies",     tags=["Proxies"])
app.include_router(logs_router,       prefix="/api/logs",        tags=["Logs"])
app.include_router(system_router,     prefix="/api/system",      tags=["System"])
app.include_router(member_adder_router, prefix="/api/member-adder", tags=["MemberAdder"])
app.include_router(message_campaign_router, prefix="/api/message-campaign", tags=["MessageCampaign"])
app.include_router(ws_router,         prefix="/api",             tags=["WebSockets"])

@app.get("/")
async def root():
    return {
        "status": "online",
        "message": "Telegram CRM API is running",
        "docs": "/docs"
    }

if __name__ == "__main__":
    import uvicorn
    import sys
    
    # Use uvloop for massive CPU performance improvements on Linux/Mac
    if sys.platform != "win32":
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass

    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=False)
