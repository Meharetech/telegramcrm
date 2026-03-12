import uuid
import time
import asyncio
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from typing import Optional
from app.models import TelegramAccount, User, TelegramAPI, Proxy
from app.client_cache import get_client
from app.api.auth_utils import get_current_user
from bson import ObjectId
import socks

import random
import os
from fastapi import Request

router = APIRouter()

_device_list_cache = None

# --- RATE LIMITER ---
from slowapi import Limiter
from slowapi.util import get_remote_address
limiter = Limiter(key_func=get_remote_address)

def get_random_device():
    global _device_list_cache
    
    # Use memory cache to avoid blocking I/O on every request
    if _device_list_cache:
        return random.choice(_device_list_cache)

    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(base_dir, "devices", "devicelist.csv")
        
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                devices = [line.strip() for line in f if line.strip()]
                if devices:
                    _device_list_cache = devices
                    return random.choice(devices)
    except Exception as e:
        print(f"[auth] Device list error: {e}")
    return "Telegram Android"

# Global dictionary to store temporary clients during login flow
pending_sessions = {}
pending_qr_sessions = {}
upload_progresses = {}

# FIX: Sessions expire after 5 minutes to prevent memory/client leaks
_PENDING_TTL = 300  # seconds

async def _cleanup_expired_pending():
    """Evict stale login sessions older than _PENDING_TTL seconds."""
    now = time.time()
    # Clean phone-code sessions
    for phone in list(pending_sessions.keys()):
        data = pending_sessions.get(phone)
        if data and now - data.get('created_at', now) > _PENDING_TTL:
            try:
                await data['client'].disconnect()
            except Exception:
                pass
            pending_sessions.pop(phone, None)
    # Clean QR sessions
    for sid in list(pending_qr_sessions.keys()):
        data = pending_qr_sessions.get(sid)
        if data and now - data.get('created_at', now) > _PENDING_TTL:
            if data.get('status') == 'pending':
                try:
                    await data['client'].disconnect()
                except Exception:
                    pass
            pending_qr_sessions.pop(sid, None)

@router.get("/upload-progress/{temp_id}")
async def get_upload_progress(temp_id: str):
    return {"progress": upload_progresses.get(temp_id, 0)}

class ConnectRequest(BaseModel):
    phone: str
    api_id: int
    api_hash: str

class VerifyRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str
    password: Optional[str] = None

class QRVerifyRequest(BaseModel):
    session_id: str
    password: str

# Ensure models are fully defined for Pydantic v2
VerifyRequest.model_rebuild()
QRVerifyRequest.model_rebuild()

VerifyRequest.model_rebuild()
QRVerifyRequest.model_rebuild()

