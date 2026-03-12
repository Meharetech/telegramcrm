import io
import os
import logging
import asyncio
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import StreamingResponse
from app.models import TelegramAccount, Reminder
from app.api.accounts.utils import format_status
from app.client_cache import get_client
from telethon import functions, types
from datetime import datetime

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/chats/{account_id}")
async def get_account_chats(account_id: str, limit: int = 50, offset_date: Optional[str] = None):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))

    try:
        parsed_offset_date = None
        if offset_date:
            try:
                parsed_offset_date = datetime.fromisoformat(offset_date)
            except ValueError:
                pass

        dialogs = await client.get_dialogs(limit=limit, offset_date=parsed_offset_date)
        
        # Fetch pending reminder counts for this account
        reminders = await Reminder.find(
            Reminder.telegram_account_id == account_id,
            Reminder.status == "pending"
        ).to_list()
        reminder_counts = {}
        for r in reminders:
            reminder_counts[str(r.chat_id)] = reminder_counts.get(str(r.chat_id), 0) + 1

        chats = []

        for d in dialogs:
            entity = d.entity
            is_broadcast = getattr(entity, 'broadcast', False)
            is_megagroup  = getattr(entity, 'megagroup',  False)

            if is_broadcast and not is_megagroup:
                chat_type = "channel"
            elif d.is_group or is_megagroup:
                chat_type = "group"
            else:
                chat_type = "user"

            status = ""
            is_online = False
            if chat_type == "user" and hasattr(entity, 'status'):
                status = format_status(entity.status)
            
            # Formatting status & online info
            status_text = ""
            is_online = False
            if chat_type == "user" and hasattr(entity, 'status'):
                status_text = format_status(entity.status)
                is_online = isinstance(entity.status, types.UserStatusOnline)

            # Metadata properties
            can_send_messages = True
            if chat_type in ["group", "channel"]:
                if is_megagroup:
                    is_admin = getattr(entity, 'creator', False) or (getattr(entity, 'admin_rights', None) is not None)
                    if not is_admin:
                        banned_rights = getattr(entity, 'default_banned_rights', None)
                        if banned_rights and banned_rights.send_messages:
                            can_send_messages = False
            
            p_count = 0
            if hasattr(entity, 'participants_count'):
                p_count = entity.participants_count

            last_msg_text = ""
            last_msg_date = None
            if d.message:
                raw = d.message.message or ""
                last_msg_text = (raw[:65] + '…') if len(raw) > 65 else raw
                last_msg_date = d.message.date.isoformat() if d.message.date else None

            chats.append({
                "id": str(d.id),
                "name": d.name or "Unknown",
                "chat_type": chat_type,
                "unread_count": d.unread_count,
                "last_message": last_msg_text,
                "last_message_date": last_msg_date,
                "is_online": is_online,
                "status_text": status_text,
                "participants_count": p_count,
                "can_send_messages": can_send_messages,
                "reminder_count": reminder_counts.get(str(d.id), 0)
            })

        return chats
    except Exception as e:
        logging.error(f"Error fetching chats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/unread-counts/{account_id}")
async def get_unread_counts(account_id: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        from app.client_cache import get_client, invalidate
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))

        if not await client.is_user_authorized():
            await invalidate(account_id)
            raise HTTPException(status_code=401, detail="Unauthorized")

        dialogs = await client.get_dialogs(limit=50)
        
        # Fetch pending reminder counts
        reminders = await Reminder.find(
            Reminder.telegram_account_id == account_id,
            Reminder.status == "pending"
        ).to_list()
        reminder_counts = {}
        for r in reminders:
            reminder_counts[str(r.chat_id)] = reminder_counts.get(str(r.chat_id), 0) + 1

        user_ids = [d.id for d in dialogs if not (d.is_group or d.is_channel)]
        fresh_users = {}
        if user_ids:
            try:
                users = await client.get_entity(user_ids)
                if not isinstance(users, list): users = [users]
                fresh_users = {u.id: u for u in users}
            except: pass

        result = []
        for d in dialogs:
            entity = fresh_users.get(d.id, d.entity)
            chat_type = "user"
            if getattr(entity, 'broadcast', False): chat_type = "channel"
            elif d.is_group or getattr(entity, 'megagroup', False): chat_type = "group"

            status = ""
            is_online = False
            if chat_type == "user" and hasattr(entity, 'status'):
                status = format_status(entity.status)
                is_online = status == "online"
            elif chat_type == "group":
                p_count = getattr(entity, 'participants_count', 0)
                if p_count:
                    status = f"{p_count} members"
            elif chat_type == "channel":
                p_count = getattr(entity, 'participants_count', 0)
                if p_count:
                    status = f"{p_count} subscribers"

            last_msg_text = ""
            last_msg_date = None
            if d.message:
                raw = getattr(d.message, 'message', '') or ''
                if not raw and hasattr(d.message, 'media') and d.message.media:
                    raw = "📎 Media"
                last_msg_text = (raw[:65] + '…') if len(raw) > 65 else raw
                if d.message.date:
                    last_msg_date = d.message.date.isoformat()

            result.append({
                "id":                str(d.id),
                "unread_count":      d.unread_count,
                "last_message":      last_msg_text,
                "last_message_date": last_msg_date,
                "status":            status,
                "is_online":         is_online,
                "is_member":         True,
                "reminder_count":    reminder_counts.get(str(d.id), 0)
            })

        return result
    except Exception as e:
        logging.error(f"Error fetching unread counts: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/search/{account_id}")
