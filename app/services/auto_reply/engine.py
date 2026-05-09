import asyncio
import logging
from telethon import events
from app.models.auto_reply import AutoReplySettings, AutoReplyRule
from app.models import TelegramAccount
from app.client_cache import get_client
from .logic import is_daytime, should_trigger_welcome, matches_rule, resolve_variables
from .media import send_rule_media, mark_read
from .cache import get_cached_settings, get_cached_rules
from app.services.terminal_service import terminal_manager
from app.services.ai_agent_service import handle_ai_message

logger = logging.getLogger(__name__)

# { account_id: handler_func }
_attached_handlers = {}

async def attach_handler(client, account_id: str) -> None:
    """Register the auto-reply event handler (idempotent)."""
    if account_id in _attached_handlers:
        return

    @client.on(events.NewMessage(incoming=True))
    async def _handler(event):
        logger.debug(f"[auto-reply] Event triggered for {account_id}")
        await process_message_event(event, account_id)

    _attached_handlers[account_id] = _handler
    logger.info(f"[auto-reply] Handler attached: {account_id}")

async def detach_account(client, account_id: str):
    """Cleanly remove the auto-reply handler from the client."""
    handler = _attached_handlers.pop(account_id, None)
    if handler and client:
        try:
            client.remove_event_handler(handler)
            logger.info(f"[auto-reply] Handler detached: {account_id}")
        except Exception as e:
            logger.warning(f"[auto-reply] Error detaching handler for {account_id}: {e}")


async def _send_welcome_with_media(client, event, text: str, tg_media: list, delay: int):
    """
    Send a welcome/night message, optionally with attached Telegram-hosted media.
    Media is resolved from Saved Messages using the stored msg_id reference.
    """
    if delay > 0:
        await asyncio.sleep(delay)

    sent_text = False

    if tg_media:
        for m_item in tg_media:
            if not isinstance(m_item, dict):
                continue
            media_ref = m_item.get("media")
            caption   = m_item.get("caption", "") or ""

            # Resolve Telegram-hosted media (saved_msg reference)
            if isinstance(media_ref, dict) and media_ref.get("type") == "saved_msg":
                try:
                    saved = await client.get_messages("me", ids=int(media_ref["msg_id"]))
                    if saved and saved.media:
                        # Use caption from media item; fall back to the welcome text
                        file_caption = caption or (text if not sent_text else "")
                        await client.send_file(
                            entity=event.chat_id,
                            file=saved.media,
                            caption=file_caption,
                            reply_to=event.message.id
                        )
                        sent_text = True  # text consumed as first media's caption
                except Exception as me:
                    logger.error(f"[auto-reply] Failed to send welcome media: {me}")

    # Send text separately if it wasn't used as a media caption
    if text and not sent_text:
        await event.reply(text)


