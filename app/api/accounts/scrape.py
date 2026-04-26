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

