import logging
from typing import Dict
from telethon import TelegramClient, events
from telethon.sessions import MemorySession
from app.config import settings
from app.models.bot_forwarder import BotForwarder

logger = logging.getLogger(__name__)

# { bot_id: TelegramClient }
_active_bots: Dict[str, TelegramClient] = {}

async def init_bots_on_startup():
    """Start all enabled bots for active users when the server starts."""
    from app.models.user import User
    import asyncio
    import random

    enabled_bots = await BotForwarder.find(BotForwarder.is_enabled == True).to_list()
    logger.info(f"[bot-service] Initializing {len(enabled_bots)} active bots...")
    
    for idx, bot in enumerate(enabled_bots):
        try:
            # Audit Check: Only start if user has system services active
            user = await User.get(bot.user_id)
            if not user or not user.services_active:
                continue
            
            # Stagger jitter to prevent FloodWait login storm on IP
            if idx > 0: await asyncio.sleep(random.uniform(2.0, 5.0))
            
            await start_bot(bot)
        except Exception as e:
            logger.error(f"[bot-service] Startup failure for {bot.name}: {e}")

async def start_bot(bot: BotForwarder):
    """Initialize and connect a bot client with reuse logic."""
    bot_id = str(bot.id)
    
    # ── Client Reuse Logic ─────────────────────────────────────────────
    if bot_id in _active_bots:
        existing_client = _active_bots[bot_id]
        if existing_client.is_connected():
            # Check if token is the same — if so, just keep using it
            # (In Telethon we can't easily check the token from the client, 
            # so we assume it's the same unless stopped/deleted)
            logger.info(f"[bot-service] Reusing active session for bot {bot.name}")
            return

    # Ensure sessions directory exists (fallback for other logic)
    import os
    os.makedirs("sessions", exist_ok=True)

    # 1. Try Global Settings
    api_id = settings.DEFAULT_API_ID
    api_hash = settings.DEFAULT_API_HASH

    # 2. Try User-specific Credentials
    if not api_id or not api_hash:
        from app.models.telegram_api import TelegramAPI
        creds = await TelegramAPI.find_one(TelegramAPI.user_id == bot.user_id)
        if creds:
            api_id = creds.api_id
            api_hash = creds.api_hash

    if not api_id or not api_hash:
        logger.error(f"[bot-service] API_ID/HASH missing for user {bot.user_id} — cannot start bot {bot.name}")
        return

    # 3. Resolve Proxy if assigned
    proxy_config = None
    if bot.proxy_id and bot.proxy_id.strip():
        from app.models.proxy import Proxy
        p = await Proxy.get(bot.proxy_id)
        if p:
            # Canonicalize protocol for Telethon/Socks compatibility
            proto = p.protocol.lower().replace('socks5', 'socks5') # ensure clean string
            proxy_config = {
                'proxy_type': proto, 
                'addr': p.host,
                'port': p.port,
                'username': p.username,
                'password': p.password,
                'rdns': True 
            }

    # Use MemorySession to avoid SQLite path issues on Windows
    client = TelegramClient(
        MemorySession(),
        api_id,
        api_hash,
        proxy=proxy_config
    )

    from telethon.errors.rpcerrorlist import FloodWaitError
    from datetime import datetime, timedelta, timezone
    from app.services.terminal_service import terminal_manager

    # 4. Check if currently locked by FloodWait
    flood_until = bot.flood_wait_until
    if flood_until:
        if flood_until.tzinfo is None:
            flood_until = flood_until.replace(tzinfo=timezone.utc)
        
        now_utc = datetime.now(timezone.utc)
        if flood_until > now_utc:
            wait_sec = int((flood_until - now_utc).total_seconds())
            await terminal_manager.log_event(str(bot.user_id), f"⏳ {bot.name} is LOCKED by Telegram for another {wait_sec}s. Skipping start.", bot_id, "bot_hub", "WARNING")
            return

    try:
        # This is where the FloodWait occurs if called too often
        await client.start(bot_token=bot.bot_token)
        
        # Clear any existing flood wait upon successful start
        if bot.flood_wait_until:
            bot.flood_wait_until = None
            await bot.save()

        _active_bots[bot_id] = client
        _attach_bot_handlers(client, bot)
        
        await terminal_manager.log_event(str(bot.user_id), f"🤖 Bot Hub agent {bot.name} is now ONLINE.", bot_id, "bot_hub", "SUCCESS")
        
        # ── Startup Notification to Admins ──
        try:
            startup_msg = f"🚀 **Bot Forwarder '{bot.name}' is now ONLINE** and ready to forward messages."
            for admin in bot.admin_usernames:
                try:
                    await client.send_message(admin.strip(), startup_msg)
                except Exception:
                    # Ignore if admin username is unreachable at start
                    pass
        except Exception as startup_err:
            logger.warning(f"Failed to send startup notification for {bot.name}: {startup_err}")

        logger.info(f"[bot-service] Bot {bot.name} (ID: {bot_id}) is ONLINE.")
        
    except FloodWaitError as e:
        # ── EXTREMELY IMPORTANT: Catch and Store FloodWait ──
        unlock_time = datetime.now(timezone.utc) + timedelta(seconds=e.seconds)
        bot.flood_wait_until = unlock_time
        await bot.save()
        
        await terminal_manager.log_event(str(bot.user_id), f"❌ Bot {bot.name} triggered FloodWait! Wait {e.seconds}s required.", bot_id, "bot_hub", "ERROR")
        logger.error(f"[bot-service] Bot {bot.name} rate-limited for {e.seconds}s")
        
    except Exception as e:
        logger.error(f"[bot-service] Critical error starting bot {bot.name}: {e}")
        if client.is_connected():
            await client.disconnect()
        raise e