@router.post("/send-code")
@limiter.limit("5/minute")
async def send_code(request: Request, req: ConnectRequest, current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    
    # Check general access
    await check_plan_limit(current_user, "access_connect")
    
    # Check account quantity limit
    acc_count = await TelegramAccount.find(TelegramAccount.user_id == str(current_user.id)).count()
    await check_plan_limit(current_user, "max_accounts", acc_count)

    # FIX: Clean up expired pending sessions before creating a new one to prevent RAM bloat
    await _cleanup_expired_pending()

    api_id = req.api_id
    api_hash = req.api_hash
    
    # If not provided, or zero/empty, pick random from user's list
    if (not api_id or not api_hash):
        apis = await TelegramAPI.find(TelegramAPI.user_id == str(current_user.id)).to_list()
        if apis:
            pair = random.choice(apis)
            api_id = pair.api_id
            api_hash = pair.api_hash

    # Fetch free proxy
    proxy_record = await Proxy.find_one(Proxy.user_id == str(current_user.id), Proxy.assigned_account_id == None)
    proxy_dict = None
    if proxy_record:
        rdns_val = True if proxy_record.protocol.lower() != "http" else False
        proxy_dict = {
            "proxy_type": proxy_type,
            "addr": proxy_record.host,
            "port": proxy_record.port,
            "rdns": rdns_val
        }
        if proxy_record.username: proxy_dict["username"] = proxy_record.username
        if proxy_record.password: proxy_dict["password"] = proxy_record.password
        print(f"[auth] Using {proxy_record.protocol.upper()} proxy: {proxy_record.host}:{proxy_record.port} (rdns={rdns_val})")

    device = get_random_device()
    client = TelegramClient(StringSession(), api_id, api_hash, device_model=device, proxy=proxy_dict)
    await client.connect()
    
    try:
        sent = await client.send_code_request(req.phone)
        # Store client and hash for verification step
        pending_sessions[req.phone] = {
            "client": client,
            "phone_code_hash": sent.phone_code_hash,
            "api_id": api_id,
            "api_hash": api_hash,
            "device_model": device,
            "user_id": str(current_user.id),
            "created_at": time.time(),
            "proxy_id": str(proxy_record.id) if proxy_record else None
        }
        return {"phone_code_hash": sent.phone_code_hash, "message": "Code sent"}
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/verify-code")
async def verify_code(req: VerifyRequest, current_user: User = Depends(get_current_user)):
    if req.phone not in pending_sessions:
        raise HTTPException(status_code=404, detail="Session not found. Restart login.")
    
    session_data = pending_sessions[req.phone]
    client = session_data["client"]
    
    try:
        try:
            await client.sign_in(req.phone, req.code, phone_code_hash=req.phone_code_hash)
        except SessionPasswordNeededError:
            if not req.password:
                return {"status": "requires_password", "message": "2FA is enabled. Please provide your password."}
            await client.sign_in(password=req.password)

        # Get the StringSession
        string_session = client.session.save()
        
        # Save to MongoDB
        acc = TelegramAccount(
            user_id=session_data["user_id"],
            phone_number=req.phone,
            api_id=session_data["api_id"],
            api_hash=session_data["api_hash"],
            session_string=string_session,
            device_model=session_data.get("device_model", "Telegram Android"),
            password=req.password, # Save 2FA password
            status="online"
        )
        await acc.insert()
        
        proxy_id = session_data.get("proxy_id")
        if proxy_id:
            db_proxy = await Proxy.get(ObjectId(proxy_id))
            if db_proxy:
                db_proxy.assigned_account_id = str(acc.id)
                await db_proxy.save()
        
        await client.disconnect()
        del pending_sessions[req.phone]
        
        return {"status": "success", "message": "Account connected successfully"}
    except Exception as e:
        if "password" not in str(e).lower():
             await client.disconnect()
             if req.phone in pending_sessions: del pending_sessions[req.phone]
        raise HTTPException(status_code=400, detail=str(e))

async def wait_for_qr_login(session_id: str):
    data = pending_qr_sessions.get(session_id)
    if not data: return
    
    client = data["client"]
    qr_login = data["qr_login"]
    
    try:
        # Wait for the user to scan the QR code
        try:
            await qr_login.wait()
        except SessionPasswordNeededError:
            # 2FA detected, update status and wait for user input
            data["status"] = "requires_password"
            return 

        # Once authorized, update the status
        if await client.is_user_authorized():
            string_session = client.session.save()
            me = await client.get_me()
            phone = me.phone or f"QR_{me.id}"
            
            # Save to MongoDB
            acc = TelegramAccount(
                user_id=data["user_id"],
                phone_number=phone,
                api_id=data["api_id"],
                api_hash=data["api_hash"],
                session_string=string_session,
                device_model=data.get("device_model", "Telegram Android"),
                status="online"
            )
            await acc.insert()
            
            proxy_id = data.get("proxy_id")
            if proxy_id:
                db_proxy = await Proxy.get(ObjectId(proxy_id))
                if db_proxy:
                    db_proxy.assigned_account_id = str(acc.id)
                    await db_proxy.save()
            
            data["status"] = "success"
            data["phone"] = phone
            await client.disconnect()
        else:
            data["status"] = "failed"
            await client.disconnect()
            
    except Exception as e:
        print(f"QR Login Error: {e}")
        data["status"] = "error"
        data["error"] = str(e)
        await client.disconnect()

@router.post("/qr/password")
async def qr_password_verify(req: QRVerifyRequest):
    if req.session_id not in pending_qr_sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    data = pending_qr_sessions[req.session_id]
    client = data["client"]
    
    try:
        await client.sign_in(password=req.password)
        
        if await client.is_user_authorized():
            string_session = client.session.save()
            me = await client.get_me()
            phone = me.phone or f"QR_{me.id}"
            
            # Save to MongoDB
            acc = TelegramAccount(
                user_id=data["user_id"],
                phone_number=phone,
                api_id=data["api_id"],
                api_hash=data["api_hash"],
                session_string=string_session,
                device_model=data.get("device_model", "Telegram Android"),
                password=req.password, # Save the 2FA password
                status="online"
            )
            await acc.insert()
            
            proxy_id = data.get("proxy_id")
            if proxy_id:
                db_proxy = await Proxy.get(ObjectId(proxy_id))
                if db_proxy:
                    db_proxy.assigned_account_id = str(acc.id)
                    await db_proxy.save()
            
            data["status"] = "success"
            data["phone"] = phone
            await client.disconnect()
            return {"status": "success", "message": "2FA verified and account connected"}
        else:
             raise Exception("Authorization failed after password")
             
    except Exception as e:
        data["status"] = "error"
        data["error"] = str(e)
        await client.disconnect()
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/qr/login")
async def qr_login_init(req: ConnectRequest, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user)):
    # FIX: Clean up expired sessions before creating a new QR session
    await _cleanup_expired_pending()

    api_id = req.api_id
    api_hash = req.api_hash
    
    if (not api_id or not api_hash):
        apis = await TelegramAPI.find(TelegramAPI.user_id == str(current_user.id)).to_list()
        if apis:
            pair = random.choice(apis)
            api_id = pair.api_id
            api_hash = pair.api_hash

    proxy_record = await Proxy.find_one(Proxy.user_id == str(current_user.id), Proxy.assigned_account_id == None)
    proxy_dict = None
    if proxy_record:
        proxy_type = socks.HTTP if proxy_record.protocol.lower() == "http" else socks.SOCKS5
        proxy_dict = {
            "proxy_type": proxy_type,
            "addr": proxy_record.host,
            "port": proxy_record.port,
            "rdns": True
        }
        if proxy_record.username: proxy_dict["username"] = proxy_record.username
        if proxy_record.password: proxy_dict["password"] = proxy_record.password

    device = get_random_device()
    client = TelegramClient(StringSession(), api_id, api_hash, device_model=device, proxy=proxy_dict)
    await client.connect()
    
    try:
        qr_login = await client.qr_login()
        session_id = str(uuid.uuid4())
        
        pending_qr_sessions[session_id] = {
            "client": client,
            "qr_login": qr_login,
            "api_id": api_id,
            "api_hash": api_hash,
            "device_model": device,
            "user_id": str(current_user.id),
            "status": "pending",
            "url": qr_login.url,
            "created_at": time.time(),
            "proxy_id": str(proxy_record.id) if proxy_record else None
        }
        
        # Start background task to wait for scan
        background_tasks.add_task(wait_for_qr_login, session_id)
        
        return {"session_id": session_id, "url": qr_login.url}
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/qr/status/{session_id}")
async def qr_login_status(session_id: str):
    if session_id not in pending_qr_sessions:
        raise HTTPException(status_code=404, detail="Session expired or not found")
    
    data = pending_qr_sessions[session_id]
    
    # Auto cleanup sessions older than 5 minutes
    if time.time() - data["created_at"] > 300:
        if data["status"] == "pending":
            await data["client"].disconnect()
        del pending_qr_sessions[session_id]
        raise HTTPException(status_code=404, detail="Session expired")
    
    response = {"status": data["status"]}
    if data["status"] == "success":
        response["phone"] = data["phone"]
    elif data["status"] == "error":
        response["error"] = data.get("error")
        
    return response

