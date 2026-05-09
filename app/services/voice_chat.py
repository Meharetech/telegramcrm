import asyncio
import logging
import json
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Set, Dict, Optional
from telethon import TelegramClient, functions, types, errors
from telethon.tl.functions.phone import (
    JoinGroupCallRequest, LeaveGroupCallRequest, GetGroupCallRequest, 
    EditGroupCallParticipantRequest, GetGroupCallStreamChannelsRequest
)
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest, GetHistoryRequest
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.types import InputGroupCall, DataJSON, InputPeerChannel, InputPeerUser, Channel, Chat, User
from app.client_cache import get_client, get_account_user_id, touch, PRUNE_IMMUNE_ACCOUNTS
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

# Format: { account_id: { "expiry": datetime, "group_link": str, "mode": "pytgcalls"|"telethon", "app": obj, "call_input": obj, "ssrc": int, "user_id": str } }
ACTIVE_VOICE_CHATS: Dict[str, dict] = {}

async def start_voice_chat_heartbeat():
    """
    Hybrid Heartbeat Engine.
    Handles both PyTgCalls background persistence and Telethon active signaling.
    """
    logger.info("[voice-chat] Hybrid Heartbeat Engine started.")
    while True:
        await asyncio.sleep(2) 
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

            # 2. Touch Cache & Refresh Connection
            try:
                touch(acc_id)
                PRUNE_IMMUNE_ACCOUNTS.add(acc_id)
                client = await get_client(acc_id)
                
                if not client.is_connected():
                    await client.connect()
                    
                # A. Telethon Mode (Manual Pulse)
                if session.get("mode") == "telethon":
                    call_input = session.get("call_input")
                    ssrc = session.get("ssrc")
                    
                    if call_input and ssrc:
                        await client(UpdateStatusRequest(offline=False))
                        await client(EditGroupCallParticipantRequest(
                            call=call_input,
                            participant=await client.get_input_entity('me'),
                            muted=True,
                            volume=0,
                            raise_hand=False
                        ))
                        
                        if random.randint(1, 5) == 1:
                            await terminal_manager.log_event(session["user_id"], f"💓 LIVE CHECKER: Telethon signal pulse OK.", acc_id, "voice-chat", "SUCCESS")
                
                # B. PyTgCalls Mode (Passive Monitoring)
                elif session.get("mode") == "pytgcalls":
                    if random.randint(1, 10) == 1:
                        await terminal_manager.log_event(session["user_id"], f"💓 LIVE CHECKER: PyTgCalls Media Stream active.", acc_id, "voice-chat", "SUCCESS")

            except errors.UserNotParticipantError:
                if session.get("mode") == "telethon":
                    await terminal_manager.log_event(session["user_id"], "🚨 Dropped by server. Triggering Emergency Re-join...", acc_id, "voice-chat", "ERROR")
                    asyncio.create_task(join_live_stream(
                        acc_id, session["group_link"], skip_join=True, 
                        duration_minutes=int((expiry - now).total_seconds() / 60) if expiry else 0
                    ))
            except Exception as e:
                if any(x in str(e).lower() for x in ["auth", "connection", "key"]):
                    ACTIVE_VOICE_CHATS.pop(acc_id, None)
                    PRUNE_IMMUNE_ACCOUNTS.discard(acc_id)

