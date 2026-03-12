import logging
from fastapi import APIRouter
from app.models import TelegramAccount
from app.client_cache import get_client
from app.services.auto_reply import attach_handler

router = APIRouter()

@router.post("/activate/{account_id}")
async def activate_worker(account_id: str):
    """Attach the auto-reply event handler to an account's cached client."""
    await _activate_worker(account_id)
    return {"status": "activated"}

async def _activate_worker(account_id: str):
    """Internal helper: get cached client and attach auto-reply handler."""
    try:
        account = await TelegramAccount.get(account_id)
        if not account:
            return

        client = await get_client(
            account_id,
            account.session_string,
            account.api_id,
            account.api_hash,
            device_model=getattr(account, 'device_model', 'Telegram Android')
        )
        await attach_handler(client, account_id)
    except Exception as e:
        logging.getLogger(__name__).warning(f"[auto-reply] Activate failed: {e}")
