from fastapi import APIRouter, HTTPException, Depends, status, Request
from pydantic import BaseModel, EmailStr
from app.models import (
    User, TelegramAPI, TelegramAccount, Proxy, 
    ReactionTask, AutoReplyRule, ForwarderRule, Reminder
)
from app.api.auth_utils import get_password_hash, verify_password, create_access_token, get_current_user
from typing import Optional
import re

# --- RATE LIMITER ---
from slowapi import Limiter
from slowapi.util import get_remote_address
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

class UserRegister(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user: dict

@router.post("/register", response_model=dict)
@limiter.limit("5/minute")
async def register(req: UserRegister, request: Request):
    existing_user = await User.find_one(User.email == req.email)
    if existing_user:
        raise HTTPException(status_code=400, detail="User with this email already exists")
    
    hashed_password = get_password_hash(req.password)
    user = User(
        email=req.email,
        hashed_password=hashed_password,
        full_name=req.full_name
    )
    await user.insert()
    return {"message": "User registered successfully"}

@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
async def login(req: UserLogin, request: Request):
    user = await User.find_one(User.email == req.email)
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    
    access_token = create_access_token(data={"sub": str(user.id)})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "is_admin": user.is_admin
        }
    }

@router.post("/admin/login", response_model=Token)
async def admin_login(req: UserLogin):
    user = await User.find_one(User.email == req.email)
    if not user or not user.is_admin or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin credentials")
    
    access_token = create_access_token(data={"sub": str(user.id)})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "is_admin": user.is_admin
        }
    }