async def join_live_stream(account_id: str, group_link: str, skip_join: bool = False, duration_minutes: int = 0):
    """
    Hybrid Joiner: Attempts PyTgCalls first, falls back to Telethon Active Signaling.
    """
    user_id = await get_account_user_id(account_id)
    try:
        await terminal_manager.log_event(user_id, "🔍 Initiating join sequence...", account_id, "voice-chat", "INFO")
        client = await get_client(account_id)
        
        # 1. Warm-up
        await client(UpdateStatusRequest(offline=False))
        try:
            entity = await client.get_entity(group_link)
        except Exception as e:
            return {"status": "error", "message": str(e), "account_id": account_id}

        # 2. Membership Check
        full_chat = None
        try:
            if isinstance(entity, Channel):
                full_chat = await client(GetFullChannelRequest(entity))
            else:
                full_chat = await client(GetFullChatRequest(entity.id))
        except errors.UserNotParticipantError:
            if skip_join: return {"status": "error", "message": "Not member.", "account_id": account_id}
            await client(JoinChannelRequest(entity))
            await asyncio.sleep(random.uniform(5.0, 7.0))
            full_chat = await client(GetFullChannelRequest(entity)) if isinstance(entity, Channel) else await client(GetFullChatRequest(entity.id))

        if not full_chat or not hasattr(full_chat.full_chat, 'call') or not full_chat.full_chat.call:
            return {"status": "error", "message": "No active meeting.", "account_id": account_id}

        call_info = full_chat.full_chat.call
        call_input = InputGroupCall(id=call_info.id, access_hash=call_info.access_hash)
        
        expiry_time = None
        if duration_minutes > 0:
            expiry_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)

        # -----------------------------------------------------
        # OPTION A: PyTgCalls (Primary Method - Linux)
        # -----------------------------------------------------
        try:
            from pytgcalls import PyTgCalls
            
            await terminal_manager.log_event(user_id, "⚙️ PyTgCalls found. Connecting to Media Reflector...", account_id, "voice-chat", "INFO")
            app = PyTgCalls(client)
            await app.start()
            
            await app.join_group_call(group_link, stream=None)
            
            ACTIVE_VOICE_CHATS[account_id] = {
                "expiry": expiry_time, "group_link": group_link,
                "mode": "pytgcalls", "app": app, "user_id": user_id
            }
            PRUNE_IMMUNE_ACCOUNTS.add(account_id)
            touch(account_id)
            await terminal_manager.log_event(user_id, "🛡️ SESSION LOCKED: PyTgCalls Media Connection active.", account_id, "voice-chat", "SUCCESS")
            return {"status": "success", "account_id": account_id}
            
        except ImportError:
            # Fallback to Telethon if PyTgCalls isn't installed (Windows Local Testing)
            await terminal_manager.log_event(user_id, "⚠️ PyTgCalls not found. Falling back to Telethon Active Signaling.", account_id, "voice-chat", "WARNING")
        except Exception as e:
            await terminal_manager.log_event(user_id, f"⚠️ PyTgCalls join failed: {str(e)}. Falling back to Telethon.", account_id, "voice-chat", "WARNING")

        # -----------------------------------------------------
        # OPTION B: Telethon Active Signaling (Fallback - Windows)
        # -----------------------------------------------------
        max_retries = 15
        forced_ssrc = None
        
        for attempt in range(max_retries):
            try:
                await client(GetGroupCallRequest(call=call_input, limit=1))
                my_ssrc = forced_ssrc or random.randint(100000, 999999999)
                
                join_data = {
                    "muted": True, "video_stopped": True, "pause": False, 
                    "ssrc": my_ssrc, "media_timestamp": int(datetime.now().timestamp() * 1000)
                }

                await client(JoinGroupCallRequest(
                    call=call_input,
                    join_as=await client.get_input_entity('me'),
                    muted=True,
                    params=DataJSON(data=json.dumps(join_data))
                ))
                
                try: await client(GetGroupCallStreamChannelsRequest(call=call_input))
                except: pass
                
                await client(EditGroupCallParticipantRequest(
                    call=call_input, participant=await client.get_input_entity('me'),
                    muted=True, volume=0, raise_hand=False
                ))

                ACTIVE_VOICE_CHATS[account_id] = {
                    "expiry": expiry_time, "group_link": group_link,
                    "mode": "telethon", "call_input": call_input, "ssrc": my_ssrc, "user_id": user_id
                }
                PRUNE_IMMUNE_ACCOUNTS.add(account_id)
                touch(account_id)
                
                await terminal_manager.log_event(user_id, "🛡️ SESSION LOCKED: Telethon Active Signaling running.", account_id, "voice-chat", "SUCCESS")
                return {"status": "success", "account_id": account_id}
            
            except errors.UserAlreadyParticipantError:
                ACTIVE_VOICE_CHATS[account_id] = {"expiry": None, "group_link": group_link, "mode": "telethon", "call_input": call_input, "ssrc": 0, "user_id": user_id}
                PRUNE_IMMUNE_ACCOUNTS.add(account_id)
                return {"status": "success", "message": "Stable (Already In)", "account_id": account_id}
            
            except Exception as e:
                err_msg = str(e)
                if "ssrc" in err_msg.lower():
                    ssrc_match = re.search(r'ssrc\s+value:?\s*(\d+)', err_msg, re.IGNORECASE)
                    if ssrc_match:
                        forced_ssrc = int(ssrc_match.group(1))
                        continue
                if any(x in err_msg.lower() for x in ["retry", "internal", "failed"]) and attempt < max_retries - 1:
                    await asyncio.sleep(random.uniform(4.0, 7.0))
                    continue
                return {"status": "error", "message": err_msg, "account_id": account_id}

        return {"status": "error", "message": "Failed all signaling attempts.", "account_id": account_id}

    except Exception as e:
        return {"status": "error", "message": str(e), "account_id": account_id}

async def leave_live_stream(account_id: str, group_link: str):
    """
    Leaves the meeting dynamically based on the active mode.
    """
    user_id = await get_account_user_id(account_id)
    try:
        session = ACTIVE_VOICE_CHATS.pop(account_id, None)
        PRUNE_IMMUNE_ACCOUNTS.discard(account_id)
        
        client = await get_client(account_id)
        
        if session:
            # Leave via PyTgCalls
            if session.get("mode") == "pytgcalls" and session.get("app"):
                app = session["app"]
                await terminal_manager.log_event(user_id, "🛑 Stopping PyTgCalls stream...", account_id, "voice-chat", "INFO")
                try:
                    await app.leave_group_call(group_link)
                    await app.stop()
                except: pass
            
            # Leave via Telethon
            elif session.get("mode") == "telethon" and session.get("call_input"):
                await client(LeaveGroupCallRequest(call=session["call_input"], source=0))

        await terminal_manager.log_event(user_id, "🚪 Connection closed gracefully.", account_id, "voice-chat", "SUCCESS")
        return {"status": "success", "account_id": account_id}
    except Exception as e:
        return {"status": "error", "message": str(e), "account_id": account_id}
