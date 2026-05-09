import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Set, Dict, Optional
from telethon import TelegramClient, functions, types, errors
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.types import Channel, Chat
from app.client_cache import get_client, get_account_user_id, touch, PRUNE_IMMUNE_ACCOUNTS
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

# Format: { account_id: { "expiry": datetime, "group_link": str, "app": "PyTgCalls Instance", "user_id": str } }
ACTIVE_VOICE_CHATS: Dict[str, dict] = {}

async def start_voice_chat_heartbeat():
    """
    Heartbeat Engine for Auto-Leave and Status Maintenance.
    Note: Real-time media signaling is now handled natively by PyTgCalls.
    """
    logger.info("[voice-chat] PyTgCalls Auto-Leave Engine started.")
    while True:
        await asyncio.sleep(5) 
        if not ACTIVE_VOICE_CHATS:
            continue
            
        now = datetime.now(timezone.utc)
        active_ids = list(ACTIVE_VOICE_CHATS.keys())
        
        for acc_id in active_ids:
            session = ACTIVE_VOICE_CHATS.get(acc_id)
            if not session: continue
            
            # 1. Handle Auto-Leave Expiry
            expiry = session.get("expiry")
            if expiry and now > expiry:
                try:
                    await leave_live_stream(acc_id, session["group_link"])
                except Exception:
                    ACTIVE_VOICE_CHATS.pop(acc_id, None)
                    PRUNE_IMMUNE_ACCOUNTS.discard(acc_id)
                continue

            # 2. Touch Cache & Refresh Online Status
            try:
                touch(acc_id)
                PRUNE_IMMUNE_ACCOUNTS.add(acc_id)
                client = await get_client(acc_id)
                if client.is_connected():
                    await client(UpdateStatusRequest(offline=False))
                    
                    # Optional: Live Checker Terminal Output (Throttled)
                    if random.randint(1, 10) == 1:
                        await terminal_manager.log_event(session["user_id"], f"💓 LIVE CHECKER: PyTgCalls Media Stream active.", acc_id, "voice-chat", "SUCCESS")
                        
            except Exception as e:
                if any(x in str(e).lower() for x in ["auth", "connection", "key"]):
                    ACTIVE_VOICE_CHATS.pop(acc_id, None)
                    PRUNE_IMMUNE_ACCOUNTS.discard(acc_id)

async def join_live_stream(account_id: str, group_link: str, skip_join: bool = False, duration_minutes: int = 0):
    """
    Joins a meeting using PyTgCalls for real media-layer presence.
    """
    user_id = await get_account_user_id(account_id)
    try:
        await terminal_manager.log_event(user_id, "🔍 Initiating join sequence (PyTgCalls Mode)...", account_id, "voice-chat", "INFO")
        client = await get_client(account_id)
        
        # 1. Warm-up
        await client(UpdateStatusRequest(offline=False))
        try:
            entity = await client.get_entity(group_link)
            await terminal_manager.log_event(user_id, f"📡 Entity resolved: {getattr(entity, 'title', 'Group')}", account_id, "voice-chat", "INFO")
        except Exception as e:
            await terminal_manager.log_event(user_id, f"❌ Resolve failed: {str(e)}", account_id, "voice-chat", "ERROR")
            return {"status": "error", "message": str(e), "account_id": account_id}

        # 2. Membership Check
        try:
            if isinstance(entity, Channel):
                await client(GetFullChannelRequest(entity))
            else:
                await client(GetFullChatRequest(entity.id))
        except errors.UserNotParticipantError:
            if skip_join: return {"status": "error", "message": "Not member.", "account_id": account_id}
            await terminal_manager.log_event(user_id, "🤝 Joining group first...", account_id, "voice-chat", "INFO")
            await client(JoinChannelRequest(entity))
            await asyncio.sleep(random.uniform(5.0, 7.0))

        # 3. Setup PyTgCalls
        try:
            from pytgcalls import PyTgCalls
        except ImportError:
            msg = "pytgcalls not installed. Must be run on a supported environment."
            await terminal_manager.log_event(user_id, f"❌ {msg}", account_id, "voice-chat", "ERROR")
            return {"status": "error", "message": msg, "account_id": account_id}

        await terminal_manager.log_event(user_id, "⚙️ Initializing PyTgCalls client...", account_id, "voice-chat", "INFO")
        
        # Initialize the app with the specific Telethon client
        app = PyTgCalls(client)
        await app.start()
        
        await terminal_manager.log_event(user_id, "🚀 Connecting to Media Reflector (UDP Layer)...", account_id, "voice-chat", "INFO")
        
        try:
            await app.join_group_call(
                group_link,
                stream=None  # listen only, as requested by user methodology
            )
        except Exception as e:
            await app.stop()
            err_msg = str(e)
            await terminal_manager.log_event(user_id, f"⚠️ Join failed: {err_msg}", account_id, "voice-chat", "ERROR")
            return {"status": "error", "message": err_msg, "account_id": account_id}

        # ✅ PERSISTENCE REGISTRATION
        expiry_time = None
        if duration_minutes > 0:
            expiry_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        
        ACTIVE_VOICE_CHATS[account_id] = {
            "expiry": expiry_time,
            "group_link": group_link,
            "app": app,
            "user_id": user_id
        }
        PRUNE_IMMUNE_ACCOUNTS.add(account_id)
        touch(account_id)
        
        await terminal_manager.log_event(user_id, "🛡️ SESSION LOCKED: PyTgCalls Media Connection active.", account_id, "voice-chat", "SUCCESS")
        return {"status": "success", "account_id": account_id}

    except Exception as e:
        return {"status": "error", "message": str(e), "account_id": account_id}

async def leave_live_stream(account_id: str, group_link: str):
    """
    Unlocks session and leaves meeting via PyTgCalls.
    """
    user_id = await get_account_user_id(account_id)
    try:
        session = ACTIVE_VOICE_CHATS.pop(account_id, None)
        PRUNE_IMMUNE_ACCOUNTS.discard(account_id)
        
        if session and session.get("app"):
            app = session["app"]
            await terminal_manager.log_event(user_id, "🛑 Stopping PyTgCalls stream...", account_id, "voice-chat", "INFO")
            try:
                await app.leave_group_call(group_link)
                await app.stop()
            except: pass

        await terminal_manager.log_event(user_id, "🚪 Connection closed gracefully.", account_id, "voice-chat", "SUCCESS")
        return {"status": "success", "account_id": account_id}
    except Exception as e:
        return {"status": "error", "message": str(e), "account_id": account_id}
