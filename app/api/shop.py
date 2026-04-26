import asyncio
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.models import User, TelegramAccount, WalletTransaction, ShopPurchase, SystemSettings
from app.api.auth_utils import get_current_user
from bson import ObjectId
import random
from datetime import datetime, timezone, timedelta
from app.api.ws import manager

router = APIRouter()
picking_lock = asyncio.Lock()

async def get_shop_settings():
    settings = await SystemSettings.find_one()
    if not settings:
        settings = SystemSettings()
        await settings.insert()
    return settings

@router.get("/accounts")
async def list_accounts_for_sale(current_user: User = Depends(get_current_user)):
    # 1. Identify locked accounts (pending purchases < timeout)
    shop_settings = await get_shop_settings()
    timeout_mins = shop_settings.shop_otp_timeout_mins
    three_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=timeout_mins)
    pending_purchases = await ShopPurchase.find(
        ShopPurchase.status == "pending",
        ShopPurchase.created_at > three_mins_ago
    ).to_list()
    
    locked_account_ids = {p.account_id: p for p in pending_purchases}
    
    # 2. List accounts
    accounts = await TelegramAccount.find(
        TelegramAccount.is_for_sale == True,
        TelegramAccount.is_sold == False
    ).to_list()
    
    result = []
    for a in accounts:
        aid = str(a.id)
        lock = locked_account_ids.get(aid)
        
        # If locked by someone else, hide it
        if lock and lock.user_id != str(current_user.id):
            continue
            
        result.append({
            "id": aid,
            "phone_number": a.phone_number[:5] + "****" + a.phone_number[-2:],
            "sale_price": shop_settings.shop_account_price, # Use Global Price
            "device_model": a.device_model,
            "created_at": a.created_at.isoformat() if a.created_at.tzinfo else a.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "is_locked_by_me": True if lock else False,
            "purchase_id": str(lock.id) if lock else None
        })
    
    return {
        "accounts": result,
        "settings": {
            "price": shop_settings.shop_account_price,
            "timeout_mins": shop_settings.shop_otp_timeout_mins
        }
    }

@router.post("/purchase-random")
async def buy_random_account(current_user: User = Depends(get_current_user)):
    print(f"DEBUG: buy_random_account called by {current_user.id}")
    # 1. Identify locked accounts
    shop_settings = await get_shop_settings()
    timeout_mins = shop_settings.shop_otp_timeout_mins
    price = shop_settings.shop_account_price
    
    three_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=timeout_mins)

    # 0. Anti-Spam: Check if user already has a pending purchase
    existing_pending = await ShopPurchase.find_one(
        ShopPurchase.user_id == str(current_user.id),
        ShopPurchase.status == "pending",
        ShopPurchase.created_at > three_mins_ago
    )
    if existing_pending:
        raise HTTPException(status_code=400, detail="You already have a pending purchase. Please complete or cancel it first.")
    pending_purchases = await ShopPurchase.find(
        ShopPurchase.status == "pending",
        ShopPurchase.created_at > three_mins_ago
    ).to_list()
    locked_ids = {p.account_id for p in pending_purchases}

    # 2. Find available accounts
    available = await TelegramAccount.find(
        TelegramAccount.is_for_sale == True,
        TelegramAccount.is_sold == False
    ).to_list()
    
    # 3. Filter out locked
    candidates = [a for a in available if str(a.id) not in locked_ids]
    
    if not candidates:
        raise HTTPException(status_code=400, detail="No accounts available right now. Please try again later.")
    
    # Check balance before lock
    if current_user.wallet_balance < price:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    # 4. Pick random (Atomic Pick with Lock)
    async with picking_lock:
        # Re-verify candidates inside the lock to be absolute sure
        pending_purchases = await ShopPurchase.find(
            ShopPurchase.status == "pending",
            ShopPurchase.created_at > three_mins_ago
        ).to_list()
        locked_ids = {p.account_id for p in pending_purchases}
        
        candidates = [a for a in available if str(a.id) not in locked_ids]
        if not candidates:
            raise HTTPException(status_code=400, detail="No accounts available right now. Please try again later.")
        
        account = random.choice(candidates)
        
        # 5. Initiate Purchase (Inside lock to prevent others from picking this one)
        purchase = ShopPurchase(
            user_id=str(current_user.id),
            account_id=str(account.id),
            phone_number=account.phone_number,
            price=price,
            status="pending",
            created_at=datetime.now(timezone.utc)
        )
        await purchase.insert()
    
    # Broadcast Stock Update
    available_count = await TelegramAccount.find(
        TelegramAccount.is_for_sale == True,
        TelegramAccount.is_sold == False
    ).count()
    await manager.broadcast({"type": "shop_update", "available_count": available_count})

    return {
        "id": str(purchase.id),
        "purchase_id": str(purchase.id),
        "status": purchase.status,
        "phone_number": account.phone_number,
        "price": price,
        "timeout_mins": timeout_mins,
        "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat()
    }

