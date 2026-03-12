"""
reaction/logic.py — Reaction boosting service.

Key fixes applied vs original:
  1. Ghost handler leak: _reaction_handlers dict tracks and removes old handlers
     before re-attaching on task update.
  2. Stale task data: react_to_message_with_all_nodes now uses re-fetched
     `current` task data (emojis, target_link) instead of the snapshot at
     function entry.
  3. Startup stagger: execute_reaction_boost is unchanged but callers in
     main.py now stagger launches (see main.py fix).
"""

import asyncio
import random
import logging
from datetime import datetime
from telethon import events
from telethon.tl.functions.messages import SendReactionRequest, ImportChatInviteRequest
from telethon.tl.types import ReactionEmoji, Channel, Chat
from app.client_cache import get_client
from app.models.account import TelegramAccount
from app.models.reaction import ReactionTask

logger = logging.getLogger(__name__)

# ── FIX: Track listener handlers so we can remove them on task update ─────────
# { task_id: handler_function }
# Without this dict, every task update stacks another handler on the same
# Telethon client, causing duplicate reactions per new post.
_reaction_handlers: dict = {}


async def execute_reaction_boost(task_id: str):
    """
    Dispatcher for reaction tasks. Supports one-time boosts and continuous monitoring.
    """
    task = await ReactionTask.get(task_id)
    if not task:
        return

    # ── NEW: Bulk Join Phase ──────────────────────────────────────────
    # User requested: "make first join group all id first"
    await bulk_join_all_nodes(task)

    if task.task_type == "one_time":
        await run_one_time_boost(task)
    else:
        await start_continuous_monitor(task)

async def run_one_time_boost(task: ReactionTask):
    task.status = "running"
    await task.save()

    for account_id in task.account_ids:
        # Check for cancellation
        current_task = await ReactionTask.get(str(task.id))
        if not current_task or current_task.status == "cancelled":
            break

        success = await send_single_reaction(account_id, task.target_link, task.message_id, task.emojis)
        
        if success:
            task.processed_accounts.append(account_id)
        else:
            task.failed_accounts.append({"id": account_id, "error": "Reaction failed"})
        
        await task.save()
        await asyncio.sleep(random.randint(task.min_delay, task.max_delay))

    task.status = "completed" if not task.failed_accounts else "partially_completed"
    await task.save()

async def start_continuous_monitor(task: ReactionTask):
    """
    Monitoring mode: Listens for NEW messages in the target link and reacts using all nodes.
    """
    task_id_str = str(task.id)

    task.status = "monitoring"
    await task.save()

    # We use the FIRST account as the 'Listener'
    listener_id = task.account_ids[0]
    listener_acc = await TelegramAccount.get(listener_id)
    if not listener_acc:
        task.status = "failed"
        await task.save()
        return

    try:
        client = await get_client(
            listener_id, 
            listener_acc.session_string, 
            listener_acc.api_id, 
            listener_acc.api_hash
        )

        # ── FIX: Remove stale handler from previous run/update ────────────────
        if task_id_str in _reaction_handlers:
            try:
                client.remove_event_handler(_reaction_handlers[task_id_str])
                logger.info(f"[reaction] Removed old handler for task {task_id_str}")
            except Exception:
                pass
            del _reaction_handlers[task_id_str]

        actual_target = task.target_link
        if "://" in actual_target:
            actual_target = actual_target.split("://")[-1]

        listen_chat = actual_target
        if "t.me/" in actual_target and not "joinchat" in actual_target and not "+" in actual_target:
            parts = actual_target.split("/")
            if len(parts) >= 3 and parts[-1].isdigit():
                listen_chat = parts[-2]
            else:
                listen_chat = parts[-1]
        
        if "joinchat" in actual_target or "+" in actual_target:
            try: await ensure_joined_robust(client, task.target_link)
            except: pass
            
        try:
            # Resolve the accurate entity ID for Telethon 'chats' filter
            entity = await client.get_entity(task.target_link if ("joinchat" in actual_target or "+" in actual_target) else listen_chat)
            listen_chat = entity.id
        except Exception as e:
            logger.warning(f"Could not preemptively resolve entity for {task.target_link}: {e}")

        @client.on(events.NewMessage())
        async def handler(event):
            # Check if user services are active
            from app.client_cache import is_user_active
            if not await is_user_active(task.user_id):
                return

            # Manually filter the chat to ensure 100% intercept rate
            chat = await event.get_chat()
            if not chat: return

            # Check if event chat matches our target
            event_id = getattr(chat, 'id', None)
            event_username = getattr(chat, 'username', None)

            is_match = False
            if listen_chat == event_id:
                is_match = True
            elif event_username and str(event_username).lower() in str(task.target_link).lower():
                is_match = True

            if not is_match:
                return

            # Verify task is still active
            active_task = await ReactionTask.get(task_id_str)
            if not active_task or not active_task.is_active or active_task.status == "cancelled":
                # Self-clean the handler
                try:
                    client.remove_event_handler(handler)
                    _reaction_handlers.pop(task_id_str, None)
                except Exception:
                    pass
                return

            msg_id = event.message.id
            logger.info(f"New post detected in {task.target_link} (ID: {msg_id}). Triggering boost...")
            
            # Record that we are reacting to this message
            active_task.reacted_messages.append(msg_id)
            await active_task.save()

            # Trigger reactions from ALL accounts in background to not block the listener
            asyncio.create_task(react_to_message_with_all_nodes(task_id_str, msg_id))

        # ── FIX: Store handler ref so future updates can remove it ─────────
        _reaction_handlers[task_id_str] = handler

        logger.info(f"Started monitoring {task.target_link} for task {task.id}")
        
        # Keep alive while active (Optimized Polling)
        wait_cycles = 0
        user_active = True
        while True:
            await asyncio.sleep(20) # Check every 20s
            
            # Real-time User status check (Cached & fast)
            from app.client_cache import is_user_active
            user_active = await is_user_active(task.user_id)
            
            active_task = await ReactionTask.get(task_id_str)
            if not active_task or not active_task.is_active or active_task.status == "cancelled" or not user_active:
                try:
                    client.remove_event_handler(handler)
                    _reaction_handlers.pop(task_id_str, None)
                except Exception: pass
                break

    except Exception as e:
        logger.error(f"Monitor failed for task {task.id}: {e}")
        task.status = "failed"
        await task.save()
        # Clean any registered handler on failure
        _reaction_handlers.pop(task_id_str, None)

