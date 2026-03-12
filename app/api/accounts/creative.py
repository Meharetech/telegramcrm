from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sse_starlette.sse import EventSourceResponse
from telethon import TelegramClient, functions, types, errors as rpcerrorlist
from telethon.sessions import StringSession
import asyncio
import json
import random
import os
import shutil
import uuid
from typing import List

from app.api.auth_utils import get_current_user
from app.models import TelegramAccount
from app.client_cache import get_client

router = APIRouter(prefix="/creative", tags=["Creative"])
UPLOAD_DIR = "uploads/creative"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@router.post("/upload-logo")
async def upload_logo(file: UploadFile = File(...), token: str = None):
    try:
        user = await get_current_user(token)
        # Isolation: User-specific subfolders
        user_dir = os.path.join(UPLOAD_DIR, str(user.id))
        os.makedirs(user_dir, exist_ok=True)
        
        ext = os.path.splitext(file.filename)[1]
        filename = f"logo_{uuid.uuid4()}{ext}"
        file_path = os.path.join(user_dir, filename)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        return {"file_path": os.path.abspath(file_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stream")
async def create_creative_stream(
    account_ids: str, # Comma separated
    creation_type: str = Query(..., alias="type"),
    count: int = 1,
    name_mode: str = "series",
    name_prefix: str = "",
    name_list_json: str = "[]",
    about: str = "",
    min_delay: int = 3,
    max_delay: int = 8,
    add_members: bool = True,
    send_messages: bool = True,
    logo_path: str = "",
    token: str = None
):
    try:
        user = await get_current_user(token)
        if count > 10:
             async def limit_error():
                yield {"event": "error", "data": json.dumps({"message": "🚫 Max 10 creations per account allowed for safety."})}
             return EventSourceResponse(limit_error())
             
        ids = [i.strip() for i in account_ids.split(",") if i.strip()]
        name_list = json.loads(name_list_json)
    except Exception as e:
        async def error_gen():
            yield {"event": "error", "data": json.dumps({"message": f"Auth/Param Error: {str(e)}"})}
        return EventSourceResponse(error_gen())

    async def log_generator():
        try:
            # ── Step 0: User Service Guard ───────────────────────────────────
            from app.models.user import User
            user_obj = await User.get(str(user.id))
            if not user_obj or not user_obj.services_active:
                yield {"event": "error", "data": json.dumps({"message": "🛑 Task Aborted: User services are currently STOPPED."})}
                return

            total_created = 0
            
            # ── OPTIMIZED: Batch Fetch & Safety Audit ──────────────────────
            from bson import ObjectId
            acc_ids_list = [ObjectId(aid.strip()) for aid in account_ids.split(",") if aid.strip()]
            all_accounts = await TelegramAccount.find({"_id": {"$in": acc_ids_list}}).to_list()
            
            # Audit safety
            ready_accounts = []
            from datetime import datetime
            now_utc = datetime.utcnow().replace(tzinfo=None) # naive comparison
            
            for acc in all_accounts:
                # 1. Check if Banned
                if not acc.is_active or acc.status in ["banned", "error"]:
                    yield {"event": "warning", "data": json.dumps({"message": f"⚠️ Skipping {acc.phone_number}: Account is Banned or Inactive."})}
                    continue
                
                # 2. Check for FloodWait
                if acc.flood_wait_until:
                    fw_naive = acc.flood_wait_until.replace(tzinfo=None)
                    if fw_naive > now_utc:
                        wait = int((fw_naive - now_utc).total_seconds())
                        yield {"event": "warning", "data": json.dumps({"message": f"⏳ Skipping {acc.phone_number}: Active FloodWait ({wait}s)."})}
                        continue
                
                ready_accounts.append(acc)

            if not ready_accounts:
                yield {"event": "error", "data": json.dumps({"message": "❌ No ready accounts found (banned/flood). Task terminated."})}
                return

            yield {"event": "info", "data": json.dumps({"message": f"💎 Audit complete. {len(ready_accounts)} accounts are active and ready."})}

            for account in ready_accounts:
                yield {"event": "info", "data": json.dumps({"message": f"🚀 Switching to account: {account.phone_number}"})}
                
                try:
                    client = await get_client(
                        str(account.id),
                        account.session_string,
                        account.api_id,
                        account.api_hash
                    )
                    if not await client.is_user_authorized():
                        yield {"event": "error", "data": json.dumps({"message": f"Account {account.phone_number} unauthorized."})}
                        continue

                    for i in range(count):
                        # Determine name
                        if name_mode == "series":
                            title = f"{name_prefix} #{total_created + 1}"
                        else:
                            if total_created < len(name_list):
                                title = name_list[total_created]
                            else:
                                title = f"{name_prefix} Task-{total_created+1}"

                        yield {"event": "info", "data": json.dumps({"message": f"[{account.phone_number}] Creating {creation_type}: {title}..."})}

                        try:
                            if not title or title.strip() == "":
                                yield {"event": "warning", "data": json.dumps({"message": f"⚠️ Empty name detected, skipping this item."})}
                                total_created += 1
                                continue

                            if creation_type == "group":
                                # Create Supergroup
                                result = await client(functions.channels.CreateChannelRequest(
                                    title=title,
                                    about=about,
                                    megagroup=True
                                ))
                                channel = result.chats[0]
                                
                                # Set Permissions
                                try:
                                    await client(functions.messages.EditChatDefaultBannedRightsRequest(
                                        peer=channel,
                                        banned_rights=types.ChatBannedRights(
                                            until_date=None,
                                            view_messages=False,
                                            send_messages=not send_messages,
                                            send_media=not send_messages,
                                            send_stickers=not send_messages,
                                            send_gifs=not send_messages,
                                            send_games=not send_messages,
                                            send_inline=not send_messages,
                                            embed_links=not send_messages,
                                            send_polls=not send_messages,
                                            change_info=True,
                                            invite_users=not add_members,
                                            pin_messages=True
                                        )
                                    ))
                                except Exception as perm_err:
                                    yield {"event": "warning", "data": json.dumps({"message": f"⚠️ Could not set permissions: {str(perm_err)}"})}
                            else:
                                # Create Broadcast Channel
                                result = await client(functions.channels.CreateChannelRequest(
                                    title=title,
                                    about=about,
                                    broadcast=True
                                ))
                                channel = result.chats[0]

                            # Apply Logo if provided
                            if logo_path and os.path.exists(logo_path):
                                try:
                                    # Upload the file first
                                    uploaded_logo = await client.upload_file(logo_path)
                                    # Then set it as the channel/group photo
                                    await client(functions.channels.EditPhotoRequest(
                                        channel=channel,
                                        photo=types.InputChatUploadedPhoto(file=uploaded_logo)
                                    ))
                                    yield {"event": "info", "data": json.dumps({"message": f"🖼️ Logo applied to {title}!"})}
                                except Exception as photo_err:
                                    yield {"event": "warning", "data": json.dumps({"message": f"⚠️ Could not set logo: {str(photo_err)}"})}

                            total_created += 1
                            yield {"event": "success", "data": json.dumps({"message": f"✅ {title} created and configured!"})}

                            # Random Delay
                            if total_created < (count * len(ids)):
                                delay = random.randint(min_delay, max_delay)
                                yield {"event": "info", "data": json.dumps({"message": f"Waiting {delay}s for anti-ban..."})}
                                await asyncio.sleep(delay)

                        except Exception as e:
                            err_name = type(e).__name__
                            
                            if err_name == 'FloodWaitError':
                                yield {"event": "error", "data": json.dumps({"message": f"⚠️ Flood Wait: {getattr(e, 'seconds', 60)}s. Stopping this account for now."})}
                                break # Skip this account entirely
                                
                            elif err_name == 'ChannelsAdminPublicTooManyError':
                                yield {"event": "error", "data": json.dumps({"message": f"🚨 Account has too many public channels. Stopping account."})}
                                break
                                
                            elif err_name == 'UserBannedInChannelError':
                                yield {"event": "error", "data": json.dumps({"message": f"🚫 Account is restricted/banned from creating {creation_type}s."})}
                                break
                                
                            elif err_name == 'PeerFloodError':
                                yield {"event": "warning", "data": json.dumps({"message": f"⚠️ Peer Flood (Soft Ban). Skipping current task."})}
                                await asyncio.sleep(60) # Longer wait on flood
                                continue

                            err_msg = str(e)
                            if "RPCError" in err_msg or "telethon.errors" in str(type(e)):
                                yield {"event": "warning", "data": json.dumps({"message": f"❌ Telegram Error: {err_msg}. Skipping this one."})}
                            else:
                                yield {"event": "warning", "data": json.dumps({"message": f"❌ System Error: {err_msg}. Skipping."})}
                            total_created += 1 # Advance count anyway to avoid infinite retry on same name
                            continue

                except Exception as e:
                    yield {"event": "error", "data": json.dumps({"message": f"Connection lost for {account.phone_number}: {str(e)}"})}
                # client.disconnect() removed - cache handles it

            # Cleanup Logo after all accounts are done
            if logo_path and os.path.exists(logo_path):
                try:
                    os.remove(logo_path)
                    # logger.info(f"Cleaned up logo: {logo_path}")
                except:
                    pass

            yield {"event": "done", "data": "finished"}

        except Exception as e:
            # Final cleanup in case of crash
            if logo_path and os.path.exists(logo_path):
                try: os.remove(logo_path)
                except: pass
            yield {"event": "error", "data": json.dumps({"message": f"Fatal Engine Error: {str(e)}"})}

    return EventSourceResponse(log_generator())