@router.post("/buy/{account_id}")
async def buy_account(account_id: str, current_user: User = Depends(get_current_user)):
    account = await TelegramAccount.get(ObjectId(account_id))
    
    if not account or not account.is_for_sale or account.is_sold:
        raise HTTPException(status_code=404, detail="Account not available for sale")
    
    if current_user.wallet_balance < account.sale_price:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance. Please top up.")

    # Check if already locked by someone else
    shop_settings = await get_shop_settings()
    timeout_mins = shop_settings.shop_otp_timeout_mins
    price = shop_settings.shop_account_price

    three_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=timeout_mins)
    
    async with picking_lock:
        existing_lock = await ShopPurchase.find_one(
            ShopPurchase.account_id == account_id,
            ShopPurchase.status == "pending",
            ShopPurchase.created_at > three_mins_ago
        )
        if existing_lock and existing_lock.user_id != str(current_user.id):
            raise HTTPException(status_code=400, detail="Account is currently being purchased by another user")

        # Create Pending Purchase Attempt
        purchase = ShopPurchase(
            user_id=str(current_user.id),
            account_id=str(account.id),
            phone_number=account.phone_number,
            price=price,
            status="pending"
        )
        await purchase.insert()
    
    # Broadcast Stock Update
    available_count = await TelegramAccount.find(
        TelegramAccount.is_for_sale == True,
        TelegramAccount.is_sold == False
    ).count()
    await manager.broadcast({"type": "shop_update", "available_count": available_count})

    return {
        "id": str(purchase.id),
        "purchase_id": str(purchase.id), 
        "status": purchase.status,
        "phone_number": account.phone_number,
        "price": price,
        "timeout_mins": timeout_mins,
        "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat()
    }

