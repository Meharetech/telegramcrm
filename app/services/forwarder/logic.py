"""
forwarder/logic.py — Message forwarding service.

FIXES applied vs original:
  1. FIX: Closure bug — `rule` inside `forward_handler` was captured by reference.
     When `start_forwarder_for_account` loops over rules, the `rule` variable in 
     every previously-defined handler would point to the LAST rule in the loop by
     the time any handler fires. Fixed by passing rule as a default argument.
  2. FIX: All `print()` calls replaced with `logger.*` for proper log levels and
     aggregation (print() does not appear in server logs with uvicorn workers).
  3. FIX: `word` key in replacements was `find` in model but `word` checked here —
     now consistently uses `find` key matching the ForwarderRule model schema.
"""

import asyncio
import random
import logging
import re
from telethon import events
from telethon.tl.types import MessageMediaWebPage
from app.client_cache import get_client
from app.models.forwarder import ForwarderRule
from app.models import TelegramAccount
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

# { account_id: { rule_id: handler } }
_attached_handlers: dict = {}


async def start_forwarder_for_account(account_id: str):
    """
    Find all forwarder rules for this account and (re-)attach their handlers.
    Disabled rules have their handlers removed if previously attached.
    """
    rules = await ForwarderRule.find(
        ForwarderRule.account_id == account_id
    ).to_list()

    enabled_count = sum(1 for r in rules if r.is_enabled)
    logger.info(f"[forwarder] {enabled_count} enabled rule(s) for account {account_id}")

    account = await TelegramAccount.get(account_id)
    if not account or not account.session_string:
        logger.warning(f"[forwarder] Account {account_id} not found or has no session — skipping")
        return

    client = await get_client(
        account_id, account.session_string, account.api_id, account.api_hash,
        device_model=getattr(account, "device_model", "Telegram Android"),
    )

    if account_id not in _attached_handlers:
        _attached_handlers[account_id] = {}

    for rule in rules:
        if not rule.is_enabled:
            # Remove handler for any previously-active disabled rule
            rule_id = str(rule.id)
            if rule_id in _attached_handlers[account_id]:
                try:
                    client.remove_event_handler(_attached_handlers[account_id][rule_id])
                except Exception:
                    pass
                del _attached_handlers[account_id][rule_id]
                logger.info(f"[forwarder] Detached DISABLED rule: {rule.name}")
            continue

        await _attach_rule_handler(client, account_id, rule)


