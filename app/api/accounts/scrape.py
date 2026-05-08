import logging
from fastapi import APIRouter, HTTPException, Query
from typing import List, Dict, Any

from app.models import TelegramAccount
from app.client_cache import get_client
from telethon.tl.types import Channel, Chat
from telethon import utils
from fastapi import Depends
from app.api.auth_utils import get_current_user
from app.models.user import User
from bson import ObjectId
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter, InputPeerChannel, InputPeerChat, InputPeerUser

router = APIRouter(prefix="/scrape", tags=["Scraper"])

# Global tracker for active scrapes (AccountID -> {GroupID, TotalScraped, Status})
ACTIVE_SCRAPES = {}

@router.get("/active-tasks")
async def get_active_scrape_tasks(current_user: User = Depends(get_current_user)):
    """Return any active scrapes belonging to this user."""
    user_tasks = []
    for account_id, task in ACTIVE_SCRAPES.items():
        if task.get('user_id') == str(current_user.id):
            user_tasks.append({
                "account_id": account_id,
                "group_id": task.get('group_id'),
                "total": task.get('total', 0),
                "status": "running"
            })
    return user_tasks

@router.get("/{account_id}/groups")
async def get_account_groups(account_id: str, current_user: User = Depends(get_current_user)):
    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    if not await client.is_user_authorized():
        raise HTTPException(status_code=403, detail="Telegram account session is unauthorized or expired. Please reconnect.")

    try:
        # FIX: limit=None fetches ALL dialogs which can be thousands and takes
        # 30+ seconds. A limit of 500 is more than enough for group selection.
        dialogs = await client.get_dialogs(limit=500)
        groups = []

        for d in dialogs:
            # Check if it's a group or megagroup
            is_megagroup = getattr(d.entity, 'megagroup', False)
            is_group = d.is_group or is_megagroup
            is_channel = getattr(d.entity, 'broadcast', False)

            if is_group or is_channel:
                # Try getting participant count
                p_count = getattr(d.entity, 'participants_count', 0)
                
                # Broaden the detection of hidden members
                # 1. Channels (broadcasts) always hide members from non-admins
                # 2. Megagroups can have participants_hidden=True
                # 3. If count is 0 but it's a large group, it might be hidden
                
                is_admin = bool(getattr(d.entity, 'admin_rights', None))
                # For channels, members are ALWAYS hidden unless you are an admin
                members_hidden = False
                if is_channel and not is_admin:
                    members_hidden = True
                elif is_megagroup:
                    members_hidden = bool(getattr(d.entity, 'participants_hidden', False) or getattr(d.entity, 'participants_count_hidden', False))
                
                # Sometimes Telegram doesn't send the flag in get_dialogs, but if it's a large group 
                # and we don't have certain properties, it's a hint.
                # However, participants_hidden is the primary flag.

                groups.append({
                    "id": str(d.id),
                    "name": d.name or "Unknown Group",
                    "participants_count": p_count,
                    "is_channel": is_channel,
                    "is_megagroup": is_megagroup,
                    "is_public": bool(getattr(d.entity, 'username', None)),
                    "members_hidden": members_hidden
                })

        # Sort by participant count descending
        groups.sort(key=lambda x: x["participants_count"] or 0, reverse=True)
        return groups
    except Exception as e:
        logging.error(f"Error fetching groups for scraping: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/join-and-resolve/{account_id}")
async def join_and_resolve_group(account_id: str, payload: dict, current_user: User = Depends(get_current_user)):
    """Join a group via link/username and return its entity info for scraping."""
    link = payload.get("link", "").strip()
    if not link:
        raise HTTPException(status_code=400, detail="Link is required")

    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.types import Channel, Chat
        from telethon.errors import UserAlreadyParticipantError
        
        entity = None
        
        # Determine if private
        is_private = "+" in link or "joinchat/" in link
        
        if is_private:
            if "+" in link: invite_hash = link.split("+")[-1]
            else: invite_hash = link.split("joinchat/")[-1]
            invite_hash = invite_hash.split("/")[0].split("?")[0]
            
            try:
                updates = await client(ImportChatInviteRequest(hash=invite_hash))
                if updates.chats: entity = updates.chats[0]
            except UserAlreadyParticipantError:
                # Already in, need to resolve to get ID
                entity = await client.get_entity(link)
        else:
            # Public join or already member
            try:
                entity = await client.get_entity(link)
                if getattr(entity, 'left', True):
                    await client(JoinChannelRequest(entity))
            except Exception:
                # If get_entity fails, try joining first then resolve
                pass
        
        if not entity:
            entity = await client.get_entity(link)
        
        is_megagroup = getattr(entity, 'megagroup', False)
        is_channel = getattr(entity, 'broadcast', False)
        is_admin = bool(getattr(entity, 'admin_rights', None))
        
        members_hidden = False
        if is_channel and not is_admin:
            members_hidden = True
        elif is_megagroup:
            members_hidden = bool(getattr(entity, 'participants_hidden', False))

        return {
            "id": str(entity.id),
            "name": getattr(entity, 'title', "Unknown Group"),
            "participants_count": getattr(entity, 'participants_count', 0),
            "is_channel": is_channel,
            "is_megagroup": is_megagroup,
            "is_public": bool(getattr(entity, 'username', None)),
            "members_hidden": members_hidden
        }
    except Exception as e:
        logging.error(f"Error in join_and_resolve_group: {e}")
        raise HTTPException(status_code=400, detail=str(e))