@router.get("/admin/stats")
async def get_admin_stats(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    total_users = await User.count()
    return {
        "total_users": total_users,
        "total_accounts": await TelegramAccount.count(),
        "total_proxies": await Proxy.count(),
        "system_health": "99.9%",
        "uptime_days": 42
    }

@router.get("/admin/service-usage")
async def get_service_usage(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    users = await User.find_all().to_list()
    if not users:
        return []

    # Get bulk counts via aggregation to prevent O(N * 7) massive database query load
    async def get_bulk_counts(model, filters=None):
        match_stage = {"$match": filters} if filters else {"$match": {}}
        pipeline = [
            match_stage,
            {"$group": {"_id": "$user_id", "count": {"$sum": 1}}}
        ]
        results = await model.get_motor_collection().aggregate(pipeline).to_list(length=None)
        return {str(item["_id"]): item["count"] for item in results if item.get("_id")}

    import asyncio
    # Execute all group-by queries entirely in parallel in 1 round-trip
    counts = await asyncio.gather(
        get_bulk_counts(TelegramAccount),
        get_bulk_counts(Proxy),
        get_bulk_counts(TelegramAPI),
        get_bulk_counts(ReactionTask, {"is_active": True}),
        get_bulk_counts(AutoReplyRule, {"is_enabled": True}),
        get_bulk_counts(ForwarderRule, {"is_enabled": True}),
        get_bulk_counts(Reminder, {"status": "pending"})
    )

    acc_map, proxy_map, api_map, rx_map, auto_map, fwd_map, rem_map = counts

    usage_data = []
    for u in users:
        uid = str(u.id)
        usage_data.append({
            "id": uid,
            "full_name": u.full_name,
            "email": u.email,
            "account_count": acc_map.get(uid, 0),
            "proxy_count": proxy_map.get(uid, 0),
            "api_count": api_map.get(uid, 0),
            "reaction_count": rx_map.get(uid, 0),
            "auto_reply_count": auto_map.get(uid, 0),
            "forwarder_count": fwd_map.get(uid, 0),
            "reminder_count": rem_map.get(uid, 0),
            "services_active": getattr(u, 'services_active', False),
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if hasattr(u, 'created_at') and u.created_at else None
        })
    
    return usage_data

@router.get("/admin/user-stats")
async def get_admin_user_stats(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    one_week = now - timedelta(days=7)
    one_month = now - timedelta(days=30)
    six_months = now - timedelta(days=180)

    total = await User.count()
    admins = await User.find({"$or": [{"is_admin": True}, {"is_super_admin": True}]}).count()
    active = await User.find({"is_active": True}).count()
    new_week = await User.find({"created_at": {"$gt": one_week}}).count()
    new_month = await User.find({"created_at": {"$gt": one_month}}).count()
    new_six_months = await User.find({"created_at": {"$gt": six_months}}).count()

    return {
        "total": total,
        "admins": admins,
        "active": active,
        "newWeek": new_week,
        "newMonth": new_month,
        "newSixMonths": new_six_months
    }

@router.get("/admin/users")
async def list_admin_users(
    skip: int = 0, 
    limit: int = 100, 
    search: str = "",
    current_user: User = Depends(get_current_user)
):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    query = {}
    if search:
        # ── SECURITY FIX: Escape regex special characters to prevent ReDoS ────
        safe_search = re.escape(search)
        query = {"$or": [
            {"email": {"$regex": safe_search, "$options": "i"}},
            {"full_name": {"$regex": safe_search, "$options": "i"}}
        ]}

    # USE PROJECTION: Fetch only metadata for admin user list
    users = await User.find(query).sort("-created_at").skip(skip).limit(limit).project(User.UserShort).to_list()
    total = await User.find(query).count()

    return {
        "total": total,
        "users": [
            {
                "id": str(u.id),
                "email": u.email,
                "full_name": u.full_name,
                "is_active": u.is_active,
                "is_admin": u.is_admin,
                "is_super_admin": u.is_super_admin,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "plan_id": u.plan_id,
                "plan_expiry_at": u.plan_expiry_at.isoformat() if u.plan_expiry_at else None,
                "billing_cycle": u.billing_cycle,
                "services_active": getattr(u, 'services_active', True),
                "disabled_services": getattr(u, 'disabled_services', []),
                "enabled_services": getattr(u, 'enabled_services', [])
            } for u in users
        ]
    }

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    is_super_admin: Optional[bool] = None
    plan_id: Optional[str] = None
    plan_expiry_at: Optional[str] = None # ISO string
    services_active: Optional[bool] = None
    disabled_services: Optional[list[str]] = None
    enabled_services: Optional[list[str]] = None

@router.put("/admin/users/{user_id}")
async def update_admin_user(user_id: str, req: UserUpdate, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    from bson import ObjectId
    user = await User.get(ObjectId(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # ── Master Protection ─────────────────────────────────────────────────────
    # Super Admin protection
    if user.is_super_admin:
        # A Super Admin can only be edited by themselves or another Super Admin, 
        # but let's make it even more restricted: 
        # You cannot remove IS_ADMIN or IS_ACTIVE from a super admin account
        if req.is_admin is False or req.is_active is False or req.is_super_admin is False:
             raise HTTPException(status_code=403, detail="Super Admin status is permanent and cannot be removed")
    
    if req.full_name is not None: user.full_name = req.full_name
    if req.email is not None: user.email = req.email
    if req.is_active is not None: user.is_active = req.is_active
    if req.is_admin is not None: user.is_admin = req.is_admin
    if req.is_super_admin is not None: user.is_super_admin = req.is_super_admin
    if req.services_active is not None: user.services_active = req.services_active
    if req.disabled_services is not None: user.disabled_services = req.disabled_services
    if req.enabled_services is not None: user.enabled_services = req.enabled_services
    
    # Subscription management
    if req.plan_id is not None:
        user.plan_id = req.plan_id if req.plan_id else None
    
    if req.plan_expiry_at is not None:
        from datetime import datetime
        try:
            user.plan_expiry_at = datetime.fromisoformat(req.plan_expiry_at.replace("Z", "+00:00"))
        except:
             pass
    
    await user.save()
    
    # ── Real-time Notification (Smoothness) ──────────────────────────────
    try:
        from app.api.ws import manager
        await manager.send_to_user(str(user.id), {
            "type": "plan_updated",
            "data": {"message": "Plan settings updated by admin"}
        })
    except: pass
    
    return {"status": "success"}

@router.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    from bson import ObjectId
    user = await User.get(ObjectId(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Protect Super Admins from deletion
    if user.is_super_admin:
        raise HTTPException(status_code=403, detail="Super Admin cannot be deleted")
        
    await user.delete()
    return {"message": "User deleted successfully"}

class UserAPISettings(BaseModel):
    telegram_apis: list[dict]

@router.get("/profile")
async def get_user_profile(current_user: User = Depends(get_current_user)):
    """Fetch user profile and plan details."""
    from app.models.plan import Plan
    
    plan_name = "Free Plan"
    plan_details = None
    
    if current_user.plan_id:
        plan = await Plan.get(current_user.plan_id)
        if plan:
            plan_name = plan.name
            plan_details = plan.dict()

    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "plan_name": plan_name,
        "plan_details": plan_details,
        "plan_expiry": current_user.plan_expiry_at.isoformat() if current_user.plan_expiry_at else None,
        "is_active": current_user.is_active,
        "services_active": current_user.services_active,
        "created_at": current_user.created_at.isoformat() if hasattr(current_user, 'created_at') and current_user.created_at else None
    }

@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    apis = await TelegramAPI.find(TelegramAPI.user_id == str(current_user.id)).to_list()
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "telegram_apis": [{"api_id": a.api_id, "api_hash": a.api_hash} for a in apis],
        "is_active": current_user.is_active
    }

@router.get("/profile")
async def get_profile(current_user: User = Depends(get_current_user)):
    from app.models import Plan
    from bson import ObjectId
    
    plan_details = None
    if current_user.plan_id:
        try:
            plan = await Plan.get(ObjectId(current_user.plan_id))
            if plan:
                plan_details = {
                    "id": str(plan.id),
                    "name": plan.name,
                    "price_inr": plan.price_inr,
                    "price_yearly_inr": plan.price_yearly_inr,
                    "max_accounts": plan.max_accounts,
                    "max_api_keys": plan.max_api_keys,
                    "max_scraper_tasks": getattr(plan, 'max_scraper_tasks', 0),
                    "daily_contacts_limit": getattr(plan, 'daily_contacts_limit', 50),
                    "can_auto_reply": getattr(plan, 'can_auto_reply', False),
                    "can_forward": getattr(plan, 'can_forward', False),
                    "can_react": getattr(plan, 'can_react', False)
                }
        except:
            pass

    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_active": current_user.is_active,
        "created_at": current_user.created_at.isoformat() if hasattr(current_user, 'created_at') and current_user.created_at else None,
        "plan_id": current_user.plan_id,
        "plan_expiry_at": current_user.plan_expiry_at.isoformat() if current_user.plan_expiry_at else None,
        "billing_cycle": current_user.billing_cycle,
        "plan": plan_details
    }

@router.get("/settings")
async def get_settings(current_user: User = Depends(get_current_user)):
    apis = await TelegramAPI.find(TelegramAPI.user_id == str(current_user.id)).to_list()
    return {
        "telegram_apis": [{"api_id": a.api_id, "api_hash": a.api_hash} for a in apis]
    }

@router.post("/settings")
async def update_settings(req: UserAPISettings, current_user: User = Depends(get_current_user)):
    from app.api.auth_utils import check_plan_limit
    await check_plan_limit(current_user, "max_api_keys", len(req.telegram_apis))

    # Delete existing apis for this user and replace with new ones
    await TelegramAPI.find(TelegramAPI.user_id == str(current_user.id)).delete()
    for api_data in req.telegram_apis:
        new_api = TelegramAPI(
            user_id=str(current_user.id),
            api_id=api_data["api_id"],
            api_hash=api_data["api_hash"]
        )
        await new_api.insert()
    return {"message": "Settings updated successfully"}

# ── Global Resource Management (Admin Only) ──────────────────────────────────

@router.get("/admin/accounts")
async def admin_get_all_accounts(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # USE PROJECTION: Admin overview only needs metadata, skip heavy session strings
    accounts = await TelegramAccount.find_all().project(TelegramAccount.AccountShort).to_list()
    # Join with user email for better context
    users = {str(u.id): u.email for u in await User.find_all().to_list()}
    
    return [{
        "id": str(a.id),
        "user_email": users.get(a.user_id, "unknown"),
        "phone_number": a.phone_number,
        "status": a.status,
        "is_active": a.is_active,
        "created_at": a.created_at.isoformat() if a.created_at else None
    } for a in accounts]

@router.delete("/admin/accounts/{acc_id}")
async def admin_delete_account(acc_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    acc = await TelegramAccount.get(ObjectId(acc_id))
    if acc:
        await acc.delete()
    return {"message": "Account deleted"}

@router.get("/admin/proxies")
async def admin_get_all_proxies(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    proxies = await Proxy.find_all().to_list()
    users = {str(u.id): u.email for u in await User.find_all().to_list()}
    
    return [{
        "id": str(p.id),
        "user_email": users.get(p.user_id, "unknown"),
        "host": p.host,
        "port": p.port,
        "protocol": p.protocol,
        "username": p.username,
        "password": p.password
    } for p in proxies]

class ProxyUpdate(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

@router.put("/admin/proxies/{proxy_id}")
async def admin_update_proxy(proxy_id: str, req: ProxyUpdate, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    proxy = await Proxy.get(ObjectId(proxy_id))
    if not proxy: raise HTTPException(status_code=404)
    
    if req.host is not None: proxy.host = req.host
    if req.port is not None: proxy.port = req.port
    if req.protocol is not None: proxy.protocol = req.protocol
    if req.username is not None: proxy.username = req.username
    if req.password is not None: proxy.password = req.password
    
    await proxy.save()
    return {"message": "Proxy updated"}

@router.delete("/admin/proxies/{proxy_id}")
async def admin_delete_proxy(proxy_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    proxy = await Proxy.get(ObjectId(proxy_id))
    if proxy: await proxy.delete()
    return {"message": "Proxy deleted"}

@router.get("/admin/apis")
async def admin_get_all_apis(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    apis = await TelegramAPI.find_all().to_list()
    users = {str(u.id): u.email for u in await User.find_all().to_list()}
    
    return [{
        "id": str(a.id),
        "user_email": users.get(a.user_id, "unknown"),
        "api_id": a.api_id,
        "api_hash": a.api_hash
    } for a in apis]

class APIUpdate(BaseModel):
    api_id: Optional[int] = None
    api_hash: Optional[str] = None

@router.put("/admin/apis/{api_id}")
async def admin_update_api(api_id: str, req: APIUpdate, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    api = await TelegramAPI.get(ObjectId(api_id))
    if not api: raise HTTPException(status_code=404)
    
    if req.api_id is not None: api.api_id = req.api_id
    if req.api_hash is not None: api.api_hash = req.api_hash
    
    await api.save()
    return {"message": "API updated"}

@router.delete("/admin/apis/{api_id}")
async def admin_delete_api(api_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    api = await TelegramAPI.get(ObjectId(api_id))
    if api: await api.delete()
    return {"message": "API deleted"}

# ── Service Management (Admin Task Control) ──────────────────────────────────

@router.get("/admin/reactions")
async def admin_get_all_reactions(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    reactions = await ReactionTask.find_all().to_list()
    users = {str(u.id): u.email for u in await User.find_all().to_list()}
    
    return [{
        "id": str(r.id),
        "user_email": users.get(r.user_id, "unknown"),
        "target_link": r.target_link,
        "status": r.status,
        "account_count": len(r.account_ids),
        "is_active": r.is_active,
        "created_at": r.created_at.isoformat() if r.created_at else None
    } for r in reactions]

@router.delete("/admin/reactions/{task_id}")
async def admin_delete_reaction(task_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    task = await ReactionTask.get(ObjectId(task_id))
    if task: await task.delete()
    return {"message": "Reaction task deleted"}

@router.get("/admin/auto-replies")
async def admin_get_all_auto_replies(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    rules = await AutoReplyRule.find_all().to_list()
    users = {str(u.id): u.email for u in await User.find_all().to_list()}
    
    return [{
        "id": str(rule.id),
        "user_email": users.get(rule.user_id, "unknown"),
        "name": rule.name,
        "is_enabled": rule.is_enabled,
        "trigger_type": rule.trigger_type,
        "reply_text": rule.reply_text[:50] + "..." if len(rule.reply_text) > 50 else rule.reply_text
    } for rule in rules]

@router.delete("/admin/auto-replies/{rule_id}")
async def admin_delete_auto_reply(rule_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    rule = await AutoReplyRule.get(ObjectId(rule_id))
    if rule: await rule.delete()
    return {"message": "Auto-reply rule deleted"}

@router.get("/admin/forwarders")
async def admin_get_all_forwarders(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    rules = await ForwarderRule.find_all().to_list()
    users = {str(u.id): u.email for u in await User.find_all().to_list()}
    
    return [{
        "id": str(rule.id),
        "user_email": users.get(rule.user_id, "unknown"),
        "name": rule.name,
        "is_enabled": rule.is_enabled,
        "source_id": rule.source_id,
        "targets_count": len(rule.target_ids)
    } for rule in rules]

@router.delete("/admin/forwarders/{rule_id}")
async def admin_delete_forwarder(rule_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    rule = await ForwarderRule.get(ObjectId(rule_id))
    if rule: await rule.delete()
    return {"message": "Forwarder rule deleted"}

@router.get("/admin/reminders")
async def admin_get_all_reminders(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    reminders = await Reminder.find_all().to_list()
    users = {str(u.id): u.email for u in await User.find_all().to_list()}
    
    return [{
        "id": str(rem.id),
        "user_email": users.get(rem.user_id, "unknown"),
        "message": rem.message[:50] + "..." if len(rem.message) > 50 else rem.message,
        "status": rem.status,
        "remind_at": rem.remind_at.isoformat() if rem.remind_at else None
    } for rem in reminders]

@router.delete("/admin/reminders/{rem_id}")
async def admin_delete_reminder(rem_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    from bson import ObjectId
    rem = await Reminder.get(ObjectId(rem_id))
    if rem: await rem.delete()
    return {"message": "Reminder deleted"}