@router.get("/list")
async def list_accounts(
    current_user: User = Depends(get_current_user),
    skip: Optional[int] = Query(None, ge=0),
    limit: Optional[int] = Query(None), # removed le=1000
    search: Optional[str] = None
):
    try:
        user_id_str = str(current_user.id)
        query = TelegramAccount.find(TelegramAccount.user_id == user_id_str)
        
        if search:
            query = query.find({"phone_number": {"$regex": search, "$options": "i"}})
            
        is_paginated = skip is not None or limit is not None or search is not None
        
        # Apply defaults only if searching/paginating
        effective_skip = skip or 0
        effective_limit = limit or 1000
        
        if is_paginated:
            total = await query.count()
            # USE PROJECTION: Fetch only UI metadata, skip heavy session strings
            accounts = await query.skip(effective_skip).limit(effective_limit).project(TelegramAccount.AccountShort).to_list()
        else:
            accounts = await query.project(TelegramAccount.AccountShort).to_list()
        
        # Fetch all proxies for this user to map them
        proxies = await Proxy.find(Proxy.user_id == user_id_str).to_list()
        proxy_map = {p.assigned_account_id: p for p in proxies if p.assigned_account_id}
        
        result_list = []
        for a in accounts:
            acc_id = str(a.id)
            proxy = proxy_map.get(acc_id)
            
            acc_data = {
                "phone": a.phone_number, 
                "status": a.status or "disconnected", 
                "id": acc_id, 
                "device_model": a.device_model or "Telegram Android",
                "daily_contacts_limit": a.daily_contacts_limit,
                "contacts_added_today": a.contacts_added_today,
                "unread_count": a.unread_count,
                "last_message_at": a.last_message_at,
                "contact_count": a.contact_count,
                "created_at": a.created_at,
                "last_check_status": a.last_check_status,
                "last_check_time": a.last_check_time,
                "proxy": {
                    "host": proxy.host,
                    "port": proxy.port,
                    "protocol": proxy.protocol
                } if proxy else None
            }
            result_list.append(acc_data)
            
        if is_paginated:
            return {"total": total, "accounts": result_list}
        return result_list
        
    except Exception as e:
        print(f"[list_accounts] Error: {e}")
        return [] if not is_paginated else {"total": 0, "accounts": []}