async def process_message_event(event, account_id: str):
    """Main execution flow for an incoming message."""
    user_id = "unknown"
    try:
        from app.client_cache import get_client, get_account_user_id, is_user_active
        from datetime import datetime
        import zoneinfo

        user_id = await get_account_user_id(account_id)
        if user_id == "unknown": 
            return
        
        # Check if user services are active
        if not await is_user_active(user_id):
            return

        client = await get_client(account_id)
        if not client: 
            await terminal_manager.log_event(user_id, f"❌ Engine Error: Telegram client not found.", account_id, "auto-reply", "ERROR")
            return
        
        # ── AI Agent Flow FIRST (bypasses master is_enabled check) ──────────
        # AI Agent is independent of the keyword auto-reply system.
        ai_replied = await handle_ai_message(account_id, event, client)
        if ai_replied:
            return
            
        settings = await get_cached_settings(account_id)

        if not settings or not settings.is_enabled:
            return

        # ── Timezone Handling ────────────────────────────────────────────────
        tz_str = getattr(settings, "timezone", "Asia/Kolkata")
        tz = zoneinfo.ZoneInfo(tz_str)
        now_tz = datetime.now(tz)
        time_str = now_tz.strftime("%I:%M %p") # e.g. 10:51 PM
        
        # ── Scope Check ───────────────────────────────────────────────────────
        is_private = event.is_private
        is_group   = event.is_group or event.is_channel
        
        # Only log incoming if it passes basic master switch (to avoid spamming logs with ignored group messages)
        if is_group and not settings.group_enabled:
            # Silently ignore if disabled globally to keep terminal clean
            return
        
        if is_private and not settings.dm_enabled:
            return

        sender_info = f"UID:{event.sender_id}"
        await terminal_manager.log_event(user_id, f"📩 [{time_str}] Incoming msg from {sender_info}", account_id, "auto-reply", "INFO")

        # ── Keyword Rule Matching Flow ────────────────────────────────────────
        rules = await get_cached_rules(account_id)
        msg_text = (event.raw_text or "").strip()
        apply_scope = "dm" if is_private else "group"
        
        rule_matched = False
        if rules:
            for rule in rules:
                # 1. Scope check
                if rule.apply_to not in ("both", apply_scope):
                    continue
                
                # 2. Group whitelist check
                if is_group and getattr(rule, "group_reply_mode", "all") == "selected":
                    if str(event.chat_id) not in getattr(rule, "allowed_group_ids", []):
                        continue

                # 3. Match check
                if matches_rule(msg_text, rule):
                    await terminal_manager.log_event(user_id, f"🎯 Matched: '{rule.name}'", account_id, "auto-reply", "SUCCESS")
                    
                    delay = rule.delay_seconds if rule.delay_seconds is not None else settings.default_delay
                    if delay > 0: 
                        await asyncio.sleep(delay)
                    
                    reply_sent = False
                    # Text reply
                    if rule.reply_text:
                        try:
                            # Show typing action to look more natural
                            async with client.action(event.chat_id, 'typing'):
                                resolved = await resolve_variables(rule.reply_text, event, client, settings)
                                await client.send_message(event.chat_id, resolved, reply_to=event.id)
                                reply_sent = True
                        except Exception as re:
                            await terminal_manager.log_event(user_id, f"❌ Reply Error: {str(re)}", account_id, "auto-reply", "ERROR")
                    
                    # Media reply
                    if rule.tg_media or rule.media_paths:
                        try:
                            await send_rule_media(client, event, rule)
                            reply_sent = True
                        except Exception as me:
                            await terminal_manager.log_event(user_id, f"❌ Media Error: {str(me)}", account_id, "auto-reply", "ERROR")

                    if reply_sent:
                        await terminal_manager.log_event(user_id, f"📤 Auto-Reply Sent ({rule.name})", account_id, "auto-reply", "SUCCESS")
                        await mark_read(client, event.chat_id)
                        return # STOP after first match
                    
                    rule_matched = True
                    break

        # ── Night Shift / Welcome Flow (Only if no keyword matched) ──────────
        if is_private and not rule_matched:
            night_shift_on = getattr(settings, "night_shift_enabled", False)
            is_day = is_daytime(settings)
            
            if night_shift_on:
                if not is_day:
                    # NIGHT TIME
                    night_msg = getattr(settings, "welcome_message_night", "").strip()
                    if night_msg and await should_trigger_welcome(client, event.sender_id, event=event):
                        resolved = await resolve_variables(night_msg, event, client, settings)
                        night_media = getattr(settings, "night_tg_media", []) or []
                        await terminal_manager.log_event(user_id, f"🌙 Night Shift active. Sending welcome...", account_id, "auto-reply", "DEBUG")
                        await _send_welcome_with_media(client, event, resolved, night_media, settings.default_delay)
                        await mark_read(client, event.chat_id)
                        await terminal_manager.log_event(user_id, f"🌙 Sent Night Welcome to {sender_info}", account_id, "auto-reply", "SUCCESS")
                        return
                    
                    # If we reached here in Night Shift, and it wasn't a welcome msg, 
                    # and no rule matched, we just ignore it (or return).
                    return
                else:
                    await terminal_manager.log_event(user_id, f"☀️ Night Shift enabled but it's currently Day window.", account_id, "auto-reply", "DEBUG")

            # Standard Welcome
            if settings.welcome_enabled:
                if await should_trigger_welcome(client, event.sender_id, event=event):
                    msg = getattr(settings, "welcome_message", "").strip()
                    tg_media = getattr(settings, "welcome_tg_media", []) or []
                    if msg or tg_media:
                        resolved = await resolve_variables(msg, event, client, settings)
                        await _send_welcome_with_media(client, event, resolved, tg_media, settings.default_delay)
                        await mark_read(client, event.chat_id)
                        await terminal_manager.log_event(user_id, f"👋 Sent Standard Welcome to {sender_info}", account_id, "auto-reply", "SUCCESS")
                        return


    except Exception as e:
        await terminal_manager.log_event(user_id, f"⚠️ Engine Error: {str(e)}", account_id, "auto-reply", "ERROR")
        logger.error(f"[auto-reply] Error: {e}", exc_info=True)