async def react_to_message_with_all_nodes(task_id: str, message_id: int):
    task = await ReactionTask.get(task_id)
    if not task: return

    for account_id in task.account_ids:
        # Apply the user's delay BEFORE sending the reaction
        delay = random.randint(task.min_delay, task.max_delay)
        logger.info(f"Task {task_id}: Waiting {delay}s before next reaction...")
        await asyncio.sleep(delay)

        # ── FIX: Re-fetch task to get latest config (emojis, target_link, is_active)
        # Previously used stale snapshot from function entry, so updates mid-loop
        # would still use old emojis / target_link.
        current = await ReactionTask.get(task_id)
        if not current or not current.is_active: break

        await send_single_reaction(account_id, current.target_link, message_id, current.emojis)

async def send_single_reaction(account_id: str, target: str, message_id: int, emojis: list) -> bool:
    account = await TelegramAccount.get(account_id)
    if not account: return False

    try:
        client = await get_client(
            account_id, account.session_string, account.api_id, account.api_hash,
            device_model=account.device_model
        )

        # Handle full message links (e.g. t.me/channel/123 or https://t.me/channel/123)
        actual_target = target
        actual_msg_id = message_id or 0

        # Strip https:// or http://
        if "://" in actual_target:
            actual_target = actual_target.split("://")[-1]

        # Extract message ID if it's in the link e.g. t.me/channelname/456
        if "t.me/" in actual_target and not "joinchat" in actual_target and not "+" in actual_target:
            parts = actual_target.split("/")
            if len(parts) >= 3 and parts[-1].isdigit():
                actual_msg_id = int(parts[-1])
                actual_target = "t.me/" + parts[-2]

        # Ensure joined (for private and public links)
        try: 
            await ensure_joined_robust(client, actual_target)
        except: pass

        # Resolve entity
        try:
            peer_entity = await client.get_entity(actual_target)
        except Exception as e:
            logger.warning(f"Could not resolve entity before reacting: {e}")
            peer_entity = actual_target 

        # Auto-detect latest message ID if 0 or missing
        if not actual_msg_id or actual_msg_id == 0:
            try:
                messages = await client.get_messages(peer_entity, limit=1)
                if messages and len(messages) > 0:
                    actual_msg_id = messages[0].id
                    logger.info(f"Auto-detected latest message ID: {actual_msg_id} for {actual_target}")
                else:
                    logger.warning(f"No messages found in {actual_target} to react to.")
                    return False
            except Exception as e:
                logger.error(f"Failed to fetch latest message from {actual_target}: {e}")
                return False

        # Pick random emoji from selection
        emoji = random.choice(emojis)
        logger.info(f"Account {account.phone_number} attempting to react {emoji} to {actual_target} msg {actual_msg_id}")

        await client(SendReactionRequest(
            peer=peer_entity,
            msg_id=actual_msg_id,
            reaction=[ReactionEmoji(emoticon=emoji)]
        ))
        from app.services.terminal_service import terminal_manager
        await terminal_manager.log_event(account.user_id, f"⚡ REACTED {emoji}: {account.phone_number} -> {actual_target}", str(account.id), "reaction", "SUCCESS")
        
        logger.info(f"Account {account.phone_number} successfully reacted to {actual_target} msg {actual_msg_id}")
        return True
    except Exception as e:
        logger.error(f"Single reaction failed for account {account_id} on {target}: {e}")
        return False