from sse_starlette.sse import EventSourceResponse
import json
import asyncio

from app.api.auth_utils import get_current_user, get_current_user_optional

@router.get("/{account_id}/{group_id}/members/stream")
async def scrape_group_members_stream(
    account_id: str, 
    group_id: str, 
    token: str = None, 
    skip_bots: bool = Query(False),
    current_user: User = Depends(get_current_user_optional)
):
    user = current_user
    if not user and token:
        from app.api.auth_utils import get_user_from_token
        user = await get_user_from_token(token)
    
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(user, "access_group_scraping")

    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    if not await client.is_user_authorized():
        raise HTTPException(status_code=403, detail="Telegram account session is unauthorized or expired. Please reconnect.")

    async def stream_generator():
        scrape_id = f"{account_id}_{group_id}"
        ACTIVE_SCRAPES[account_id] = {
            "group_id": group_id,
            "user_id": str(user.id),
            "total": 0,
            "status": "running"
        }
        
        try:
            # First, check group info
            try:
                entity = await client.get_entity(int(group_id))
            except:
                entity = await client.get_entity(group_id)
            
            pending_members = []
            seen_ids = set()
            total_count = 0
            stats = {
                "total": 0, "online": 0, "recently": 0,
                "not_active": 0, "with_username": 0, "without_username": 0
            }

            async for member in client.iter_participants(entity):
                member_id = member.id
                if member_id in seen_ids:
                    continue
                seen_ids.add(member_id)
                if skip_bots and member.bot: continue

                total_count += 1
                if account_id in ACTIVE_SCRAPES:
                    ACTIVE_SCRAPES[account_id]["total"] = total_count

                # Granular status tracking
                from telethon.tl.types import UserStatusOnline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth
                last_seen = member.status
                status_label = "Offline"
                if isinstance(last_seen, UserStatusOnline):
                    stats["online"] += 1
                    status_label = "Online"
                elif isinstance(last_seen, UserStatusRecently):
                    stats["recently"] += 1
                    status_label = "Recently"
                elif isinstance(last_seen, UserStatusLastWeek):
                    status_label = "LastWeek"
                elif isinstance(last_seen, UserStatusLastMonth):
                    status_label = "LastMonth"
                else:
                    stats["not_active"] += 1

                if member.username: stats["with_username"] += 1
                else: stats["without_username"] += 1
                stats["total"] = total_count

                member_data = {
                    "id": str(member_id),
                    "access_hash": str(getattr(member, 'access_hash', '')),
                    "first_name": member.first_name or "",
                    "last_name": member.last_name or "",
                    "username": member.username or "",
                    "phone": member.phone or "",
                    "status_label": status_label,
                    "is_bot": bool(member.bot),
                    "is_premium": bool(getattr(member, 'premium', False)),
                    "is_verified": bool(getattr(member, 'verified', False)),
                    "is_scam": bool(getattr(member, 'scam', False)),
                    "is_fake": bool(getattr(member, 'fake', False)),
                    "has_photo": bool(getattr(member, 'photo', None)),
                    "restricted": bool(getattr(member, 'restricted', False))
                }
                pending_members.append(member_data)
                
                # Stream members in small batches of 5 for ultimate UI smoothness
                if len(pending_members) >= 5:
                    yield {
                        "event": "update",
                        "data": json.dumps({"stats": stats, "members": pending_members})
                    }
                    pending_members = []
                    await asyncio.sleep(0.005) 
            
            # Final cleanup
            if account_id in ACTIVE_SCRAPES:
                del ACTIVE_SCRAPES[account_id]
            yield {
                "event": "done",
                "data": json.dumps({"stats": stats, "members": pending_members})
            }
            
        except Exception as e:
            if account_id in ACTIVE_SCRAPES:
                del ACTIVE_SCRAPES[account_id]
            logging.error(f"Error scraping members: {e}")
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)})
            }


    return EventSourceResponse(stream_generator())