async def stop_bot(bot_id: str):
    """Disconnect and remove a bot client."""
    if bot_id in _active_bots:
        client = _active_bots.pop(bot_id)
        try:
            await client.disconnect()
            logger.info(f"[bot-service] Bot {bot_id} disconnected.")
        except Exception as e:
            logger.error(f"[bot-service] Error disconnecting bot {bot_id}: {e}")

def _attach_bot_handlers(client: TelegramClient, bot: BotForwarder):
    """Attach the core forwarding engine to the bot client."""
    
    @client.on(events.NewMessage)
    async def bot_forward_handler(event):
        try:
            bot_id = str(bot.id)
            rule = await BotForwarder.get(bot_id)
            if not rule or not rule.is_enabled:
                return 

            # 2. Safety Valve: Only process if User Terminal is STARTED
            from app.models.user import User
            user = await User.get(rule.user_id)
            if not user or not user.services_active:
                logger.debug(f"[bot-service] System Terminal for {rule.user_id} is OFFLINE. Ignoring message.")
                return

            # 3. Authorization Check (Sender Username)
            sender_id = event.sender_id
            sender = await event.get_sender()
            sender_username = getattr(sender, 'username', None)
            
            logger.info(f"[bot-service] Message received by bot {rule.name} from ID: {sender_id} (@{sender_username})")

            auth_list = [u.lstrip('@').lower().strip() for u in rule.admin_usernames]
            is_auth = (sender_username and sender_username.lower() in auth_list)
            
            if not is_auth:
                logger.debug(f"[bot-service] Unauthorized sender {sender_username} for bot {rule.name}")
                return

            # ── COMMAND HANDLER: /start ───────────────────────────────────────
            if event.message.text and event.message.text.startswith('/start'):
                admin_text = "\n".join([f"👤 @{u}" for u in rule.admin_usernames])
                target_text = "\n".join([f"📢 {t}" for t in rule.target_chat_ids]) or "No specific targets (Auto-Forward Mode)"
                
                response = (
                    "🚀 **Bot Forwarding System Active**\n\n"
                    f"🛡️ **Authorized Admins:**\n{admin_text}\n\n"
                    f"🎯 **Target Channels/Groups:**\n{target_text}\n\n"
                    "✅ Send any message here, and I will forward it to the targets."
                )
                await event.reply(response)
                return

            # Prevent forwarding of other commands
            if event.message.text and event.message.text.startswith('/'):
                return

            # 4. Filters
            msg_text = event.message.text or ""
            if rule.keyword_filters:
                if not any(kw.lower() in msg_text.lower() for kw in rule.keyword_filters):
                    logger.debug(f"[bot-service] Keyword filter blocked message")
                    return

            # 5. Forwarding Loop
            targets = rule.target_chat_ids
            if not targets:
                logger.debug("[bot-service] No targets set, auto-detecting dialogs...")
                async for dialog in client.iter_dialogs():
                    if dialog.is_group or dialog.is_channel:
                        targets.append(str(dialog.id))

            success_count = 0
            for target in targets:
                try:
                    t = target.strip()
                    if t.startswith("-") and t.lstrip("-").isdigit():
                        t = int(t)
                    
                    try:
                        entity = await client.get_entity(t)
                    except Exception:
                        entity = t

                    from app.services.terminal_service import terminal_manager
                    if rule.forward_mode == "forward":
                        await client.forward_messages(entity, event.message)
                    else:
                        await client.send_message(entity, event.message)
                    
                    success_count += 1
                    msg_snippet = (event.message.text[:30] + "...") if event.message.text and len(event.message.text) > 30 else (event.message.text or "Media/Attachment")
                    await terminal_manager.log_event(rule.user_id, f"📤 Bot {rule.name} forwarded: '{msg_snippet}' to {target}", bot_id, "bot_hub", "SUCCESS")
                    logger.info(f"[bot-service] Bot {rule.name} forwarded message successfully to {target}")
                except Exception as e:
                    from app.services.terminal_service import terminal_manager
                    await terminal_manager.log_event(rule.user_id, f"❌ Bot {rule.name} failed to forward to {target}: {str(e)}", bot_id, "bot_hub", "ERROR")
                    logger.error(f"[bot-service] Bot {rule.name} failed to forward to {target}: {e}")

            # ── Success Confirmation Reply ──
            if success_count > 0:
                await event.reply(f"✅ Successfully forwarded to {success_count} target(s).")
            elif targets:
                await event.reply("⚠️ Failed to forward to any targets. Check the terminal for errors.")

        except Exception as outer_e:
            logger.error(f"[bot-service] Error in bot message handler: {outer_e}")

    logger.debug(f"[bot-service] Handlers attached for bot {bot.name}")

