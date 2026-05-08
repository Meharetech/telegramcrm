from datetime import datetime
from telethon import types

def format_status(status):
    if not status:
        return ""
    if isinstance(status, types.UserStatusOnline):
        return "online"
    if isinstance(status, types.UserStatusOffline):
        # status.was_online is a datetime
        # Check if tzinfo exists, if not use naive comparison
        now = datetime.now(status.was_online.tzinfo) if status.was_online.tzinfo else datetime.utcnow()
        diff = now - status.was_online
        
        if diff.days > 0:
            if diff.days == 1: return "last seen yesterday"
            return f"last seen {diff.days} days ago"
        hours = diff.seconds // 3600
        if hours > 0: return f"last seen {hours} hours ago"
        minutes = (diff.seconds % 3600) // 60
        if minutes > 0: return f"last seen {minutes} mins ago"
        return "last seen just now"
    if isinstance(status, types.UserStatusRecently):
        return "last seen recently"
    if isinstance(status, types.UserStatusLastWeek):
        return "last seen last week"
    if isinstance(status, types.UserStatusLastMonth):
        return "last seen last month"
    return ""

async def handle_account_death(account_id: str, account=None, reason: str = "Unknown"):
    """
    Centrally handle an account that has been banned, revoked, or logged out.
    Performs full cleanup: stops background tasks, clears cache, removes from DB.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        from app.models import TelegramAccount
        from app.client_cache import invalidate, _cache
        from app.services.auto_reply.engine import detach_account
        from app.models.auto_reply import AutoReplySettings, AutoReplyRule
        from app.models.forwarder import ForwarderRule

        if not account:
            account = await TelegramAccount.get(account_id)
        
        if not account:
            return False

        logger.warning(f"[cleanup] Removing dead account {account.phone_number} ({account_id}). Reason: {reason}")

        # 1. Stop background services and clear memory cache
        active_client = _cache.get(account_id)
        if active_client:
            await detach_account(active_client, account_id)
        await invalidate(account_id)
        
        # 2. Clean up all related settings/rules
        await AutoReplySettings.find(AutoReplySettings.account_id == account_id).delete()
        await AutoReplyRule.find(AutoReplyRule.account_id == account_id).delete()
        await ForwarderRule.find(ForwarderRule.account_id == account_id).delete()

        # 3. Finally remove from database
        await account.delete()
        return True
    except Exception as e:
        logger.error(f"[cleanup] Critical error during account cleanup for {account_id}: {e}")
        return False