async def search_telegram(account_id: str, q: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        if not q or len(q) < 3:
            return []
            
        result = await client(functions.contacts.SearchRequest(q=q, limit=20))
        
        reminders = await Reminder.find(
            Reminder.telegram_account_id == account_id,
            Reminder.status == "pending"
        ).to_list()
        reminder_counts = {}
        for r in reminders:
            reminder_counts[str(r.chat_id)] = reminder_counts.get(str(r.chat_id), 0) + 1

        found = []
        for user in result.users:
            if getattr(user, 'bot', False): continue 
            found.append({
                "id": str(user.id),
                "name": (getattr(user, 'first_name', '') or '') + (' ' + getattr(user, 'last_name', '') if getattr(user, 'last_name', None) else ''),
                "chat_type": "user",
                "username": getattr(user, 'username', None),
                "is_online": isinstance(getattr(user, 'status', None), types.UserStatusOnline),
                "status": format_status(getattr(user, 'status', None)),
                "reminder_count": reminder_counts.get(str(user.id), 0)
            })
            
        for chat in result.chats:
            is_broadcast = getattr(chat, 'broadcast', False)
            is_megagroup = getattr(chat, 'megagroup', False)
            chat_type = "group"
            if is_broadcast and not is_megagroup:
                chat_type = "channel"
            
            # Check if member
            is_member = not getattr(chat, 'left', False)
                
            found.append({
                "id": str(chat.id),
                "name": getattr(chat, 'title', 'Unknown'),
                "chat_type": chat_type,
                "username": getattr(chat, 'username', None),
                "unread_count": 0,
                "last_message": "Global Search Result",
                "is_member": is_member,
                "reminder_count": reminder_counts.get(str(chat.id), 0)
            })
            
        return found
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/chat-info/{account_id}/{chat_id}")
async def get_chat_info(account_id: str, chat_id: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        target_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        entity = await client.get_entity(target_id)
        
        participants_count = 0
        about = ""
        invite_link = ""

        if isinstance(entity, types.Channel):
            full = await client(functions.channels.GetFullChannelRequest(channel=entity))
            participants_count = getattr(full.full_chat, 'participants_count', 0)
            about = getattr(full.full_chat, 'about', '')
            if full.full_chat.exported_invite:
                invite_link = getattr(full.full_chat.exported_invite, 'link', '')
        elif isinstance(entity, types.Chat) or (isinstance(entity, types.Channel) and not entity.broadcast):
            if isinstance(entity, types.Chat):
                full = await client(functions.messages.GetFullChatRequest(chat_id=entity.id))
            else:
                full = await client(functions.channels.GetFullChannelRequest(channel=entity))
            
            full_chat = full.full_chat
            about = getattr(full_chat, 'about', '')
            if hasattr(full_chat, 'participants_count'):
                participants_count = full_chat.participants_count
            elif hasattr(full_chat, 'participants') and hasattr(full_chat.participants, 'count'):
                participants_count = full_chat.participants.count
            if full_chat.exported_invite:
                invite_link = getattr(full_chat.exported_invite, 'link', '')

        is_member = True
        if isinstance(entity, (types.Channel, types.Chat)):
            is_member = not getattr(entity, 'left', False)

        return {
            "id": str(entity.id),
            "participants_count": participants_count,
            "about": about,
            "invite_link": invite_link,
            "username": getattr(entity, 'username', None),
            "is_member": is_member
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/chat-photo/{account_id}/{chat_id}")
async def get_chat_photo(account_id: str, chat_id: str):
    CACHE_DIR = "data/photo_cache"
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"chat_{account_id}_{chat_id}.jpg")

    # 1. Check Cache FIRST without connecting
    if os.path.exists(cache_path):
        import time
        if time.time() - os.path.getmtime(cache_path) < 3600:
            return StreamingResponse(open(cache_path, "rb"), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})

    # 2. Only connect if cache is missing
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        target_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        buffer = io.BytesIO()
        path = await client.download_profile_photo(target_id, file=buffer)
        
        if not path:
             try:
                 entity = await client.get_input_entity(target_id)
                 path = await client.download_profile_photo(entity, file=buffer)
             except: pass
             
        if not path or buffer.tell() == 0:
            raise HTTPException(status_code=404, detail="No photo")
            
        buffer.seek(0)
        with open(cache_path, "wb") as f:
            f.write(buffer.read())
        buffer.seek(0)

        return StreamingResponse(buffer, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/profile-photo/{account_id}")
async def get_profile_photo(account_id: str):
    """
    Fetch and stream profile photo. 
    Caching now happens exclusively on the FRONTEND (Identity Store).
    This keeps the backend thin and saves disk space.
    """
    # 1. Check if client is ALREADY in memory to avoid redundant connections
    from app.client_cache import _cache
    is_hot = account_id in _cache and _cache[account_id].is_connected()
    
    if not is_hot:
        # If the account is cold, we don't connect just for an avatar.
        # Frontend will use its local store or initials.
        raise HTTPException(status_code=404, detail="Account is cold, no photo stream")

    # 2. Extract client and download stream
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    try:
        from app.client_cache import get_client
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)
        me = await client.get_me()
        
        if not me.photo:
            raise HTTPException(status_code=410, detail="User has no photo")

        buffer = io.BytesIO()
        await client.download_profile_photo(me, file=buffer)
        
        if buffer.tell() > 0:
            buffer.seek(0)
            return StreamingResponse(buffer, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=1800"})
    except Exception as e:
        logger.warning(f"Failed to stream photo for {account_id}: {e}")
    
    raise HTTPException(status_code=404, detail="No photo")
@router.post("/resolve-link/{account_id}")
async def resolve_link(account_id: str, payload: dict):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    url = payload.get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
        
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        # url can be 't.me/username', 'https://t.me/joinchat/...', etc.
        # Strip query parameters for get_entity (like ?start=...)
        clean_url = url
        start_param = None
        if '?' in url:
            parts = url.split('?')
            clean_url = parts[0]
            params = parts[1].split('&')
            for p in params:
                if p.startswith('start='):
                    start_param = p.split('=')[1]
            
        entity = await client.get_entity(clean_url)
        
        chat_type = "user"
        if getattr(entity, 'bot', False):
            chat_type = "user"
        elif isinstance(entity, (types.Chat, types.ChatFull)): 
            chat_type = "group"
        elif isinstance(entity, types.Channel):
            chat_type = "channel" if entity.broadcast else "group"
            
        is_member = True
        if isinstance(entity, (types.Channel, types.Chat)):
            try:
                # For channels/supergroups, we can check participant status
                if isinstance(entity, types.Channel):
                    participant = await client(functions.channels.GetParticipantRequest(
                        channel=entity,
                        participant=await client.get_me()
                    ))
                    is_member = True
                else: 
                    is_member = True # Basic chats usually mean you're in if you have the entity 
            except Exception:
                is_member = False
                
        return {
            "id": str(entity.id),
            "name": getattr(entity, 'title', None) or ((getattr(entity, 'first_name', '') or '') + ' ' + (getattr(entity, 'last_name', '') or '')).strip() or "Unknown",
            "chat_type": chat_type,
            "username": getattr(entity, 'username', None),
            "unread_count": 0,
            "last_message": "Resolved from link",
            "can_send_messages": True,
            "start_param": start_param,
            "is_member": is_member
        }
    except Exception as e:
        logging.error(f"Error resolving link {url}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/resolve-username/{account_id}")
async def resolve_username(account_id: str, payload: dict):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    username = payload.get("username", "").strip().lstrip('@')
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
        
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        entity = await client.get_entity(username)
        
        chat_type = "user"
        if isinstance(entity, (types.Chat, types.ChatFull)): chat_type = "group"
        if isinstance(entity, types.Channel):
            chat_type = "channel" if entity.broadcast else "group"
            
        is_member = True
        if isinstance(entity, (types.Channel, types.Chat)):
            try:
                if isinstance(entity, types.Channel):
                    await client(functions.channels.GetParticipantRequest(
                        channel=entity,
                        participant=await client.get_me()
                    ))
                    is_member = True
                else: is_member = True
            except:
                is_member = False

        return {
            "id": str(entity.id),
            "name": getattr(entity, 'title', None) or ((getattr(entity, 'first_name', '') or '') + ' ' + (getattr(entity, 'last_name', '') or '')).strip() or "Unknown",
            "chat_type": chat_type,
            "username": getattr(entity, 'username', None),
            "unread_count": 0,
            "last_message": f"Resolved from @{username}",
            "is_member": is_member
        }
    except Exception as e:
        logging.error(f"Error resolving username {username}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ── Delete a single Telegram message ─────────────────────────────────────────
@router.delete("/messages/{account_id}/{chat_id}/{message_id}")
async def delete_message(account_id: str, chat_id: str, message_id: int, revoke: bool = True):
    """
    Delete a message from Telegram.
    revoke=True  → delete for everyone (if permitted by Telegram)
    revoke=False → delete only from your own device (local delete)
    """
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))

    try:
        # Resolve the chat entity
        try:
            entity = await client.get_entity(int(chat_id))
        except Exception:
            entity = int(chat_id)

        deleted = await client.delete_messages(entity, [message_id], revoke=revoke)
        return {
            "success": True,
            # FIX: original expression `getattr(deleted, 'pts_count', None) and [1] or []`
            # always returned [] because pts_count is an int, never a list.
            # delete_messages() raises an exception on failure, so reaching here = success.
            "deleted_count": 1,
            "message_id": message_id,
            "revoke": revoke
        }
    except Exception as e:
        logging.error(f"Failed to delete message {message_id} in chat {chat_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ── Bulk delete multiple messages ─────────────────────────────────────────────
@router.post("/messages/{account_id}/{chat_id}/bulk-delete")
async def bulk_delete_messages(account_id: str, chat_id: str, payload: dict):
    """
    Delete multiple messages at once.
    Body: { "message_ids": [int, ...], "revoke": true }
    """
    message_ids = payload.get("message_ids", [])
    revoke = payload.get("revoke", True)

    if not message_ids:
        raise HTTPException(status_code=400, detail="No message_ids provided")

    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))

    try:
        try:
            entity = await client.get_entity(int(chat_id))
        except Exception:
            entity = int(chat_id)

        await client.delete_messages(entity, message_ids, revoke=revoke)
        return {
            "success": True,
            "deleted_count": len(message_ids),
            "message_ids": message_ids,
            "revoke": revoke
        }
    except Exception as e:
        logging.error(f"Bulk delete failed in chat {chat_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/join/{account_id}")
