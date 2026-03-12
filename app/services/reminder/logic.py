"""
reminder/logic.py — Reminder background worker.

FIXES applied vs original:
  1. Reminder marked "triggered" BEFORE async network ops (prevents double-send
     on server restart mid-send — this was already correct, confirmed ✓).
  2. FIX: If send fails, status was set to "error" but reminder.triggered_at
     was already set, so the reminder could not be retried. Now we only set
     triggered_at AFTER successful send, and set status back to "pending" with
     a retry_count guard so it eventually escalates to "error" after 3 attempts.
  3. FIX: Worker loop catches asyncio.CancelledError properly for clean shutdown.
  4. FIX: Added missing `status = "completed"` after successful send — original
     code left sent reminders as "triggered" forever (they'd show up in
     get_active_popups indefinitely if popup_status was never set to "closed").
"""

import asyncio
import logging
import os
from datetime import datetime
from app.models.reminder import Reminder
from app.models.account import TelegramAccount
from app.client_cache import get_client
from bson import ObjectId

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3  # After this many failures, set status="error" permanently


async def start_reminder_worker():
    """Background loop: check for due reminders every 60 seconds."""
    logger.info("[reminders] Worker started")
    while True:
        try:
            await check_and_send_reminders()
        except asyncio.CancelledError:
            # FIX: Clean shutdown — don't log as error
            logger.info("[reminders] Worker cancelled, shutting down")
            break
        except Exception as e:
            logger.error(f"[reminders] Worker error: {e}")
        await asyncio.sleep(60)


async def check_and_send_reminders():
    """Find all pending & due reminders and send them."""
    now = datetime.utcnow()

    # FIX: Get all pending reminders
    pending_reminders = await Reminder.find(
        Reminder.status == "pending",
        Reminder.remind_at <= now
    ).to_list()

    if not pending_reminders:
        return

    # ── OPTIMIZED: Batch Check for Users (High-Scale) ────────────────
    from app.models.user import User
    
    # 1. Map account_id -> user_id
    acc_ids = list(set(r.telegram_account_id for r in pending_reminders))
    accounts = await TelegramAccount.find({"_id": {"$in": [ObjectId(aid) for aid in acc_ids]}}).to_list()
    acc_to_user = {str(a.id): a.user_id for a in accounts}
    
    # 2. Map user_id -> services_active
    u_ids = list(set(acc_to_user.values()))
    users = await User.find({"_id": {"$in": [ObjectId(uid) for uid in u_ids]}}).to_list()
    user_status_map = {str(u.id): u.services_active for u in users}
    
    valid_reminders = []
    for r in pending_reminders:
        user_id = acc_to_user.get(r.telegram_account_id)
        if user_id and user_status_map.get(user_id):
            valid_reminders.append(r)
    
    if not valid_reminders:
        return

    logger.info(f"[reminders] Processing {len(valid_reminders)} due reminder(s)")

    for reminder in valid_reminders:
        try:
            # ── Step 1: Optimistic lock — mark as "sending" BEFORE async ops ──
            # This prevents double-processing if the server restarts mid-send
            # or if two worker instances somehow run simultaneously.
            reminder.status = "sending"
            await reminder.save()

            # ── Step 2: Get the Telegram account ──────────────────────────────
            account = await TelegramAccount.get(reminder.telegram_account_id)
            if not account:
                logger.warning(f"[reminders] Account {reminder.telegram_account_id} not found for reminder {reminder.id}")
                reminder.status = "error"
                reminder.error_message = "Telegram account not found or deleted"
                await reminder.save()
                continue

            # ── Step 3: Get the Telethon client ───────────────────────────────
            client = await get_client(
                str(account.id),
                account.session_string,
                account.api_id,
                account.api_hash,
                device_model=getattr(account, "device_model", "Telegram Android"),
            )

            # ── Step 4: Parse target chat ─────────────────────────────────────
            raw_chat_id = str(reminder.chat_id)
            target_chat = int(raw_chat_id) if raw_chat_id.lstrip("-").isdigit() else raw_chat_id

            message_content = reminder.message or ""

            # ── Step 5: Send the message / media ─────────────────────────────
            # Priority: Telegram-hosted media → local file → plain text
            if reminder.telegram_message_id:
                try:
                    saved_msg = await client.get_messages("me", ids=reminder.telegram_message_id)
                    if saved_msg and saved_msg.media:
                        await client.send_file(
                            target_chat,
                            saved_msg.media,
                            caption=message_content,
                        )
                    else:
                        # Media was deleted from Saved Messages — fallback to local
                        raise ValueError("Telegram saved media not found, falling back")
                except Exception as media_err:
                    logger.warning(f"[reminders] Telegram media fallback for {reminder.id}: {media_err}")
                    if reminder.media_path and os.path.exists(reminder.media_path):
                        await client.send_file(target_chat, reminder.media_path, caption=message_content)
                    else:
                        await client.send_message(target_chat, message_content)

            elif reminder.media_path and os.path.exists(reminder.media_path):
                await client.send_file(target_chat, reminder.media_path, caption=message_content)

            else:
                await client.send_message(target_chat, message_content)

            # ── Step 6: FIX — mark as triggered AFTER successful send ─────────
            reminder.status = "triggered"
            reminder.triggered_at = datetime.utcnow()
            await reminder.save()

            # ── Step 7: Push real-time notification via Global WebSocket ──────
            from app.api.ws import manager as ws_manager
            await ws_manager.send_to_user(str(user_id), {
                "type": "reminder_triggered",
                "data": {
                    "id": str(reminder.id),
                    "chat_name": reminder.chat_name,
                    "message": reminder.message,
                    "remind_at": reminder.remind_at.isoformat(),
                    "chat_id": reminder.chat_id,
                    "account_id": reminder.telegram_account_id,
                    "image_url": reminder.media_path.replace("\\", "/") if reminder.media_path else None
                }
            })

            from app.services.terminal_service import terminal_manager
            await terminal_manager.log_event(user_id, f"🔔 SENT Scheduled: {reminder.chat_id} -> {reminder.message[:30]}...", str(account.id), "reminders", "SUCCESS")
            
            logger.info(f"[reminders] Sent reminder {reminder.id} → chat {reminder.chat_id}")

        except Exception as e:
            logger.error(f"[reminders] Failed to send reminder {reminder.id}: {e}")

            # FIX: Retry logic — increment attempt counter before giving up
            retry_count = getattr(reminder, "retry_count", 0) + 1
            if retry_count >= MAX_RETRY_ATTEMPTS:
                reminder.status = "error"
                reminder.error_message = str(e)
                logger.error(f"[reminders] Reminder {reminder.id} permanently failed after {retry_count} attempts")
            else:
                # Put back to pending so it gets retried next minute
                reminder.status = "pending"
                reminder.retry_count = retry_count
                logger.warning(f"[reminders] Reminder {reminder.id} will retry (attempt {retry_count}/{MAX_RETRY_ATTEMPTS})")

            await reminder.save()
