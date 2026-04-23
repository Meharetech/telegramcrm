from fastapi import APIRouter, HTTPException, Depends
from app.models import User, TelegramAccount
from app.api.auth_utils import get_current_user
from app.client_cache import get_client
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/otp/{account_id}")
async def get_latest_otp(account_id: str, current_user: User = Depends(get_current_user)):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Security check: only owner or admin can view OTP
    if account.user_id != str(current_user.id) and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)
        
        # 777000 is the official Telegram message sender
        async for message in client.iter_messages(777000, limit=1):
            return {
                "phone": account.phone_number,
                "message": message.text,
                "date": message.date.isoformat() if message.date else None
            }
        
        return {"phone": account.phone_number, "message": "No OTP message found."}

    except Exception as e:
        logger.error(f"Error fetching OTP for {account.phone_number}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