async def join_chat(account_id: str, payload: dict):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    link = payload.get("link", "").strip()
    if not link:
        raise HTTPException(status_code=400, detail="Invite link or @username is required")
        
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        from app.services.reaction.logic import ensure_joined_robust
        await ensure_joined_robust(client, link)
        return {"status": "success", "message": "Joined successfully"}
    except Exception as e:
        logging.error(f"Error joining chat {link}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/leave/{account_id}/{chat_id}")
async def leave_chat(account_id: str, chat_id: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))

    try:
        # Resolve target
        target = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        entity = await client.get_entity(target)
        
        if isinstance(entity, (types.Channel, types.Chat)):
            await client(functions.channels.LeaveChannelRequest(channel=entity))
        else:
            # For private chats, we delete history
            await client(functions.messages.DeleteHistoryRequest(peer=entity, max_id=0, revoke=True))
            
        return {"status": "success", "message": "Left successfully"}
    except Exception as e:
        logging.error(f"Error leaving chat {chat_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/clear-history/{account_id}/{chat_id}")
async def clear_chat_history(account_id: str, chat_id: str, revoke: bool = Query(False)):
    account = await TelegramAccount.get(account_id)
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    try:
        peer = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        await client(functions.messages.DeleteHistoryRequest(
            peer=peer,
            max_id=0,
            revoke=revoke,
            just_clear=False
        ))
        return {"status": "success", "message": "History cleared"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/chat/{account_id}/{chat_id}")
async def delete_chat(account_id: str, chat_id: str, revoke: bool = Query(True)):
    account = await TelegramAccount.get(account_id)
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    try:
        peer = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        entity = await client.get_entity(peer)
        
        if isinstance(entity, (types.Channel, types.Chat)):
            await client(functions.channels.LeaveChannelRequest(channel=entity))
        else:
            await client(functions.messages.DeleteHistoryRequest(
                peer=entity,
                max_id=0,
                revoke=revoke
            ))
        return {"status": "success", "message": "Chat removed"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/bulk-join")
async def bulk_join_chats(
    account_ids: List[str] = Body(...),
    link: str = Body(...),
    min_delay: int = Body(2),
    max_delay: int = Body(5)
):
    if not account_ids:
        raise HTTPException(status_code=400, detail="No accounts provided")
        
    from app.services.reaction.logic import ensure_joined_robust
    import random
    
    results = []
    for aid in account_ids:
        try:
            acc = await TelegramAccount.get(aid)
            if not acc: continue
            
            client = await get_client(aid, acc.session_string, acc.api_id, acc.api_hash)
            await ensure_joined_robust(client, link)
            results.append({"id": aid, "status": "joined"})
            
            if len(account_ids) > 1:
                await asyncio.sleep(random.uniform(min_delay, max_delay))
        except Exception as e:
            results.append({"id": aid, "status": "error", "error": str(e)})
            
    return {"results": results}