async def send_direct_message(bot_id: str, text: str):
    """Manually send a message to all targets from a specific bot."""
    if bot_id not in _active_bots:
        # Try to start it if not active
        bot = await BotForwarder.get(bot_id)
        if bot and bot.is_enabled:
            await start_bot(bot)
        else:
            raise Exception("Bot is not active or enabled")

    client = _active_bots[bot_id]
    rule = await BotForwarder.get(bot_id)
    if not rule: return

    targets = rule.target_chat_ids
    if not targets:
        # AUTOMATIC MODE: Fetch all dialogs the bot is part of
        try:
            logger.info(f"[bot-service] Bot {rule.name} has no targets. Fetching all dialogs...")
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    targets.append(str(dialog.id))
        except Exception as e:
            logger.error(f"[bot-service] Failed to fetch auto-targets: {e}")

    for target in targets:
        try:
            t = target.strip()
            if t.startswith("-") and t.lstrip("-").isdigit():
                t = int(t)
            
            # Resolve entity if it's a username to be safe
            try:
                entity = await client.get_entity(t)
            except Exception:
                entity = t
                
            await client.send_message(entity, text)
            logger.info(f"[bot-service] Manual broadcast successful to {target}")
        except Exception as e:
            logger.error(f"[bot-service] Manual send failed for {target}: {e}")