@router.get("/purchase-status/{purchase_id}")
async def check_purchase_status(purchase_id: str, current_user: User = Depends(get_current_user)):
    purchase = await ShopPurchase.get(ObjectId(purchase_id))
    if not purchase or purchase.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Purchase not found")
    
    # CRITICAL: If already finalized, just return current status
    if purchase.status in ["success", "cancelled", "expired"]:
        return {
            "id": str(purchase.id),
            "purchase_id": str(purchase.id),
            "status": purchase.status,
            "phone_number": purchase.phone_number,
            "price": purchase.price,
            "otp_message": purchase.otp_message,
            "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat()
        }
    
    if purchase.status != "pending":
        return {
            "id": str(purchase.id),
            "purchase_id": str(purchase.id),
            "status": purchase.status,
            "phone_number": purchase.phone_number,
            "price": purchase.price,
            "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "otp_message": purchase.otp_message
        }

    # Check timeout (3 mins = 180 seconds)
    now = datetime.now(timezone.utc)
    purchase_date = purchase.created_at
    if purchase_date.tzinfo is None:
        purchase_date = purchase_date.replace(tzinfo=timezone.utc)
        
    shop_settings = await get_shop_settings()
    timeout_mins = shop_settings.shop_otp_timeout_mins
    
    elapsed = (now - purchase_date).total_seconds()
    if elapsed > (timeout_mins * 60):
        purchase.status = "expired"
        await purchase.save()
        await manager.broadcast({"type": "shop_update"})
        return {
            "id": str(purchase.id),
            "purchase_id": str(purchase.id),
            "status": purchase.status,
            "phone_number": purchase.phone_number,
            "price": purchase.price,
            "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat()
        }

    # Try to fetch OTP
    account = await TelegramAccount.get(ObjectId(purchase.account_id))
    if not account:
         purchase.status = "cancelled"
         await purchase.save()
         return {
            "id": str(purchase.id),
            "purchase_id": str(purchase.id),
            "status": purchase.status,
            "phone_number": purchase.phone_number,
            "price": purchase.price,
            "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat()
        }

    try:
        from app.client_cache import get_client
        client = await get_client(str(account.id), account.session_string, account.api_id, account.api_hash)
        
        # Look for messages from 777000 received AFTER purchase.created_at
        async for message in client.iter_messages(777000, limit=5):
            # Telethon dates are UTC but might be naive depending on library version
            msg_date = message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            
            # Ensure purchase.created_at is also timezone-aware
            purchase_date = purchase.created_at
            if purchase_date.tzinfo is None:
                purchase_date = purchase_date.replace(tzinfo=timezone.utc)

            # 2. OTP RECEIVED! Finalize Purchase
            # Ensure it's a login code message (Telegram uses 'Login code: XXXXX' or similar)
            is_otp = "code" in message.text.lower() or any(char.isdigit() for char in message.text)
            
            if msg_date > purchase_date and is_otp:
                # RE-FETCH USER TO PREVENT BALANCE RACE CONDITIONS
                user = await User.get(ObjectId(current_user.id))
                if not user or user.wallet_balance < purchase.price:
                     purchase.status = "cancelled"
                     await purchase.save()
                     return {
                         "id": str(purchase.id),
                         "status": "cancelled",
                         "phone_number": purchase.phone_number,
                         "price": purchase.price,
                         "detail": "Insufficient balance at final step"
                     }

                # Double check account availability one last time
                account = await TelegramAccount.get(ObjectId(purchase.account_id))
                if not account or account.is_sold:
                     purchase.status = "cancelled"
                     await purchase.save()
                     raise HTTPException(status_code=400, detail="Account was sold to someone else")

                # Deduct Balance
                user.wallet_balance -= purchase.price
                await user.save()

                try:
                    # Transfer Account Ownership
                    account.user_id = str(user.id)
                    account.is_for_sale = False
                    account.is_sold = True
                    account.sold_at = now
                    await account.save()

                    # Log Transaction
                    txn = WalletTransaction(
                        user_id=str(user.id),
                        amount=purchase.price,
                        type="debit",
                        description=f"Purchased account {account.phone_number} (OTP verified)",
                        reference_id=str(account.id),
                        created_at=now
                    )
                    await txn.insert()

                    # Update Purchase Record
                    purchase.status = "success"
                    purchase.otp_message = message.text
                    purchase.otp_received_at = now
                    await purchase.save()
                    
                    # Broadcast Shop Update
                    available_count = await TelegramAccount.find(
                        TelegramAccount.is_for_sale == True,
                        TelegramAccount.is_sold == False
                    ).count()
                    await manager.broadcast({"type": "shop_update", "available_count": available_count})
                
                except Exception as e:
                    print(f"CRITICAL ERROR during account transfer: {e}")
                    # Refund user if account transfer failed
                    user.wallet_balance += purchase.price
                    await user.save()
                    raise HTTPException(status_code=500, detail="Failed to finalize account transfer. Refunded.")
                
                return {
                    "id": str(purchase.id),
                    "purchase_id": str(purchase.id),
                    "status": purchase.status,
                    "phone_number": purchase.phone_number,
                    "price": purchase.price,
                    "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat(),
                    "otp_message": purchase.otp_message
                }
    except Exception as e:
        # Possible connection issue with the account, just continue polling
        pass

    return {
        "id": str(purchase.id),
        "purchase_id": str(purchase.id),
        "status": purchase.status,
        "phone_number": purchase.phone_number,
        "price": purchase.price,
        "created_at": purchase.created_at.isoformat() if purchase.created_at.tzinfo else purchase.created_at.replace(tzinfo=timezone.utc).isoformat()
    }

