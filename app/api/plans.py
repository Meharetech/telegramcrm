"""
Plan Management API — with Razorpay Payment Integration
Routes are mounted at /api/plans (see main.py)
"""
import hmac, hashlib
import razorpay
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from typing import Optional
from bson import ObjectId

from app.models import User, Plan, Payment
from app.api.auth_utils import get_current_user
from app.config import settings

router = APIRouter()

# ── Razorpay client (lazy init) ───────────────────────────────────────────────
def get_razorpay():
    return razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
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
    max_reaction_channels: int = 0
    max_forwarder_channels: int = 0
    access_chat_message: bool = False
    access_member_adding: bool = False
    access_message_sender: bool = False
    access_group_scraping: bool = False
    access_connect: bool = True


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
    max_reaction_channels: Optional[int] = None
    max_forwarder_channels: Optional[int] = None
    access_chat_message: Optional[bool] = None
    access_member_adding: Optional[bool] = None
    access_message_sender: Optional[bool] = None
    access_group_scraping: Optional[bool] = None
    access_connect: Optional[bool] = None


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
    plan = Plan(**req.dict())
    await plan.insert()
    return plan_to_dict(plan)


@router.put("/admin/{plan_id}")
async def update_plan(plan_id: str, req: PlanUpdate, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    plan = await Plan.get(ObjectId(plan_id))
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    for field, value in req.dict(exclude_none=True).items():
        setattr(plan, field, value)
    await plan.save()
    
    # ── Global Broadcast (Marketplace Sync) ──────────────────────────────
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


# ── Public: list active plans + my plan ──────────────────────────────────────

@router.get("/")
async def list_public_plans(current_user: User = Depends(get_current_user)):
    """All active plans — visible to any authenticated user."""
    plans = await Plan.find(Plan.is_active == True).to_list()
    return [plan_to_dict(p) for p in plans]


@router.get("/my-plan")
async def get_my_plan(current_user: User = Depends(get_current_user)):
    """Returns the active plan assigned to the currently authenticated user with expiry."""
    if not current_user.plan_id:
        return {"plan": None, "expiry_at": None, "is_expired": False}
    
    try:
        plan = await Plan.get(ObjectId(current_user.plan_id))
        if not plan:
            return {"plan": None, "expiry_at": None, "is_expired": False}

        # ── Individual Service Disable ─────────────────────────────────────────
        disabled = getattr(current_user, "disabled_services", [])
        enabled = getattr(current_user, "enabled_services", [])
        
        plan_data = plan_to_dict(plan)
        
        # Map of disabled_services IDs to plan field names
        service_map = {
            "auto_reply": ["max_auto_replies"],
            "scraper": ["access_group_scraping", "access_member_adding"],
            "reactions": ["max_reaction_channels"],
            "forwarder": ["max_forwarder_channels"],
            "member_adding": ["access_member_adding"],
            "campaign": ["access_message_sender"],
            "creative": ["access_creative_tools"],
            "ban_checker": ["access_ban_checker"],
            "terminal": ["access_terminal"]
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

        return {
            "plan": plan_dict,
            "expiry_at": current_user.plan_expiry_at.isoformat() if current_user.plan_expiry_at else None,
            "purchased_at": purchased_at.isoformat() if purchased_at else None,
            "is_expired": is_expired
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

    price = plan.price_yearly_inr if req.billing_cycle == "yearly" else plan.price_inr
    if price <= 0:
        raise HTTPException(status_code=400, detail="This plan requires manual activation. Contact admin.")

    try:
        client = get_razorpay()
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
            "key_id": settings.RAZORPAY_KEY_ID,
            "plan_name": f"{plan.name} ({req.billing_cycle.capitalize()})",
            "user_email": current_user.email,
            "user_name": getattr(current_user, "full_name", "") or current_user.email,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Razorpay error: {str(e)}")


@router.post("/verify-payment")
async def verify_razorpay_payment(req: VerifyPaymentReq, current_user: User = Depends(get_current_user)):
    """Verify Razorpay payment signature and activate the plan for the user."""
    from app.models.payment import Payment
    from datetime import datetime, timedelta, timezone
    
    # 1. Block Replay Attacks (Check if Order ID is already processed)
    existing_payment = await Payment.find_one(Payment.razorpay_order_id == req.razorpay_order_id)
    if existing_payment:
        raise HTTPException(status_code=400, detail="This transaction has already been processed.")

    # 2. Verify signature
    msg = f"{req.razorpay_order_id}|{req.razorpay_payment_id}"
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()

    if expected != req.razorpay_signature:
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
        plan_id=trusted_plan_id,
        plan_name=f"{plan.name} ({trusted_billing_cycle})",
        amount_inr=actual_price,
        razorpay_order_id=req.razorpay_order_id,
        razorpay_payment_id=req.razorpay_payment_id,
        status="success"
    )
    await payment.insert()

    return {
        "success": True,
        "message": f"Payment verified! Plan '{plan.name}' is now active.",
        "plan": plan_to_dict(plan),
    }

@router.get("/my-payments")
async def get_my_payments(current_user: User = Depends(get_current_user)):
    """Returns the payment history for the currently authenticated user."""
    payments = await Payment.find(Payment.user_id == str(current_user.id)).sort("-created_at").to_list()
    return [{
        "id": str(p.id),
        "plan_name": p.plan_name,
        "amount": p.amount_inr,
        "status": p.status,
        "date": p.created_at.isoformat(),
        "payment_id": p.razorpay_payment_id
    } for p in payments]

@router.get("/admin/subscriptions")
async def get_all_subscriptions_and_payments(current_user: User = Depends(get_current_user)):
    """Admin only: Returns all active users with their current plans AND full payment history."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    from app.models.payment import Payment
    from app.models.user import User
    from app.models.plan import Plan

    # Get all users who have a plan
    users = await User.find(User.plan_id != None).to_list()
    plans = await Plan.find_all().to_list()
    plan_map = {str(p.id): p.name for p in plans}
    
    # Get recent payments (Limit to 200 for performance)
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
            "plan_name": p.plan_name,
            "amount": p.amount_inr,
            "status": p.status,
            "date": p.created_at.isoformat() if p.created_at else None,
            "payment_id": p.razorpay_payment_id
        } for p in payments]
    }
