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

logger = logging.getLogger(__name__)

# { account_id: handler_func }
_attached_handlers = {}

async def attach_handler(client, account_id: str) -> None:
    """Register the auto-reply event handler (idempotent)."""
    if account_id in _attached_handlers:
        return

    @client.on(events.NewMessage(incoming=True))
    async def _handler(event):
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
    try:
        from app.client_cache import get_client, get_account_user_id, is_user_active
        user_id = await get_account_user_id(account_id)
        if user_id == "unknown": return
        
        # Check if user services are active
        if not await is_user_active(user_id):
            return

        # Passing None to get_client will trigger a DB fetch ONLY if not already in RAM cache.
        client = await get_client(account_id)
        if not client: return
        settings = await get_cached_settings(account_id)
        if not settings or not settings.is_enabled: return

        # ── Scope Check ───────────────────────────────────────────────────────
        is_private = event.is_private
        is_group   = event.is_group or event.is_channel

        if is_private and not settings.dm_enabled: return
        if is_group:
            if not settings.group_enabled: return
            if getattr(settings, "group_reply_mode", "all") == "selected":
                if str(event.chat_id) not in getattr(settings, "allowed_group_ids", []): return

        sender_info = f"UID:{event.sender_id}"
        await terminal_manager.log_event(user_id, f"Incoming msg from {sender_info}", account_id, "auto-reply", "INFO")

        logger.info(f"[auto-reply] Incoming from {event.sender_id} | private={is_private}")

        # ── Welcome / Night-Shift Flow (DM only) ─────────────────────────────
        if is_private:
            night_shift_on = getattr(settings, "night_shift_enabled", False)
            night_msg      = getattr(settings, "welcome_message_night", "").strip()

            # ─── NIGHT SHIFT MODE ───────────────────────────────────────────
            if night_shift_on and night_msg:
                is_day = is_daytime(settings)
                if not is_day:
                    if await should_trigger_welcome(client, event.sender_id, event=event):
                        resolved = await resolve_variables(night_msg, event, client, settings)
                        night_media = getattr(settings, "night_tg_media", []) or []
                        await _send_welcome_with_media(
                            client, event, resolved, night_media, settings.default_delay
                        )
                        await mark_read(client, event.chat_id)
                        await terminal_manager.log_event(account.user_id, f"Sent Night-Shift Welcome to {sender_info}", account_id, "auto-reply", "SUCCESS")
                        return  # Normal behavior: block keyword rules at night
                    else:
                        logger.info(f"[auto-reply] Night session already counted for {event.sender_id}")
                    
                    # ─── New Toggle Check ───────────────────────────────────────
                    if not getattr(settings, "night_allow_rules", False):
                        return  # Normal behavior: block keyword rules at night
                    
                    logger.info(f"[auto-reply] NIGHT shift active but rules are ALLOWED for {event.sender_id}")
                else:
                    logger.info(f"[auto-reply] Night Shift ON but it's DAY — running keyword rules for {event.sender_id}")

            # ─── STANDARD WELCOME ───────────────────────────────────────────
            #  Fires when welcome_enabled=True regardless of time (night_shift_off)
            elif settings.welcome_enabled and not night_shift_on:
                if await should_trigger_welcome(client, event.sender_id, event=event):
                    msg      = getattr(settings, "welcome_message", "").strip()
                    tg_media = getattr(settings, "welcome_tg_media", []) or []
                    if msg or tg_media:
                        resolved = await resolve_variables(msg, event, client, settings) if msg else ""
                        await _send_welcome_with_media(
                            client, event, resolved, tg_media, settings.default_delay
                        )
                        await mark_read(client, event.chat_id)
                        await terminal_manager.log_event(account.user_id, f"Sent Standard Welcome to {sender_info}", account_id, "auto-reply", "SUCCESS")
                    # Fall through — also check keyword rules below
                else:
                    logger.info(f"[auto-reply] Welcome cooldown active for {event.sender_id} — running keyword rules")
                    # Fall through to keyword rules below

        # ── Keyword Rule Matching Flow ────────────────────────────────────────
        rules = await get_cached_rules(account_id)
        apply_scope = "dm" if is_private else "group"
        msg_text = (event.raw_text or "").strip()

        for rule in rules:
            if rule.apply_to not in ("both", apply_scope): continue
            if is_group and getattr(rule, "group_reply_mode", "all") == "selected":
                if str(event.chat_id) not in getattr(rule, "allowed_group_ids", []):
                    continue

            if matches_rule(msg_text, rule):
                delay = rule.delay_seconds or settings.default_delay
                if delay > 0: await asyncio.sleep(delay)
                if rule.reply_text:
                    resolved = await resolve_variables(rule.reply_text, event, client, settings)
                    await event.reply(resolved)
                await send_rule_media(client, event, rule)
                await mark_read(client, event.chat_id)
                await terminal_manager.log_event(account.user_id, f"Matched Rule '{rule.name}' for {sender_info}", account_id, "auto-reply", "SUCCESS")
                return

    except Exception as e:
        u_id = account.user_id if 'account' in locals() and account else "unknown"
        await terminal_manager.log_event(u_id, f"Engine Error: {str(e)}", account_id, "auto-reply", "ERROR")
        logger.error(f"[auto-reply] Error processing message: {e}")