async def _attach_rule_handler(client, account_id: str, rule: ForwarderRule):
    """Attach (or re-attach after update) the Telethon event handler for one rule."""
    from app.client_cache import get_account_user_id, is_user_active
    user_id = await get_account_user_id(account_id)
    if user_id == "unknown": return
    
    rule_id = str(rule.id)

    # Remove old handler if this rule was already registered (e.g. after an update)
    if rule_id in _attached_handlers.get(account_id, {}):
        try:
            client.remove_event_handler(_attached_handlers[account_id][rule_id])
        except Exception:
            pass
        logger.debug(f"[forwarder] Removed old handler for updated rule: {rule.name}")

    async def _resolve_peer(peer_str: str):
        """
        Intelligently resolve a peer identifier.
        Handles channel IDs missing '-100' prefix and ensures Telethon has the entity cached.
        """
        # 1. Clean up common formatting issues
        p = peer_str.strip()
        
        # 2. Try to convert to integer if it looks like one
        is_neg = p.startswith("-")
        digits = p.lstrip("-")
        if digits.isdigit():
            val = int(p)
            # Optimization: If it's a 10-digit negative number, it's likely a channel missing '-100'
            # (Channel IDs in Telegram are often 10 digits; MTProto expects -100 prefix)
            if is_neg and not p.startswith("-100") and len(digits) >= 9:
                try:
                    alt_val = int("-100" + digits)
                    return await client.get_entity(alt_val)
                except Exception:
                    pass # Fall back to original
            
            try:
                return await client.get_entity(val)
            except Exception:
                # Last ditch effort for positive IDs that might be channels (Telethon sometimes wants int)
                if not is_neg and len(digits) >= 9:
                     try:
                         return await client.get_entity(int("-100" + digits))
                     except: pass
                raise
        
        # 3. Handle usernames (@username) or invite links
        try:
            return await client.get_entity(p)
        except Exception:
            # Final fallback: return the original string or int 
            # Telethon might still be able to use it if it's already in the internal cache
            return int(p) if digits.isdigit() else p

    # ── PERSISTENT ENTITY CACHE (SMOOTH SCALING) ──────────────────────────────
    # Pre-resolve peers once during attachment; avoid _resolve_peer in the loop.
    try:
        source_peer = await _resolve_peer(rule.source_id)
        # Pre-resolve all targets as well
        target_peers = {}
        for t_id in rule.target_ids:
            try:
                target_peers[t_id] = await _resolve_peer(t_id)
            except Exception as e:
                logger.warning(f"[forwarder] Could not pre-resolve target {t_id}: {e}")
    except Exception as e:
        logger.error(f"[forwarder] Failed to resolve source {rule.source_id}: {e}")
        return

    logger.info(f"[forwarder] Rule '{rule.name}' ACTIVE. Source: {getattr(source_peer, 'title', rule.source_id)}")

    @client.on(events.NewMessage(chats=source_peer))
    async def forward_handler(event, _rule=rule, _source=source_peer, _targets=target_peers):
        # Check if user services are active
        if not await is_user_active(user_id):
            return
        try:
            msg_text = event.message.text or ""
            
            # 1. Filters (Keyword / Blacklist)
            if _rule.keyword_filters and not any(kw.lower() in msg_text.lower() for kw in _rule.keyword_filters): return
            if _rule.blacklist_keywords and any(kw.lower() in msg_text.lower() for kw in _rule.blacklist_keywords): return

            # 2. Timing (Anti-flood)
            delay = random.randint(_rule.min_delay, _rule.max_delay)
            await terminal_manager.log_event(user_id, f"Rule '{_rule.name}' triggered. Waiting {delay}s...", account_id, "forwarder", "INFO")
            await asyncio.sleep(delay)

            source_name = getattr(event.chat, 'title', str(event.chat_id))

            # 3. Execution (Using Cached Peers)
            for t_id, t_peer in _targets.items():
                try:
                    target_name = getattr(t_peer, 'title', str(t_id))

                    if _rule.forward_mode == "forward":
                        await client.forward_messages(t_peer, event.message)
                        await terminal_manager.log_event(user_id, f"Forwarded: '{source_name}' -> '{target_name}' (Rule: {_rule.name})", account_id, "forwarder", "SUCCESS")
                    else:
                        caption = msg_text
                        if _rule.word_replacements:
                            for rep in _rule.word_replacements:
                                w_find = rep.get("find", rep.get("word", ""))
                                w_repl = rep.get("replace", "")
                                if w_find: caption = caption.replace(w_find, w_repl)

                        if _rule.replace_usernames: caption = re.sub(r"@[\w]+", _rule.replace_usernames, caption)
                        if _rule.replace_links: caption = re.sub(r"https?://[^\s]+", _rule.replace_links, caption)
                        if not _rule.remove_caption and _rule.add_custom_text: caption = f"{caption}\n\n{_rule.add_custom_text}"
                        elif _rule.remove_caption: caption = _rule.add_custom_text or ""

                        # Send with pre-resolved t_peer
                        await client.send_message(t_peer, caption, file=event.message.media, parse_mode="html")
                        await terminal_manager.log_event(user_id, f"Copied: '{source_name}' -> '{target_name}' (Rule: {_rule.name})", account_id, "forwarder", "SUCCESS")

                except Exception as e:
                    logger.error(f"[forwarder] Failed to send to {target}: {e}")
                    await terminal_manager.log_event(user_id, f"Target {target} Error: {str(e)}", account_id, "forwarder", "ERROR")

        except Exception as e:
            logger.error(f"[forwarder] Unhandled error in rule '{_rule.name}': {e}")
            await terminal_manager.log_event(user_id, f"Forwarder Unhandled Error: {str(e)}", account_id, "forwarder", "ERROR")

    _attached_handlers[account_id][rule_id] = forward_handler
    logger.info(f"[forwarder] Rule '{rule.name}' is ACTIVE for account {account_id}")


# Keep the old public name for backward compatibility with forwarder.py API
async def attach_rule_handler(client, account_id: str, rule: ForwarderRule):
    await _attach_rule_handler(client, account_id, rule)


async def stop_forwarder_for_rule(account_id: str, rule_id: str):
    """Detach and remove the handler for a deleted/stopped rule."""
    if account_id not in _attached_handlers or rule_id not in _attached_handlers[account_id]:
        return

    account = await TelegramAccount.get(account_id)
    if account and account.session_string:
        try:
            client = await get_client(
                account_id, account.session_string, account.api_id, account.api_hash,
                device_model=getattr(account, "device_model", "Telegram Android"),
            )
            client.remove_event_handler(_attached_handlers[account_id][rule_id])
        except Exception as e:
            logger.warning(f"[forwarder] Could not remove handler for rule {rule_id}: {e}")

    del _attached_handlers[account_id][rule_id]
    logger.info(f"[forwarder] Detached rule {rule_id} for account {account_id}")

async def stop_all_forwarders_for_account(account_id: str):
    """Remove all forwarder handlers for an account."""
    if account_id not in _attached_handlers:
        return

    account = await TelegramAccount.get(account_id)
    if not account or not account.session_string:
        return

    try:
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)
        for rule_id, handler in list(_attached_handlers[account_id].items()):
            try:
                client.remove_event_handler(handler)
            except Exception: pass
        
        del _attached_handlers[account_id]
        logger.info(f"[forwarder] All rules detached for account {account_id}")
    except Exception as e:
        logger.error(f"[forwarder] Failed to stop all forwarders for {account_id}: {e}")
