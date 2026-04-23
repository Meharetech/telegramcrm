from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.models import User, TelegramAccount, WalletTransaction
from app.api.auth_utils import get_current_user
from bson import ObjectId
from datetime import datetime, timezone

router = APIRouter()

@router.get("/accounts")
async def list_accounts_for_sale(current_user: User = Depends(get_current_user)):
    # List accounts where is_for_sale is True and not already sold
    accounts = await TelegramAccount.find(
        TelegramAccount.is_for_sale == True,
        TelegramAccount.is_sold == False
    ).to_list()
    
    return [{
        "id": str(a.id),
        "phone_number": a.phone_number[:5] + "****" + a.phone_number[-2:],
        "sale_price": a.sale_price,
        "device_model": a.device_model,
        "created_at": a.created_at
    } for a in accounts]

@router.post("/buy/{account_id}")
async def buy_account(account_id: str, current_user: User = Depends(get_current_user)):
    account = await TelegramAccount.get(ObjectId(account_id))
    
    if not account or not account.is_for_sale or account.is_sold:
        raise HTTPException(status_code=404, detail="Account not available for sale")
    
    if current_user.wallet_balance < account.sale_price:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance. Please top up.")

    # Process Transaction
    current_user.wallet_balance -= account.sale_price
    await current_user.save()

    # Process Account Transfer
    account.user_id = str(current_user.id)
    account.is_for_sale = False
    account.is_sold = True
    account.sold_at = datetime.now(timezone.utc)
    await account.save()

    # Log Transaction
    txn = WalletTransaction(
        user_id=str(current_user.id),
        amount=account.sale_price,
        type="debit",
        description=f"Purchased Telegram account: {account.phone_number}",
        reference_id=str(account.id),
        created_at=datetime.now(timezone.utc)
    )
    await txn.insert()

    return {"status": "success", "message": "Account purchased successfully", "account_id": str(account.id)}

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
