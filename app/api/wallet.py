from fastapi import APIRouter, HTTPException, Depends, status
from app.models import User, WalletTransaction
from app.api.auth_utils import get_current_user
from typing import List
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime, timezone

router = APIRouter()

class WalletAdjustment(BaseModel):
    user_id: str
    amount: float
    description: str
    type: str # "credit" or "debit"

@router.get("/balance")
async def get_balance(current_user: User = Depends(get_current_user)):
    return {
        "balance": current_user.wallet_balance
    }

@router.get("/transactions")
async def get_transactions(current_user: User = Depends(get_current_user)):
    transactions = await WalletTransaction.find(
        WalletTransaction.user_id == str(current_user.id)
    ).sort("-created_at").to_list()
    return transactions

# --- Admin Endpoints ---

@router.post("/admin/adjust")
async def adjust_balance(req: WalletAdjustment, current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    target_user = await User.get(ObjectId(req.user_id))
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if req.type == "credit":
        target_user.wallet_balance += req.amount
    elif req.type == "debit":
        if target_user.wallet_balance < req.amount:
            raise HTTPException(status_code=400, detail="Insufficient user balance")
        target_user.wallet_balance -= req.amount
    else:
        raise HTTPException(status_code=400, detail="Invalid adjustment type")
    
    await target_user.save()
    
    # Log transaction
    txn = WalletTransaction(
        user_id=str(target_user.id),
        amount=req.amount,
        type=req.type,
        description=req.description,
        created_at=datetime.now(timezone.utc)
    )
    await txn.insert()
    
    return {"status": "success", "new_balance": target_user.wallet_balance}