@router.get("/dashboard-stats")
async def get_dashboard_stats(current_user: User = Depends(get_current_user)):
    try:
        user_id_str = str(current_user.id)
        total = await TelegramAccount.find(TelegramAccount.user_id == user_id_str).count()
        online = await TelegramAccount.find(TelegramAccount.user_id == user_id_str, TelegramAccount.status == "online").count()
        # In a real app, these would come from more complex aggregations
        return {
            "total_accounts": total,
            "active_sessions": online,
            "messages_sent": "14.2k",
            "account_health": "100%"
        }
    except Exception as e:
        print(f"[dashboard-stats] Error: {e}")
        return {"total_accounts": 0, "active_sessions": 0, "messages_sent": "0", "account_health": "0%"}

@router.post("/select/{account_id}")
async def select_active_account(account_id: str, current_user: User = Depends(get_current_user)):
    """Mark an account as 'currently being used' and disconnect other 'idle' accounts."""
    from app.client_cache import prune_others
    pruned_count = await prune_others(account_id, str(current_user.id))
    return {"status": "success", "pruned": pruned_count}

@router.delete("/{account_id}")
async def delete_account(account_id: str, current_user: User = Depends(get_current_user)):
    from bson import ObjectId
    try:
        acc = await TelegramAccount.find_one(
            TelegramAccount.id == ObjectId(account_id),
            TelegramAccount.user_id == str(current_user.id)
        )
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found")
        
        # 1. Disconnect client if attached
        from app.services.auto_reply.engine import detach_account
        from app.client_cache import invalidate, _cache
        
        # Stop background services
        active_client = _cache.get(account_id)
        await detach_account(active_client, account_id)
        
        # Disconnect from memory cache
        await invalidate(account_id)

        # 2. Clean up ALL related data
        from app.models.auto_reply import AutoReplySettings, AutoReplyRule
        from app.models.forwarder import ForwarderRule
        
        # Delete Auto-Reply Settings & Rules
        await AutoReplySettings.find(AutoReplySettings.account_id == account_id).delete()
        await AutoReplyRule.find(AutoReplyRule.account_id == account_id).delete()
        
        # Delete Forwarder Rules
        await ForwarderRule.find(ForwarderRule.account_id == account_id).delete()

        # 3. Final removal of the identity
        await acc.delete()
        
        return {"status": "success", "message": "Account and all associated settings removed."}
    except Exception as e:
        print(f"[delete_account] Critical error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{account_id}/check-ban")
async def check_ban(account_id: str, current_user: User = Depends(get_current_user)):
    from bson import ObjectId
    from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedBanError, SessionExpiredError, UserDeactivatedError
    from app.client_cache import get_client
    
    try:
        acc = await TelegramAccount.find_one(
            TelegramAccount.id == ObjectId(account_id),
            TelegramAccount.user_id == str(current_user.id)
        )
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found")

        try:
            client = await get_client(
                account_id,
                acc.session_string,
                acc.api_id,
                acc.api_hash,
                acc.device_model or "Telegram Android"
            )
            if not client:
                raise AuthKeyUnregisteredError(request=None)
            
            # Simple check
            await client.get_me()
            from datetime import datetime
            acc.last_check_status = "active"
            acc.last_check_time = datetime.utcnow()
            await acc.save()
            return {"status": "active", "message": "Account is active"}
            
        except (AuthKeyUnregisteredError, UserDeactivatedBanError, SessionExpiredError, UserDeactivatedError) as e:
            # Delete account if banned
            from app.services.auto_reply.engine import detach_account
            from app.client_cache import invalidate, _cache
            
            # Stop background services
            active_client = _cache.get(account_id)
            if active_client:
                await detach_account(active_client, account_id)
            
            # Disconnect from memory cache
            await invalidate(account_id)

            from app.models.auto_reply import AutoReplySettings, AutoReplyRule
            from app.models.forwarder import ForwarderRule
            
            await AutoReplySettings.find(AutoReplySettings.account_id == account_id).delete()
            await AutoReplyRule.find(AutoReplyRule.account_id == account_id).delete()
            await ForwarderRule.find(ForwarderRule.account_id == account_id).delete()

            await acc.delete()
            return {"status": "banned", "message": f"Account banned/deleted: {type(e).__name__}"}
        
        except Exception as e:
            # Other errors
            from datetime import datetime
            acc.last_check_status = "error"
            acc.last_check_time = datetime.utcnow()
            await acc.save()
            return {"status": "error", "message": str(e)}

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