@router.get("/{account_id}/{group_id}/hidden-stream")
async def scrape_group_history_stream(
    account_id: str, 
    group_id: str, 
    limit: int = Query(500),
    token: str = None, 
    skip_bots: bool = Query(False),
    current_user: User = Depends(get_current_user_optional)
):
    user = current_user
    if not user and token:
        from app.api.auth_utils import get_user_from_token
        user = await get_user_from_token(token)
    
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(user, "access_group_scraping")

    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    if not await client.is_user_authorized():
        raise HTTPException(status_code=403, detail="Telegram account session is unauthorized or expired. Please reconnect.")

    async def stream_generator():
        ACTIVE_SCRAPES[account_id] = {
            "group_id": group_id,
            "user_id": str(user.id),
            "total": 0,
            "status": "running"
        }
        
        try:
            # Resolve group
            try:
                entity = await client.get_entity(int(group_id))
            except:
                entity = await client.get_entity(group_id)
            
            pending_members = []
            seen_ids = set()
            total_count = 0
            stats = {
                "total": 0, "online": 0, "recently": 0,
                "not_active": 0, "with_username": 0, "without_username": 0
            }

            from telethon.tl.types import UserStatusOnline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth

            async for message in client.iter_messages(entity, limit=limit):
                if not message.from_id:
                    continue
                
                # Resolve User
                try:
                    user_entity = await client.get_entity(message.from_id)
                except Exception:
                    continue

                if not user_entity or not hasattr(user_entity, 'id'):
                    continue
                    
                member_id = user_entity.id
                if member_id in seen_ids:
                    continue
                seen_ids.add(member_id)
                
                if skip_bots and getattr(user_entity, 'bot', False): continue

                total_count += 1
                if account_id in ACTIVE_SCRAPES:
                    ACTIVE_SCRAPES[account_id]["total"] = total_count

                # Stats & Formatting
                last_seen = getattr(user_entity, 'status', None)
                status_label = "Offline"
                if isinstance(last_seen, UserStatusOnline):
                    stats["online"] += 1
                    status_label = "Online"
                elif isinstance(last_seen, UserStatusRecently):
                    stats["recently"] += 1
                    status_label = "Recently"
                elif isinstance(last_seen, UserStatusLastWeek):
                    status_label = "LastWeek"
                elif isinstance(last_seen, UserStatusLastMonth):
                    status_label = "LastMonth"
                else:
                    stats["not_active"] += 1

                if getattr(user_entity, 'username', None): stats["with_username"] += 1
                else: stats["without_username"] += 1
                stats["total"] = total_count

                member_data = {
                    "id": str(member_id),
                    "access_hash": str(getattr(user_entity, 'access_hash', '')),
                    "first_name": getattr(user_entity, 'first_name', "") or "",
                    "last_name": getattr(user_entity, 'last_name', "") or "",
                    "username": getattr(user_entity, 'username', "") or "",
                    "phone": getattr(user_entity, 'phone', "") or "",
                    "status_label": status_label,
                    "is_bot": bool(getattr(user_entity, 'bot', False)),
                    "is_premium": bool(getattr(user_entity, 'premium', False)),
                    "is_verified": bool(getattr(user_entity, 'verified', False)),
                    "is_scam": bool(getattr(user_entity, 'scam', False)),
                    "is_fake": bool(getattr(user_entity, 'fake', False)),
                    "has_photo": bool(getattr(user_entity, 'photo', None)),
                    "restricted": bool(getattr(user_entity, 'restricted', False))
                }
                pending_members.append(member_data)
                
                if len(pending_members) >= 5:
                    yield {
                        "event": "update",
                        "data": json.dumps({"stats": stats, "members": pending_members})
                    }
                    pending_members = []
                    await asyncio.sleep(0.01) 
            
            # Final cleanup
            if account_id in ACTIVE_SCRAPES:
                del ACTIVE_SCRAPES[account_id]
            yield {
                "event": "done",
                "data": json.dumps({"stats": stats, "members": pending_members})
            }
            
        except Exception as e:
            if account_id in ACTIVE_SCRAPES:
                del ACTIVE_SCRAPES[account_id]
            logging.error(f"Error in history scraping: {e}")
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)})
            }

    return EventSourceResponse(stream_generator())


