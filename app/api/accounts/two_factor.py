from fastapi import APIRouter, Depends, HTTPException, Body
from app.api.auth_utils import get_current_user
from app.models import User, TelegramAccount
from app.client_cache import get_client
from telethon import functions, types
from typing import List, Optional
from pydantic import BaseModel
from bson import ObjectId
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/2fa", tags=["2FA Security"])

class Set2FARequest(BaseModel):
    account_ids: List[str]
    new_password: str

class Remove2FARequest(BaseModel):
    account_ids: List[str]

@router.post("/set")
async def set_two_factor(
    req: Set2FARequest, 
    current_user: User = Depends(get_current_user)
):
    """
    Sets or changes the 2FA password for the selected accounts.
    If an account already has a password stored in our DB, we use it as the 'current_password'.
    """
    results = []
    for aid in req.account_ids:
        try:
            acc = await TelegramAccount.find_one(
                TelegramAccount.id == ObjectId(aid), 
                TelegramAccount.user_id == str(current_user.id)
            )
            if not acc:
                results.append({"id": aid, "status": "error", "message": "Account not found"})
                continue
            
            client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash)
            
            # Telethon helper for editing 2FA
            await client.edit_2fa(new_password=req.new_password, current_password=acc.password)
            
            # Update local DB record
            acc.password = req.new_password
            await acc.save()
            
            results.append({
                "id": aid, 
                "phone": acc.phone_number, 
                "status": "success",
                "message": "2FA password updated successfully"
            })
            logger.info(f"[2FA] Successfully updated 2FA for {acc.phone_number}")
            
        except Exception as e:
            logger.error(f"[2FA] Error updating 2FA for {aid}: {str(e)}")
            results.append({
                "id": aid, 
                "phone": acc.phone_number if acc else aid,
                "status": "error", 
                "message": str(e)
            })
            
    return results

@router.post("/remove")
async def remove_two_factor(
    req: Remove2FARequest, 
    current_user: User = Depends(get_current_user)
):
    """Removes 2FA from the selected accounts."""
    results = []
    for aid in req.account_ids:
        try:
            acc = await TelegramAccount.find_one(
                TelegramAccount.id == ObjectId(aid), 
                TelegramAccount.user_id == str(current_user.id)
            )
            if not acc or not acc.password:
                results.append({"id": aid, "status": "error", "message": "Account not found or no password stored"})
                continue
            
            client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash)
            
            # Setting new_password to None removes it
            await client.edit_2fa(new_password=None, current_password=acc.password)
            
            acc.password = None
            await acc.save()
            results.append({"id": aid, "phone": acc.phone_number, "status": "success"})
        except Exception as e:
            results.append({
                "id": aid, 
                "phone": acc.phone_number if acc else aid,
                "status": "error", 
                "message": str(e)
            })
            
    return results
