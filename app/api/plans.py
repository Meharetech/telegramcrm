"""
Plan Management API — with Razorpay Payment Integration
Routes are mounted at /api/plans (see main.py)
"""
import hmac, hashlib, os, shutil
import razorpay
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from typing import Optional
from bson import ObjectId

from app.models import User, Plan, Payment, SystemSettings, WalletTransaction
from app.api.auth_utils import get_current_user
from app.config import settings
from app.services.email_service import send_payment_notification_to_admin

router = APIRouter()

@router.post("/upload-gateway-image")
async def upload_gateway_image(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Upload a QR code or image for a payment gateway (Admin Only)."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # 1. Validate File Type
    allowed = ['.jpg', '.jpeg', '.png', '.webp']
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Invalid file type. Only JPG, PNG, and WEBP images are allowed.")
    
    # 2. Basic Filename Sanitation
    os.makedirs("uploads/gateways", exist_ok=True)
    safe_name = f"gt_{int(datetime.now().timestamp())}{ext}"
    file_path = f"uploads/gateways/{safe_name}"
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    return {"url": f"uploads/gateways/{safe_name}"}

# ── Razorpay client (lazy init) ───────────────────────────────────────────────
def get_razorpay(key_id: str = None, key_secret: str = None):
    # Prefer passed credentials, fallback to env settings. Treatment of empty strings as None.
    kid = key_id if key_id else getattr(settings, 'RAZORPAY_KEY_ID', None)
    ksec = key_secret if key_secret else getattr(settings, 'RAZORPAY_KEY_SECRET', None)
    
    if not kid or not ksec:
        raise HTTPException(
            status_code=500, 
            detail="Razorpay authentication keys are missing. Please configure them in the Admin Panel or .env file."
        )
        
    return razorpay.Client(
        auth=(kid, ksec)
    )


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class PlanCreate(BaseModel):
    name: str
    description: Optional[str] = None
    price_inr: float = 0.0
    price_yearly_inr: float = 0.0
    is_active: bool = True
    max_accounts: int = 10
    max_api_keys: int = 10
    max_proxies: int = 10
    max_auto_replies: int = 0
    max_autoreply_accounts: int = 1
    max_reaction_channels: int = 0
    max_forwarder_channels: int = 0
    access_chat_message: bool = False
    access_member_adding: bool = False
    access_message_sender: bool = False
    access_group_scraping: bool = False
    access_connect: bool = True
    access_ban_checker: bool = False
    access_creative_tools: bool = False
    access_contacts_manager: bool = False
    access_reminders: bool = False
    access_terminal: bool = False
    access_bot_hub: bool = False
    access_folder_campaign: bool = False
    access_folder_scraper: bool = False
    access_group_joiner: bool = False
    max_bots: int = 1
    max_folder_accounts: int = 1


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price_inr: Optional[float] = None
    price_yearly_inr: Optional[float] = None
    is_active: Optional[bool] = None
    max_accounts: Optional[int] = None
    max_api_keys: Optional[int] = None
    max_proxies: Optional[int] = None
    max_auto_replies: Optional[int] = None
    max_autoreply_accounts: Optional[int] = None
    max_reaction_channels: Optional[int] = None
    max_forwarder_channels: Optional[int] = None
    access_chat_message: Optional[bool] = None
    access_member_adding: Optional[bool] = None
    access_message_sender: Optional[bool] = None
    access_group_scraping: Optional[bool] = None
    access_connect: Optional[bool] = None
    access_ban_checker: Optional[bool] = None
    access_creative_tools: Optional[bool] = None
    access_contacts_manager: Optional[bool] = None
    access_reminders: Optional[bool] = None
    access_terminal: Optional[bool] = None
    access_bot_hub: Optional[bool] = None
    access_folder_campaign: Optional[bool] = None
    access_folder_scraper: Optional[bool] = None
    access_group_joiner: Optional[bool] = None
    max_bots: Optional[int] = None
    max_folder_accounts: Optional[int] = None


class AssignPlan(BaseModel):
    plan_id: Optional[str] = None


class CreateOrderReq(BaseModel):
    plan_id: str
    billing_cycle: str = "monthly" # "monthly" or "yearly"


class VerifyPaymentReq(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    plan_id: str
    billing_cycle: str = "monthly"


class ManualGatewaySchema(BaseModel):
    name: str
    qr_code_url: Optional[str] = None
    upi_id: Optional[str] = None
    instructions: Optional[str] = None
    is_active: bool = True

class CryptoGatewaySchema(BaseModel):
    name: str
    symbol: str
    network: str
    wallet_address: str
    qr_code_url: Optional[str] = None
    is_active: bool = True

class SystemSettingsSchema(BaseModel):
    razorpay_enabled: bool = True
    manual_payment_enabled: bool = True
    crypto_payment_enabled: bool = True
    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    manual_gateways: list[ManualGatewaySchema] = []
    crypto_gateways: list[CryptoGatewaySchema] = []
    
    # Demo / Free Tier Limits
    demo_max_accounts: int = 5
    demo_max_proxies: int = 50
    demo_max_api_keys: int = 50
    demo_daily_contacts_limit: int = 0
    demo_can_auto_reply: bool = False
    demo_can_forward: bool = False
    demo_can_react: bool = False
    demo_access_member_adding: bool = False
    demo_access_group_joiner: bool = False
    demo_access_group_scraping: bool = False
    demo_access_message_sender: bool = False
    demo_access_terminal: bool = False
    demo_access_contacts_manager: bool = False
    demo_access_reminders: bool = False
    demo_access_bot_hub: bool = False
    demo_access_folder_campaign: bool = False
    demo_access_folder_scraper: bool = False
    demo_access_creative_tools: bool = False
    demo_access_ban_checker: bool = False
    demo_max_auto_replies: int = 1
    demo_max_reaction_channels: int = 1
    demo_max_forwarder_channels: int = 1
    demo_max_bots: int = 1
    demo_raw_proxies: Optional[str] = None
    demo_raw_apis: Optional[str] = None

class InitiateManualPaymentReq(BaseModel):
    plan_id: str
    billing_cycle: str = "monthly"
    gateway: str # "manual" or "crypto"
    sub_gateway: str # e.g. "PhonePe" or "USDT"
    transaction_ref: str
    proof_image_url: Optional[str] = None

class AdminVerifyPaymentReq(BaseModel):
    status: str # "success" or "rejected"
    admin_note: Optional[str] = None

class InitiateWalletTopupReq(BaseModel):
    amount: float
    gateway: str
    sub_gateway: str
    transaction_ref: str
    proof_image_url: Optional[str] = None


# ── Helper ────────────────────────────────────────────────────────────────────

def plan_to_dict(p: Plan) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "description": p.description,
        "price_inr": p.price_inr,
        "price_yearly_inr": p.price_yearly_inr,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "max_accounts": p.max_accounts,
        "max_api_keys": p.max_api_keys,
        "max_proxies": p.max_proxies,
        "max_auto_replies": p.max_auto_replies,
        "max_autoreply_accounts": getattr(p, "max_autoreply_accounts", 1),
        "max_reaction_channels": p.max_reaction_channels,
        "max_forwarder_channels": p.max_forwarder_channels,
        "access_chat_message": p.access_chat_message,
        "access_member_adding": p.access_member_adding,
        "access_message_sender": p.access_message_sender,
        "access_group_scraping": p.access_group_scraping,
        "access_connect": p.access_connect,
        "access_ban_checker": getattr(p, "access_ban_checker", False),
        "access_creative_tools": getattr(p, "access_creative_tools", False),
        "access_contacts_manager": getattr(p, "access_contacts_manager", False),
        "access_reminders": getattr(p, "access_reminders", False),
        "access_terminal": getattr(p, "access_terminal", False),
        "access_bot_hub": getattr(p, "access_bot_hub", False),
        "access_folder_campaign": getattr(p, "access_folder_campaign", False),
        "access_folder_scraper": getattr(p, "access_folder_scraper", False),
        "access_group_joiner": getattr(p, "access_group_joiner", False),
        "max_bots": getattr(p, "max_bots", 1),
        "max_folder_accounts": getattr(p, "max_folder_accounts", 1),
    }

async def get_demo_plan_data() -> dict:
    settings_db = await SystemSettings.find_one()
    if not settings_db:
        settings_db = SystemSettings()
    
    return {
        "id": "demo",
        "name": "Free Trial Plan",
        "price_inr": 0,
        "max_accounts": settings_db.demo_max_accounts,
        "max_proxies": settings_db.demo_max_proxies,
        "max_api_keys": settings_db.demo_max_api_keys,
        "daily_contacts_limit": getattr(settings_db, "demo_daily_contacts_limit", 0),
        "is_demo": True,
        
        "access_connect": True,
        "access_member_adding": getattr(settings_db, "demo_access_member_adding", False),
        "access_group_joiner": getattr(settings_db, "demo_access_group_joiner", False),
        "access_group_scraping": getattr(settings_db, "demo_access_group_scraping", False),
        "access_message_sender": getattr(settings_db, "demo_access_message_sender", False),
        "access_terminal": getattr(settings_db, "demo_access_terminal", False),
        "access_contacts_manager": getattr(settings_db, "demo_access_contacts_manager", False),
        "access_reminders": getattr(settings_db, "demo_access_reminders", False),
        "access_bot_hub": getattr(settings_db, "demo_access_bot_hub", False),
        "access_folder_campaign": getattr(settings_db, "demo_access_folder_campaign", False),
        "access_folder_scraper": getattr(settings_db, "demo_access_folder_scraper", False),
        "access_creative_tools": getattr(settings_db, "demo_access_creative_tools", False),
        "access_ban_checker": getattr(settings_db, "demo_access_ban_checker", False),
        "can_auto_reply": getattr(settings_db, "demo_can_auto_reply", False),
        "can_forward": getattr(settings_db, "demo_can_forward", False),
        "can_react": getattr(settings_db, "demo_can_react", False),
        
        "max_auto_replies": getattr(settings_db, "demo_max_auto_replies", 1),
        "max_autoreply_accounts": 1,
        "max_reaction_channels": getattr(settings_db, "demo_max_reaction_channels", 1),
        "max_forwarder_channels": getattr(settings_db, "demo_max_forwarder_channels", 1),
        "max_bots": getattr(settings_db, "demo_max_bots", 1),
        "demo_raw_proxies": getattr(settings_db, "demo_raw_proxies", ""),
        "demo_raw_apis": getattr(settings_db, "demo_raw_apis", "")
    }



# ── Admin CRUD ────────────────────────────────────────────────────────────────

@router.get("/admin")
async def list_plans(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    plans = await Plan.find_all().to_list()
    return [plan_to_dict(p) for p in plans]


@router.post("/admin")
async def create_plan(req: PlanCreate, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    plan = Plan(**req.model_dump())
    await plan.insert()
    return plan_to_dict(plan)


# ── STATIC ADMIN ROUTES (Must be ABOVE parameterized routes) ─────────────────

@router.get("/admin/gateway-settings")
async def get_gateway_settings(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    settings_db = await SystemSettings.find_one()
    if not settings_db:
        settings_db = SystemSettings()
        await settings_db.insert()
    
    return settings_db


@router.put("/admin/gateway-settings")
async def update_gateway_settings(req: SystemSettingsSchema, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    settings_db = await SystemSettings.find_one()
    if not settings_db:
        settings_db = SystemSettings(**req.model_dump())
        await settings_db.insert()
    else:
        for field, value in req.model_dump().items():
            setattr(settings_db, field, value)
        await settings_db.save()
    
    # Broadcast update to all connected clients (e.g. users on /plans)
    try:
        from app.api.ws import manager
        await manager.broadcast({"type": "gateway_settings_updated", "data": {}})
    except Exception as e:
        print(f"WS Broadcast error: {e}")
    
    return settings_db


@router.get("/admin/pending-payments")
async def get_pending_payments(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    payments = await Payment.find(Payment.status == "pending").sort("-created_at").to_list()
    return payments


@router.get("/admin/payments")
async def get_all_payments(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    payments = await Payment.find_all().sort("-created_at").limit(200).to_list()
    return payments


@router.get("/admin/subscriptions")
async def get_all_subscriptions_and_payments(current_user: User = Depends(get_current_user)):
    """Admin only: Returns all active users with their current plans AND full payment history."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")

    users = await User.find(User.plan_id != None).to_list()
    plans = await Plan.find_all().to_list()
    plan_map = {str(p.id): p.name for p in plans}
    
    payments = await Payment.find_all().sort("-created_at").limit(200).to_list()
    
    return {
        "active_users": [{
            "id": str(u.id),
            "email": u.email,
            "full_name": u.full_name,
            "plan_name": plan_map.get(str(u.plan_id), "Unknown"),
            "is_active": u.is_active,
            "services_active": getattr(u, "services_active", True)
        } for u in users],
        "all_payments": [{
            "id": str(p.id),
            "user_email": p.user_email,
            "user_phone": p.user_phone,
            "plan_name": p.plan_name,
            "amount": p.amount,
            "status": p.status,
            "date": p.created_at.isoformat() if p.created_at else None,
            "payment_id": p.razorpay_payment_id or p.transaction_ref,
            "gateway": p.gateway,
            "sub_gateway": p.sub_gateway,
            "transaction_ref": p.transaction_ref,
            "proof": p.proof_image_url
        } for p in payments]
    }


# ── PARAMETERIZED ADMIN ROUTES ────────────────────────────────────────────────

@router.put("/admin/{plan_id}")
async def update_plan(plan_id: str, req: PlanUpdate, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    plan = await Plan.get(ObjectId(plan_id))
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    for field, value in req.model_dump(exclude_none=True).items():
        setattr(plan, field, value)
    await plan.save()
    
    try:
        from app.api.ws import manager
        await manager.broadcast({
            "type": "system_plan_updated",
            "data": {"plan_id": str(plan.id), "message": "Plan details updated"}
        })
    except: pass
    
    return plan_to_dict(plan)


@router.delete("/admin/{plan_id}")
async def delete_plan(plan_id: str, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    plan = await Plan.get(ObjectId(plan_id))
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    await User.find(User.plan_id == plan_id).update({"$set": {"plan_id": None}})
    await plan.delete()
    return {"message": "Plan deleted"}


@router.post("/admin/verify-payment/{payment_id}")
async def verify_payment_admin(payment_id: str, req: AdminVerifyPaymentReq, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    payment = await Payment.get(ObjectId(payment_id))
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    if payment.status != "pending":
        raise HTTPException(status_code=400, detail="Payment is already processed")
    
    payment.status = req.status
    payment.admin_note = req.admin_note
    payment.verified_at = datetime.now(timezone.utc)
    await payment.save()
    
    if req.status == "success":
        user = await User.get(ObjectId(payment.user_id))
        if user:
            if payment.plan_id == "wallet_topup":
                # Handle Wallet Topup Approval
                user.wallet_balance += payment.amount
                await user.save()
                
                # Create Wallet Transaction Log
                txn = WalletTransaction(
                    user_id=str(user.id),
                    amount=payment.amount,
                    type="credit",
                    description=f"Wallet Top-up via {payment.sub_gateway or payment.gateway}",
                    created_at=datetime.now(timezone.utc)
                )
                await txn.insert()
            else:
                # Handle Plan Assignment
                days = 365 if payment.billing_cycle == "yearly" else 30
                expiry = datetime.now(timezone.utc) + timedelta(days=days)
                
                user.plan_id = payment.plan_id
                user.plan_expiry_at = expiry
                user.billing_cycle = payment.billing_cycle
                await user.save()
            
    return {"message": f"Payment {req.status} successful"}


@router.post("/admin/assign-user/{user_id}")
async def assign_plan_to_user(user_id: str, req: AssignPlan, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    user = await User.get(ObjectId(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if req.plan_id:
        plan = await Plan.get(ObjectId(req.plan_id))
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        user.plan_id = req.plan_id
    else:
        user.plan_id = None
    await user.save()
    return {"message": "Plan assigned", "plan_id": user.plan_id}


# ── Public (NO AUTH): for landing page ───────────────────────────────────────

@router.get("/public")
async def list_public_plans_no_auth():
    """Active plans visible to unauthenticated users (landing page pricing)."""
    plans = await Plan.find(Plan.is_active == True).sort("+price_inr").to_list()
    return [plan_to_dict(p) for p in plans]


# ── Authenticated: list active plans + my plan ──────────────────────────────

@router.get("/")
async def list_public_plans(current_user: User = Depends(get_current_user)):
    """All active plans — visible to any authenticated user."""
    plans = await Plan.find(Plan.is_active == True).to_list()
    return [plan_to_dict(p) for p in plans]


@router.get("/my-plan")
async def get_my_plan(current_user: User = Depends(get_current_user)):
    """Returns the active plan assigned to the currently authenticated user with expiry."""
    if not current_user.plan_id or current_user.plan_id == "null":
        # 1. Initialize trial if not set
        if not getattr(current_user, "trial_started_at", None):
            current_user.trial_started_at = datetime.now(timezone.utc)
            await current_user.save()
        
        # 2. Check Expiration (7 Days)
        now = datetime.now(timezone.utc)
        start = current_user.trial_started_at.replace(tzinfo=timezone.utc) if current_user.trial_started_at.tzinfo is None else current_user.trial_started_at
        days_passed = (now - start).days
        
        if days_passed >= 7:
            return {
                "plan": None,
                "expiry_at": None,
                "is_expired": True,
                "services_active": False,
                "trial_expired": True
            }
        
        demo_plan = await get_demo_plan_data()
        demo_plan["trial_days_left"] = max(0, 7 - days_passed)

        return {
            "plan": demo_plan,
            "expiry_at": (start + timedelta(days=7)).isoformat(),
            "is_expired": False,
            "services_active": getattr(current_user, "services_active", False)
        }
    
    try:
        plan = await Plan.get(ObjectId(current_user.plan_id))
        if not plan:
            # 1. Initialize trial if not set
            if not getattr(current_user, "trial_started_at", None):
                current_user.trial_started_at = datetime.now(timezone.utc)
                await current_user.save()
            
            # 2. Check Expiration (7 Days)
            now = datetime.now(timezone.utc)
            start = current_user.trial_started_at.replace(tzinfo=timezone.utc) if current_user.trial_started_at.tzinfo is None else current_user.trial_started_at
            days_passed = (now - start).days
            
            if days_passed >= 7:
                return {
                    "plan": None,
                    "expiry_at": None,
                    "is_expired": True,
                    "services_active": False,
                    "trial_expired": True
                }
            
            demo_plan = await get_demo_plan_data()
            demo_plan["trial_days_left"] = max(0, 7 - days_passed)

            return {
                "plan": demo_plan,
                "expiry_at": (start + timedelta(days=7)).isoformat(),
                "is_expired": False,
                "services_active": getattr(current_user, "services_active", False)
            }

        # ── Individual Service Disable ─────────────────────────────────────────
        disabled = getattr(current_user, "disabled_services", [])
        enabled = getattr(current_user, "enabled_services", [])
        
        plan_data = plan_to_dict(plan)
        
        # Map of disabled_services IDs to plan field names
        service_map = {
            "auto_reply": ["max_auto_replies", "max_autoreply_accounts", "access_chat_message"],
            "scraper": ["access_group_scraping", "access_member_adding"],
            "reactions": ["max_reaction_channels"],
            "forwarder": ["max_forwarder_channels"],
            "member_adding": ["access_member_adding"],
            "campaign": ["access_message_sender", "access_chat_message"],
            "creative": ["access_creative_tools"],
            "ban_checker": ["access_ban_checker"],
            "terminal": ["access_terminal"],
            "contacts": ["access_contacts_manager"],
            "reminders": ["access_reminders"],
            "connect": ["access_connect"]
        }

        # 1. Apply Force-Disables (Highest Priority)
        for svc_id in disabled:
            fields = service_map.get(svc_id, [])
            for f in fields:
                if f.startswith("max_"):
                    plan_data[f] = 0
                else:
                    plan_data[f] = False
        
        # 2. Apply Force-Enables (Override Plan Defaults)
        for svc_id in enabled:
            # If it's already disabled by 'disabled' list, we skip it or let 'disabled' win?
            # Let's say if it is in both, disabled wins for safety.
            if svc_id in disabled: continue
            
            fields = service_map.get(svc_id, [])
            for f in fields:
                if f.startswith("max_"):
                    # If the plan has 0, give them a generous default for force-enable
                    if plan_data[f] <= 0:
                        plan_data[f] = 100 # Allow 100 as a default for forced services
                else:
                    plan_data[f] = True
        
        plan_dict = plan_data

        is_expired = False
        purchased_at = None
        if current_user.plan_expiry_at:
            is_expired = datetime.now(timezone.utc) > current_user.plan_expiry_at.replace(tzinfo=timezone.utc) if current_user.plan_expiry_at.tzinfo is None else datetime.now(timezone.utc) > current_user.plan_expiry_at
            
            # Estimate purchase date based on cycle
            days = 365 if getattr(current_user, 'billing_cycle', None) == 'yearly' else 30
            purchased_at = current_user.plan_expiry_at - timedelta(days=days)

        if is_expired:
            plan_dict = await get_demo_plan_data()

        return {
            "plan": plan_dict,
            "expiry_at": current_user.plan_expiry_at.isoformat() if current_user.plan_expiry_at else None,
            "purchased_at": purchased_at.isoformat() if purchased_at else None,
            "is_expired": is_expired,
            "services_active": getattr(current_user, "services_active", False)
        }
    except Exception as e:
        print(f"Error in get_my_plan: {e}")
        return {"plan": None, "expiry_at": None, "is_expired": False}


# ── Razorpay Payment ──────────────────────────────────────────────────────────

@router.post("/create-order")
async def create_razorpay_order(req: CreateOrderReq, current_user: User = Depends(get_current_user)):
    """Create a Razorpay order for a plan purchase."""
    plan = await Plan.get(ObjectId(req.plan_id))
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.is_active:
        raise HTTPException(status_code=400, detail="Plan is not active")

    settings_db = await SystemSettings.find_one()
    if settings_db and not settings_db.razorpay_enabled:
        raise HTTPException(status_code=400, detail="Razorpay is currently disabled")

    price = plan.price_yearly_inr if req.billing_cycle == "yearly" else plan.price_inr
    if price <= 0:
        raise HTTPException(status_code=400, detail="This plan requires manual activation. Contact admin.")

    try:
        client = get_razorpay(
            key_id=settings_db.razorpay_key_id if settings_db else None,
            key_secret=settings_db.razorpay_key_secret if settings_db else None
        )
        amount_paise = int(price * 100)  # Razorpay works in paise
        order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"rcpt_{str(current_user.id)[-12:]}_{req.plan_id[-12:]}",
            "notes": {
                "plan_id": req.plan_id,
                "plan_name": plan.name,
                "billing_cycle": req.billing_cycle,
                "user_id": str(current_user.id),
                "user_email": current_user.email,
            }
        })
        return {
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key_id": settings_db.razorpay_key_id if settings_db else getattr(settings, 'RAZORPAY_KEY_ID', None),
            "plan_name": f"{plan.name} ({req.billing_cycle.capitalize()})",
            "user_email": current_user.email,
            "user_name": getattr(current_user, "full_name", "") or current_user.email,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Razorpay error: {str(e)}")


@router.post("/verify-payment")
async def verify_razorpay_payment(req: VerifyPaymentReq, current_user: User = Depends(get_current_user)):
    """Verify Razorpay payment signature and activate the plan for the user."""
    
    # 1. Block Replay Attacks (Check if Order ID is already processed)
    existing_payment = await Payment.find_one(Payment.razorpay_order_id == req.razorpay_order_id)
    if existing_payment:
        raise HTTPException(status_code=400, detail="This transaction has already been processed.")

    # 2. Verify signature
    settings_db = await SystemSettings.find_one()
    key_secret = settings_db.razorpay_key_secret if settings_db and settings_db.razorpay_key_secret else getattr(settings, 'RAZORPAY_KEY_SECRET', None)
    
    if not key_secret:
        raise HTTPException(status_code=500, detail="Razorpay secret key not configured")

    msg = f"{req.razorpay_order_id}|{req.razorpay_payment_id}"
    generated_signature = hmac.new(
        key_secret.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()

    if generated_signature != req.razorpay_signature:
        raise HTTPException(status_code=400, detail="Payment verification failed — invalid signature.")

    # 3. Prevent Client Parameter Tampering (Fetch Trusted Data from Razorpay)
    try:
        client = get_razorpay()
        razorpay_order = client.order.fetch(req.razorpay_order_id)
        
        # Extract the secure trusted notes we injected during /create-order
        trusted_notes = razorpay_order.get("notes", {})
        trusted_plan_id = trusted_notes.get("plan_id")
        trusted_billing_cycle = trusted_notes.get("billing_cycle", "monthly")
        
        if not trusted_plan_id:
            # Fallback to the client's payload if notes somehow failed (legacy compatibility)
            trusted_plan_id = req.plan_id
            trusted_billing_cycle = req.billing_cycle
            
    except Exception as e:
        # If Razorpay API drops, we can't securely verify the parameters
        raise HTTPException(status_code=500, detail="Unable to retrieve trusted order details from the gateway.")

    # 4. Activate the plan for the user using the TRUSTED plan ID
    plan = await Plan.get(ObjectId(trusted_plan_id))
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    
    # Calculate expiry using TRUSTED billing cycle
    days = 365 if trusted_billing_cycle == "yearly" else 30
    expiry = datetime.now(timezone.utc) + timedelta(days=days)

    current_user.plan_id = trusted_plan_id
    current_user.plan_expiry_at = expiry
    current_user.billing_cycle = trusted_billing_cycle
    await current_user.save()

    # 5. Save to Payment History
    actual_price = plan.price_yearly_inr if trusted_billing_cycle == "yearly" else plan.price_inr
    
    payment = Payment(
        user_id=str(current_user.id),
        user_email=current_user.email,
        user_phone=current_user.phone,
        plan_id=trusted_plan_id,
        plan_name=f"{plan.name} ({trusted_billing_cycle})",
        amount=actual_price,
        razorpay_order_id=req.razorpay_order_id,
        razorpay_payment_id=req.razorpay_payment_id,
        status="success"
    )
    await payment.insert()

    # Notify Admin
    try:
        send_payment_notification_to_admin(
            user_email=current_user.email,
            amount=actual_price,
            method="Razorpay",
            target=f"Plan: {plan.name}",
            ref=req.razorpay_payment_id
        )
    except: pass

    return {
        "success": True,
        "message": f"Payment verified! Plan '{plan.name}' is now active.",
        "plan": plan_to_dict(plan),
    }

# ── Manual & Crypto Payments (User) ───────────────────────────────────────────

@router.get("/gateways")
async def get_active_gateways(current_user: User = Depends(get_current_user)):
    """Returns all active payment gateways and their details."""
    settings_db = await SystemSettings.find_one()
    if not settings_db:
        # Create default settings if not exists
        settings_db = SystemSettings()
        await settings_db.insert()
    
    return {
        "razorpay_enabled": settings_db.razorpay_enabled,
        "manual_payment_enabled": settings_db.manual_payment_enabled,
        "crypto_payment_enabled": settings_db.crypto_payment_enabled,
        "manual_gateways": [g for g in settings_db.manual_gateways if g.is_active] if settings_db.manual_payment_enabled else [],
        "crypto_gateways": [g for g in settings_db.crypto_gateways if g.is_active] if settings_db.crypto_payment_enabled else [],
    }

@router.post("/initiate-manual")
async def initiate_manual_payment(
    plan_id: str = Form(...),
    gateway: str = Form(...),
    sub_gateway: str = Form(...),
    transaction_ref: str = Form(...),
    billing_cycle: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """User submits a manual or crypto payment proof with an image file."""
    plan = await Plan.get(ObjectId(plan_id))
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    
    settings_db = await SystemSettings.find_one()
    if gateway == "manual" and (not settings_db or not settings_db.manual_payment_enabled):
        raise HTTPException(status_code=400, detail="Manual payments are disabled")
    if gateway == "crypto" and (not settings_db or not settings_db.crypto_payment_enabled):
        raise HTTPException(status_code=400, detail="Crypto payments are disabled")

    # 1. Prevent Duplicate Submissions (Ref ID check)
    existing = await Payment.find_one(Payment.transaction_ref == transaction_ref)
    if existing:
        raise HTTPException(status_code=400, detail="This transaction ID has already been submitted.")

    # 1. Validate File Type
    allowed = ['.jpg', '.jpeg', '.png', '.webp']
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Invalid proof file.")

    # 2. Save proof image
    os.makedirs("uploads/proofs", exist_ok=True)
    safe_name = f"proof_{int(datetime.now().timestamp())}_{ObjectId()}{ext}"
    file_path = f"uploads/proofs/{safe_name}"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    proof_url = f"uploads/proofs/{safe_name}"
    price = plan.price_yearly_inr if billing_cycle == "yearly" else plan.price_inr
    
    payment = Payment(
        user_id=str(current_user.id),
        user_email=current_user.email,
        user_phone=current_user.phone,
        plan_id=plan_id,
        plan_name=f"{plan.name} ({billing_cycle})",
        amount=price,
        gateway=gateway,
        sub_gateway=sub_gateway,
        status="pending",
        transaction_ref=transaction_ref,
        proof_image_url=proof_url,
        billing_cycle=billing_cycle
    )
    await payment.insert()
    
    # Notify Admin
    try:
        send_payment_notification_to_admin(
            user_email=current_user.email,
            amount=price,
            method=f"{gateway.upper()} ({sub_gateway})",
            target=f"Plan: {plan.name}",
            ref=transaction_ref
        )
    except: pass

    return {"message": "Payment submitted!", "payment_id": str(payment.id)}


@router.post("/initiate-wallet-topup")
async def initiate_wallet_topup(
    amount: float = Form(...),
    gateway: str = Form(...),
    sub_gateway: str = Form(...),
    transaction_ref: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """User submits a wallet top-up request with payment proof."""
    # 1. Prevent Duplicate Ref IDs
    existing = await Payment.find_one(Payment.transaction_ref == transaction_ref)
    if existing:
        raise HTTPException(status_code=400, detail="Transaction ID already exists.")

    # 2. Save proof image
    ext = os.path.splitext(file.filename)[1].lower()
    os.makedirs("uploads/proofs", exist_ok=True)
    safe_name = f"wallet_{int(datetime.now().timestamp())}_{ObjectId()}{ext}"
    file_path = f"uploads/proofs/{safe_name}"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    payment = Payment(
        user_id=str(current_user.id),
        user_email=current_user.email,
        user_phone=current_user.phone,
        plan_id="wallet_topup",
        plan_name="Wallet Top-up",
        amount=amount,
        gateway=gateway,
        sub_gateway=sub_gateway,
        status="pending",
        transaction_ref=transaction_ref,
        proof_image_url=f"uploads/proofs/{safe_name}"
    )
    await payment.insert()

    # Notify Admin
    try:
        send_payment_notification_to_admin(
            user_email=current_user.email,
            amount=amount,
            method=f"{gateway.upper()} ({sub_gateway})",
            target="Wallet Top-up",
            ref=transaction_ref
        )
    except: pass

    return {"message": "Deposit request submitted!", "payment_id": str(payment.id)}


@router.get("/my-payments")
async def get_my_payments(current_user: User = Depends(get_current_user)):
    """Returns the payment history for the currently authenticated user."""
    payments = await Payment.find(Payment.user_id == str(current_user.id)).sort("-created_at").to_list()
    return [{
        "id": str(p.id),
        "plan_name": p.plan_name,
        "amount": p.amount,
        "gateway": p.gateway,
        "sub_gateway": p.sub_gateway,
        "status": p.status,
        "date": p.created_at.isoformat(),
        "payment_id": p.razorpay_payment_id or p.transaction_ref
    } for p in payments]