@router.get("/{account_id}/{group_id}/live-stream")
async def scrape_live_stream_participants(
    account_id: str, 
    group_id: str, 
    token: str = None, 
    current_user: User = Depends(get_current_user_optional)
):
    """Scrape participants from an active Live Stream (Voice/Video Chat)."""
    user = current_user
    if not user and token:
        from app.api.auth_utils import get_user_from_token
        user = await get_user_from_token(token)
    
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(user, "access_group_scraping")

    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    if not await client.is_user_authorized():
        raise HTTPException(status_code=403, detail="Telegram account session is unauthorized or expired. Please reconnect.")

    async def stream_generator():
        ACTIVE_SCRAPES[account_id] = {
            "group_id": group_id,
            "user_id": str(user.id),
            "total": 0,
            "status": "running"
        }
        
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            from telethon.tl.functions.messages import GetFullChatRequest
            from telethon.tl.functions.phone import GetGroupParticipantsRequest
            from telethon.tl.types import InputGroupCall, Channel, Chat, UserStatusOnline, UserStatusRecently

            # Resolve group
            try:
                entity = await client.get_entity(int(group_id))
            except:
                entity = await client.get_entity(group_id)

            # Get Full Info to find Group Call
            if isinstance(entity, Channel):
                full = await client(GetFullChannelRequest(channel=entity))
            else:
                full = await client(GetFullChatRequest(chat_id=entity.id))

            call = getattr(full.full_chat, 'call', None)
            if not call:
                yield {
                    "event": "error",
                    "data": json.dumps({"error": "No active Live Stream (Voice/Video Chat) found in this group."})
                }
                return

            pending_members = []
            seen_ids = set()
            total_count = 0
            stats = {
                "total": 0, "online": 0, "recently": 0,
                "not_active": 0, "with_username": 0, "without_username": 0
            }

            # Fetch participants using corrected request name for this Telethon version
            # GetGroupParticipantsRequest in phone module is for Group Calls
            
            from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
            from telethon.tl.types import DataJSON
            
            # 1. Join the meeting to ensure we see all active participants
            try:
                import random
                me = await client.get_me()
                # Some groups require a non-zero SSRC to avoid the "retry with new SSRC" error
                ssrc = random.randint(1, 0x7fffffff)
                await client(JoinGroupCallRequest(
                    call=call,
                    join_as=me,
                    params=DataJSON(data=json.dumps({"ssrc": ssrc})),
                    muted=True,
                    video_stopped=True
                ))
                logging.info(f"Account {account_id} joined live stream in {group_id} with SSRC {ssrc}")
            except Exception as e:
                logging.warning(f"Could not join live stream: {e}")

            # 2. Scrape the participants
            participants_res = await client(GetGroupParticipantsRequest(
                call=call, 
                ids=[], 
                sources=[], 
                offset='', 
                limit=1000
            ))
            
            # Map users for easy access
            user_map = {u.id: u for u in participants_res.users}

            for p in participants_res.participants:
                # p.peer can be PeerUser, PeerChat, PeerChannel
                from telethon.tl.types import PeerUser
                if not isinstance(p.peer, PeerUser):
                    continue
                
                user_entity = user_map.get(p.peer.user_id)
                if not user_entity:
                    continue

                member_id = user_entity.id
                if member_id in seen_ids:
                    continue
                seen_ids.add(member_id)

                total_count += 1
                if account_id in ACTIVE_SCRAPES:
                    ACTIVE_SCRAPES[account_id]["total"] = total_count

                # Stats & Formatting
                last_seen = getattr(user_entity, 'status', None)
                status_label = "Live" # They are in a live stream!
                if isinstance(last_seen, UserStatusOnline):
                    stats["online"] += 1
                elif isinstance(last_seen, UserStatusRecently):
                    stats["recently"] += 1
                else:
                    stats["not_active"] += 1

                if user_entity.username: stats["with_username"] += 1
                else: stats["without_username"] += 1
                stats["total"] = total_count

                member_data = {
                    "id": str(member_id),
                    "access_hash": str(getattr(user_entity, 'access_hash', '')),
                    "first_name": user_entity.first_name or "",
                    "last_name": user_entity.last_name or "",
                    "username": user_entity.username or "",
                    "phone": getattr(user_entity, 'phone', "") or "",
                    "status_label": status_label,
                    "is_bot": bool(getattr(user_entity, 'bot', False)),
                    "is_premium": bool(getattr(user_entity, 'premium', False)),
                    "is_verified": bool(getattr(user_entity, 'verified', False)),
                    "is_scam": bool(getattr(user_entity, 'scam', False)),
                    "is_fake": bool(getattr(user_entity, 'fake', False)),
                    "has_photo": bool(getattr(user_entity, 'photo', None)),
                }
                pending_members.append(member_data)
                
                if len(pending_members) >= 5:
                    yield {
                        "event": "update",
                        "data": json.dumps({"stats": stats, "members": pending_members})
                    }
                    pending_members = []

            # 3. Leave the meeting
            try:
                # Using 0 as source is often accepted for a generic leave
                await client(LeaveGroupCallRequest(call=call, source=0))
                logging.info(f"Account {account_id} left live stream in {group_id}")
            except Exception as e:
                logging.warning(f"Error leaving live stream: {e}")

            # Final cleanup
            if account_id in ACTIVE_SCRAPES:
                del ACTIVE_SCRAPES[account_id]
            yield {
                "event": "done",
                "data": json.dumps({"stats": stats, "members": pending_members})
            }
            
        except Exception as e:
            if account_id in ACTIVE_SCRAPES:
                del ACTIVE_SCRAPES[account_id]
            logging.error(f"Error scraping live stream: {e}")
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)})
            }

    return EventSourceResponse(stream_generator())