@router.delete("/cancel/{purchase_id}")
async def cancel_purchase(purchase_id: str, current_user: User = Depends(get_current_user)):
    purchase = await ShopPurchase.get(ObjectId(purchase_id))
    if not purchase or purchase.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Purchase not found")
    
    if purchase.status == "pending":
        purchase.status = "cancelled"
        await purchase.save()
        await manager.broadcast({"type": "shop_update"})
    
    return {"status": "success"}

@router.get("/purchases")
async def list_my_purchases(current_user: User = Depends(get_current_user)):
    purchases = await ShopPurchase.find(
        ShopPurchase.user_id == str(current_user.id)
    ).sort("-created_at").to_list()
    return purchases

@router.get("/admin/settings")
async def get_admin_shop_settings(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await get_shop_settings()

@router.put("/admin/settings")
async def update_admin_shop_settings(req: dict, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    settings = await get_shop_settings()
    if "shop_account_price" in req:
        settings.shop_account_price = float(req["shop_account_price"])
    if "shop_otp_timeout_mins" in req:
        settings.shop_otp_timeout_mins = int(req["shop_otp_timeout_mins"])
        
    await settings.save()
    await manager.broadcast({"type": "shop_update"})
    return settings

# --- Admin Endpoints ---

class SaleToggleRequest(BaseModel):
    is_for_sale: bool
    sale_price: float = 45.0

class AssignmentRequest(BaseModel):
    user_id: str

class BulkSaleRequest(BaseModel):
    account_ids: list[str]
    is_for_sale: bool
    sale_price: float = 45.0

class BulkAssignmentRequest(BaseModel):
    account_ids: list[str]
    user_id: str

@router.put("/admin/bulk/sale")
async def bulk_toggle_sale(req: BulkSaleRequest, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # Convert IDs to ObjectIds
    obj_ids = [ObjectId(aid) for aid in req.account_ids]
    
    await TelegramAccount.find(
        {"_id": {"$in": obj_ids}}
    ).update({"$set": {
        "is_for_sale": req.is_for_sale,
        "sale_price": req.sale_price
    }})
    
    return {"status": "success", "count": len(req.account_ids)}

@router.post("/admin/bulk/assign")
async def bulk_assign_accounts(req: BulkAssignmentRequest, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    obj_ids = [ObjectId(aid) for aid in req.account_ids]
    
    await TelegramAccount.find(
        {"_id": {"$in": obj_ids}}
    ).update({"$set": {
        "user_id": req.user_id,
        "is_for_sale": False,
        "is_sold": False,
        "sold_at": None
    }})
    
    return {"status": "success", "count": len(req.account_ids)}

@router.put("/admin/{account_id}/sale")
async def toggle_account_sale(account_id: str, req: SaleToggleRequest, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    account = await TelegramAccount.get(ObjectId(account_id))
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    account.is_for_sale = req.is_for_sale
    account.sale_price = req.sale_price
    await account.save()
    
    return {"status": "success", "is_for_sale": account.is_for_sale}

@router.post("/admin/{account_id}/assign")
async def assign_account_to_user(account_id: str, req: AssignmentRequest, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    account = await TelegramAccount.get(ObjectId(account_id))
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Transfer ownership
    account.user_id = req.user_id
    account.is_for_sale = False
    account.is_sold = False
    account.sold_at = None
    await account.save()
    
    return {"status": "success", "user_id": account.user_id}
