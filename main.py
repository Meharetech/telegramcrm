import asyncio

from datetime import datetime, timezone, timedelta
from asyncio import create_task
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from app.models.message_campaign import MessageCampaignJob, MessageCampaignSchedule
from app.models import (
    User, TelegramAccount, ForwarderRule, TelegramAPI, Reminder,
    Proxy, SystemLog, ReactionTask, MemberAddSettings, MemberAddJob,
    MemberAddSchedule, Plan, Payment, SystemSettings, BotForwarder,
    WalletTransaction, ShopPurchase, AiAgent, AiKnowledgeSummary,
    AiReplyLog, AiSettings
)
from app.models.auto_reply import AutoReplyRule, AutoReplySettings
from app.models.folder_campaign import FolderCampaignJob
from app.models.group_join import GroupJoinJob
from app.api.accounts import router as account_router
from app.api.auto_reply import router as auto_reply_router
from app.api.forwarder import router as forwarder_router
from app.api.ws import router as ws_router
from app.api.users import router as user_router
from app.api.contacts import router as contacts_router
from app.api.proxies import router as proxies_router
from app.api.plans import router as plans_router
from app.api.bot_forwarder import router as bot_forwarder_router
from app.api.wallet import router as wallet_router
from app.api.shop import router as shop_router
from app.api.accounts.otp_viewer import router as otp_viewer_router
from app.api.folder_campaign import router as folder_campaign_router
from app.api.ai_agent import router as ai_agent_router
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
else:
    # ── WINDOWS SPECIFIC PATCH ──
    # This silences the "ConnectionResetError: [WinError 10054]" traceback 
    # that happens in asyncio/proactor_events.py when a client closes a connection.
    from asyncio import proactor_events
    _old_call_connection_lost = proactor_events._ProactorBasePipeTransport._call_connection_lost

    def _patched_call_connection_lost(self, exc):
        try:
            _old_call_connection_lost(self, exc)
        except (ConnectionResetError, BrokenPipeError):
            # Suppress noisy reset errors on Windows
            pass

    proactor_events._ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost
    logging.getLogger(__name__).info("[patch] Applied Windows asyncio/proactor log fix.")

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
            
            # 3. RAM: Clear internal pools if necessary (placeholder or remove)
            pass
            
            logger.info(f"[system] Maintenance complete. GC collected {collected} objects.")
        except asyncio.CancelledError:
            # Handle graceful shutdown without throwing traceback
            break
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
                max_delay=job.max_delay,
                batch_size=job.batch_size or 2
            )
            task.job_id = str(job.id)
            task.source_type = job.source_type or "contacts"
            task.member_list = job.member_list or []
            
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

    # 5. Resume Folder Campaigns
    from app.models.folder_campaign import FolderCampaignJob
    from app.services.folder_campaign import ActiveFolderCampaign, FOLDER_CAMPAIGN_TASKS
    active_folder_jobs = await FolderCampaignJob.find(FolderCampaignJob.status == "running").to_list()
    for job in active_folder_jobs:
        if await is_user_active(job.user_id):
            logger.info(f"[startup] Resuming FolderCampaignJob for user {job.user_id} account {job.account_id}")
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
                repeat_interval=job.repeat_interval
            )
            task.job_id = str(job.id)
            if job.user_id not in FOLDER_CAMPAIGN_TASKS:
                FOLDER_CAMPAIGN_TASKS[job.user_id] = {}
            FOLDER_CAMPAIGN_TASKS[job.user_id][job.account_id] = task
            asyncio.create_task(task.run())
    
    all_resumes = auto_tasks + fwd_tasks
    if all_resumes:
        logger.info(f"[lifespan] Resuming {len(all_resumes)} background service nodes in batches...")
        # Use more conservative batching for background resume to look organic
        await _staggered_launch(all_resumes, batch_size=3, delay_between_batches=3.0)
    else:
        logger.info("[lifespan] No active background services found to resume.")

    # 5. Initialize Bot API Forwarders
    from app.services.bot_forwarder.bot_service import init_bots_on_startup
    create_task(init_bots_on_startup())


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
            maxPoolSize=200, # Optimized from 500 for better stability under high load
            minPoolSize=10,
            serverSelectionTimeoutMS=5000,
        )
        await init_beanie(
            database=client[settings.DATABASE_NAME],
            document_models=[
                User, TelegramAccount, AutoReplyRule, AutoReplySettings,
                ForwarderRule, TelegramAPI, ReactionTask, Reminder, Proxy, SystemLog,
                MemberAddSettings, MemberAddJob, MemberAddSchedule, MessageCampaignJob, MessageCampaignSchedule, Plan, Payment,
                SystemSettings, BotForwarder, WalletTransaction, ShopPurchase, FolderCampaignJob, GroupJoinJob,
                AiAgent, AiKnowledgeSummary, AiReplyLog, AiSettings
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

        # ── Start Member Adder Scheduler ─────────────────────────────────────
        try:
            from app.services.member_adder_scheduler import start_member_adder_scheduler
            create_task(start_member_adder_scheduler())
            logger.info("[startup] Member Adder Scheduler started")
        except Exception as e:
            logger.error(f"[startup] Member Adder Scheduler failed: {e}")

        # ── Start Message Campaign Scheduler ───────────────────────────────
        try:
            from app.services.message_campaign_scheduler import start_message_campaign_scheduler
            create_task(start_message_campaign_scheduler())
            logger.info("[startup] Message Campaign Scheduler started")
        except Exception as e:
            logger.error(f"[startup] Message Campaign Scheduler failed: {e}")
        # ── Migration & Resilience (COLD START MODE) ──────────────────────────
        try:
            # 1. Global Reset: Set all users to 'STOPPED' on server boot for security.
            # This prevents "Ghost Sessions" and "Two IP Address" login storms.
            await User.find_all().update({"$set": {"services_active": False}})
            
            # 2. DISABLED: resume_background_services()
            # Everything will now remain OFF until the user manually launches the Terminal.
            # create_task(resume_background_services())
            
            logger.info("[startup] COLD START: All user services RESET and locked to STOPPED state.")
        except Exception as e:
            logger.error(f"[startup] Startup migration failure: {e}")

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
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

from app.api.reactions import router as reaction_router
from app.api.reminders import router as reminder_router
from app.api.logs import router as logs_router
from app.api.system import router as system_router
from app.api.member_adder import router as member_adder_router
from app.api.member_add_schedule import router as member_add_schedule_router
from app.api.message_campaign import router as message_campaign_router
from app.api.group_join import router as group_join_router

app.include_router(account_router,    prefix="/api/accounts",    tags=["Accounts"])
app.include_router(auto_reply_router, prefix="/api/auto-reply",  tags=["AutoReply"])
app.include_router(forwarder_router, prefix="/api/forwarder",   tags=["Forwarder"])
app.include_router(reaction_router, prefix="/api/reactions",    tags=["Reactions"])
app.include_router(contacts_router,  prefix="/api/contacts",    tags=["Contacts"])
app.include_router(user_router,      prefix="/api/users",       tags=["Users"])
app.include_router(plans_router,     prefix="/api/plans",       tags=["Plans"])
app.include_router(bot_forwarder_router, prefix="/api/bot-forwarder", tags=["Bot Forwarder"])
app.include_router(reminder_router,   prefix="/api/reminders",   tags=["Reminders"])
app.include_router(proxies_router,    prefix="/api/proxies",     tags=["Proxies"])
app.include_router(logs_router,       prefix="/api/logs",        tags=["Logs"])
app.include_router(system_router,     prefix="/api/system",      tags=["System"])
app.include_router(member_adder_router, prefix="/api/member-adder", tags=["MemberAdder"])
app.include_router(member_add_schedule_router, prefix="/api/member-add-schedule", tags=["MemberAddSchedule"])
app.include_router(message_campaign_router, prefix="/api/message-campaign", tags=["MessageCampaign"])
app.include_router(wallet_router, prefix="/api/wallet", tags=["Wallet"])
app.include_router(shop_router, prefix="/api/shop", tags=["Shop"])
app.include_router(otp_viewer_router, prefix="/api/otp", tags=["OTP Viewer"])
app.include_router(folder_campaign_router, prefix="/api/folder-campaign", tags=["Folder Campaign"])
app.include_router(group_join_router, prefix="/api/group-join", tags=["Group Joiner"])
app.include_router(ai_agent_router, prefix="/api/ai-agent", tags=["AI Agent"])
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