@router.get("/{account_id}/folders")
async def get_account_folders(account_id: str, current_user: User = Depends(get_current_user)):
    """Fetch all dialog filters (folders) for a Telegram account."""
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_folder_scraper")

    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    if not await client.is_user_authorized():
        raise HTTPException(status_code=403, detail="Telegram account session is unauthorized or expired. Please reconnect.")

    try:
        result = await client(GetDialogFiltersRequest())
        # result can be a list or a DialogFilters object with a .filters attribute
        filters = getattr(result, 'filters', result)
        folder_list = []
        for f in filters:
            # We want custom folders, shared folders, and potentially others
            # Some might not have a 'title' attribute (like default folders)
            title = getattr(f, 'title', None)
            if title is None and hasattr(f, 'emoticon'):
                title = f.emoticon # Use emoticon if title is missing
            
            if title:
                # Handle TextWithEntities object from Telethon
                folder_title = getattr(title, 'text', str(title))
                
                folder_list.append({
                    "id": str(getattr(f, 'id', '0')),
                    "title": folder_title,
                    "emoticon": getattr(f, 'emoticon', ''),
                    "peers_count": len(getattr(f, 'include_peers', []))
                })
        return folder_list
    except Exception as e:
        logging.error(f"Error fetching folders: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{account_id}/folders/{folder_id}/groups")
async def get_folder_groups(account_id: str, folder_id: str, current_user: User = Depends(get_current_user)):
    """Extract all group/channel links from a specific folder."""
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "access_folder_scraper")

    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        result = await client(GetDialogFiltersRequest())
        filters = getattr(result, 'filters', result)
        target_filter = None
        for f in filters:
            if str(getattr(f, 'id', '0')) == folder_id:
                target_filter = f
                break
        
        if not target_filter:
            raise HTTPException(status_code=404, detail="Folder not found")

        groups = []
        # include_peers is a list of InputPeer objects
        for peer in target_filter.include_peers:
            try:
                entity = await client.get_entity(peer)
                
                is_channel = getattr(entity, 'broadcast', False)
                is_group = getattr(entity, 'megagroup', False) or (not is_channel and hasattr(entity, 'title'))
                
                if is_channel or is_group:
                    username = getattr(entity, 'username', None)
                    link = f"https://t.me/{username}" if username else None
                    
                    # Handle TextWithEntities for title
                    raw_title = getattr(entity, 'title', 'Unknown')
                    clean_title = getattr(raw_title, 'text', str(raw_title))
                    
                    # Check if we can send messages
                    can_send = False
                    if is_channel and not getattr(entity, 'megagroup', False):
                        if getattr(entity, 'creator', False) or getattr(entity, 'admin_rights', None):
                            can_send = True
                    else:
                        if not getattr(entity, 'left', False) and not getattr(entity, 'kicked', False):
                            banned = getattr(entity, 'banned_rights', None)
                            if not banned or not getattr(banned, 'send_messages', False):
                                can_send = True

                    groups.append({
                        "id": str(entity.id),
                        "name": clean_title,
                        "username": username,
                        "link": link,
                        "is_channel": is_channel,
                        "is_group": is_group,
                        "can_send_messages": can_send,
                        "participants_count": getattr(entity, 'participants_count', 0)
                    })
            except Exception as e:
                logging.warning(f"Failed to resolve peer in folder: {e}")
                continue

        return groups
    except Exception as e:
        logging.error(f"Error fetching groups from folder: {e}")
        raise HTTPException(status_code=500, detail=str(e))