async def ensure_joined_robust(client, target_link: str):
    """
    Robustly joins a group/channel given any Telegram link or username.
    Handles private invite links (+), joinchat links, and public handles.
    """
    from telethon.tl.functions.messages import ImportChatInviteRequest
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import Channel, Chat
    from telethon.errors import UserAlreadyParticipantError, InviteHashExpiredError, InviteHashInvalidError

    link = target_link.strip()
    
    try:
        # Pre-resolve entity if it's a simple username or known chat to check status
        is_private = "+" in link or "joinchat/" in link
        
        if not is_private:
            username = link
            if "t.me/" in link:
                username = link.split("t.me/")[-1].split("/")[0].split("?")[0].lstrip('@')
            elif link.startswith("@"):
                username = link.lstrip("@")
            
            try:
                entity = await client.get_entity(username)
                # If we successfully got entity, check if we are ALREADY in it
                if hasattr(entity, 'left'):
                    if entity.left is False: # False means we are IN
                        return True
                else:
                    # For basic Chats, we are in if we can see it
                    return True
            except Exception:
                # Could not resolve or not in, proceed to join attempt
                pass

        # 1. Handle Private Invite Links
        if is_private:
            if "+" in link:
                invite_hash = link.split("+")[-1]
            else:
                invite_hash = link.split("joinchat/")[-1]
            
            invite_hash = invite_hash.split("/")[0].split("?")[0]
            
            try:
                await client(ImportChatInviteRequest(hash=invite_hash))
                return True
            except UserAlreadyParticipantError:
                return True
            except (InviteHashExpiredError, InviteHashInvalidError):
                logger.error(f"Invite link expired or invalid: {link}")
                return False
            except Exception as e:
                logger.warning(f"Private join failed for {invite_hash}: {e}")
                return False # Don't proceed to public join for private links
        
        # 2. Handle Public Join
        username = link
        if "t.me/" in link:
            username = link.split("t.me/")[-1].split("/")[0].split("?")[0].lstrip('@')
        elif link.startswith("@"):
            username = link.lstrip("@")
            
        try:
            entity = await client.get_entity(username)
            await client(JoinChannelRequest(entity))
            return True
        except UserAlreadyParticipantError:
            return True
        except Exception as e:
            logger.error(f"Public join failed for {username}: {e}")
            return False

    except Exception as e:
        logger.error(f"Join logic failed for {target_link}: {e}")
        return False


async def bulk_join_all_nodes(task: ReactionTask):
    """
    User Request: "make first join group all id first".
    Initializes a cluster-wide join to ensure all nodes are ready to react.
    """
    from app.services.terminal_service import terminal_manager
    
    total = len(task.account_ids)
    await terminal_manager.log_event(task.user_id, f"🔄 CLUSTER JOIN: Preparing {total} accounts for reaction boost in {task.target_link}...", "SYSTEM", "reaction", "INFO")
    
    # We use a semaphore to not hit Telegram too hard at once if there are hundreds of accounts
    semaphore = asyncio.Semaphore(10)

    async def single_node_join(acc_id):
        async with semaphore:
            account = await TelegramAccount.get(acc_id)
            if not account: return
            
            try:
                client = await get_client(acc_id, account.session_string, account.api_id, account.api_hash, device_model=account.device_model)
                await ensure_joined_robust(client, task.target_link)
            except Exception as e:
                logger.error(f"Node join failed for {acc_id}: {e}")

    # Launch all joins concurrently but throttled by semaphore
    await asyncio.gather(*(single_node_join(aid) for aid in task.account_ids))
    
    await terminal_manager.log_event(task.user_id, f"✅ CLUSTER READY: All available accounts joined/verified {task.target_link}.", "SYSTEM", "reaction", "SUCCESS")
