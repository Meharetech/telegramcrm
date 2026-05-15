import asyncio
import random
import logging
from datetime import datetime, timezone
from typing import Dict, List
from app.models import TelegramAccount, AccountAgingTask
from app.client_cache import get_client
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

# { user_id: Task }
_active_aging: Dict[str, asyncio.Task] = {}

WARMUP_MESSAGES = [
    "Hello! How are you doing today?", "Hey there!", "Good morning!", "Did you see the latest news?",
    "I'm working on a project right now.", "Telegram is pretty cool, isn't it?", "What are your plans for the weekend?",
    "Just checking in!", "Hope you're having a great day.", "Nice to meet you!", "Have a good one!",
    "Talk to you later.", "I'm learning a lot lately.", "Do you like music?", "What's your favorite hobby?",
    "Stay positive!", "Everything is going well.", "Let's chat more later.", "Take care!", "Best regards.",
    "The weather is nice today.", "I'm thinking of grabbing some coffee.", "Do you use any other messaging apps?",
    "I've been quite busy lately.", "Looking forward to our next chat!", "Let me know if you need anything.",
    "I'm just browsing some channels.", "Have you tried the new Telegram features?", "It's been a productive day so far.",
    "Catch you later!", "What's up?", "How's it going?", "Any big news?", "I'm just relaxing a bit.",
    "Do you have any recommendations for books?", "I love the stickers on Telegram.", "Just finished some work.",
    "Are you a fan of movies?", "I'm thinking of traveling somewhere soon.", "Let's keep in touch.",
    "How's the weather where you are?", "I've been exploring new groups.", "Telegram's speed is impressive.",
    "Do you prefer dark mode or light mode?", "I'm feeling quite energetic today.", "Hope all is well with you.",
    "Let's talk soon!", "I just saw a funny meme.", "Are you into technology?", "I'm a bit tired, but good.",
    "What's your favorite food?", "I'm learning a new language.", "Telegram is so much better than other apps.",
    "Have a wonderful evening!", "I'm just hanging out.", "Do you enjoy sports?", "I'm planning my next week.",
    "Let's catch up later.", "I hope you have a productive day.", "What's the best thing that happened today?",
    "I'm listening to some music right now.", "Do you like to travel?", "I've been quite active on Telegram lately.",
    "Let's share some interesting links.", "I'm always looking for new things to learn.", "Have a great weekend!",
    "I'm just about to go for a walk.", "Do you have any pets?", "I love how secure Telegram is.",
    "What's your favorite color?", "I'm thinking of starting a new hobby.", "Hope you're enjoying your day.",
    "Let's stay connected.", "I just had a delicious meal.", "Are you a fan of art?", "I'm feeling quite creative today.",
    "What's your dream destination?", "I'm just checking my messages.", "Do you like to cook?",
    "I've been discovering some great bots.", "Let's chat again soon.", "I hope your day is going smoothly.",
    "What's your favorite season?", "I'm just taking a short break.", "Do you enjoy nature?",
    "I'm thinking of redecorating my room.", "Hope you have a lovely day.", "Let's keep the conversation going.",
    "I just finished a great workout.", "Are you into gaming?", "I'm feeling very motivated today.",
    "What's your favorite movie genre?", "I'm just browsing the web.", "Do you like to read?",
    "I've been following some interesting channels.", "Let's exchange some thoughts.", "I hope you're doing fantastic.",
    "What's your favorite drink?", "I'm just relaxing at home.", "Do you enjoy photography?",
    "I'm thinking of attending a webinar.", "Hope your week is going well.", "Let's talk more often.",
    "I just saw a beautiful sunset.", "Are you a fan of science?", "I'm feeling quite peaceful today.",
    "What's your favorite type of music?", "I'm just catching up on news.", "Do you like to dance?",
    "I've been trying out new Telegram themes.", "Let's have a chat later.", "I hope you're having a blast.",
    "What's your favorite city?", "I'm just thinking about life.", "Do you enjoy outdoor activities?",
    "I'm thinking of learning to play an instrument.", "Hope you have an amazing day.", "Let's stay in loop."
]

async def start_aging(user_id: str):
    if user_id in _active_aging:
        return
    
    task_obj = await AccountAgingTask.find_one(AccountAgingTask.user_id == user_id)
    if not task_obj:
        task_obj = AccountAgingTask(user_id=user_id)
        await task_obj.insert()
        
    if not task_obj.selected_account_ids:
        await terminal_manager.log_event(user_id, "⚠️ No accounts selected for aging.", "system", "aging", "ERROR")
        return
    
    task_obj.is_active = True
    await task_obj.save()
    
    _active_aging[user_id] = asyncio.create_task(run_aging_loop(user_id))
    await terminal_manager.log_event(user_id, "🚀 Account Aging service STARTED.", "system", "aging", "SUCCESS")

async def stop_aging(user_id: str):
    if user_id in _active_aging:
        _active_aging[user_id].cancel()
        del _active_aging[user_id]
        
    task_obj = await AccountAgingTask.find_one(AccountAgingTask.user_id == user_id)
    if task_obj:
        task_obj.is_active = False
        await task_obj.save()
        
    await terminal_manager.log_event(user_id, "⏹️ Account Aging service STOPPED.", "system", "aging", "WARNING")

async def run_aging_loop(user_id: str):
    try:
        busy_accounts = set()
        
        while True:
            # Refresh task object to get latest settings
            task_obj = await AccountAgingTask.find_one(AccountAgingTask.user_id == user_id)
            if not task_obj or not task_obj.is_active:
                break
            
            acc_ids = task_obj.selected_account_ids
            if len(acc_ids) < 2:
                await terminal_manager.log_event(user_id, "⚠️ Aging requires at least 2 accounts. Waiting...", "system", "aging", "WARNING")
                await asyncio.sleep(60)
                continue
            
            # Generate and shuffle pairs for this cycle
            import itertools
            all_pairs = list(itertools.combinations(acc_ids, 2))
            random.shuffle(all_pairs)
            
            await terminal_manager.log_event(user_id, f"🚀 Parallel Rotation Started: {len(all_pairs)} chat sessions planned.", "system", "aging", "INFO")
            
            # Dynamic Concurrency
            effective_concurrency = task_obj.parallel_sessions
            if task_obj.use_max_parallelism:
                effective_concurrency = max(1, len(acc_ids) // 2)
                await terminal_manager.log_event(user_id, f"⚡ MAX SPEED ENABLED: Running {effective_concurrency} sessions in parallel.", "system", "aging", "SUCCESS")

            active_sessions = []
            pair_index = 0
            
            while pair_index < len(all_pairs) or active_sessions:
                # 1. Start new sessions up to concurrency limit
                while len(active_sessions) < effective_concurrency and pair_index < len(all_pairs):
                    id_a, id_b = all_pairs[pair_index]
                    
                    # Check for account collisions
                    if id_a in busy_accounts or id_b in busy_accounts:
                        # Skip for now, will try next pair
                        pair_index += 1
                        continue
                    
                    # Start session
                    session_task = asyncio.create_task(
                        run_chat_session(user_id, id_a, id_b, busy_accounts)
                    )
                    active_sessions.append(session_task)
                    pair_index += 1

                # 2. Clean up completed sessions
                done, active_sessions = [
                    ( [t for t in active_sessions if t.done()], [t for t in active_sessions if not t.done()] )
                ][0]
                
                # 3. Handle any tasks that crashed (just for logging)
                for t in done:
                    try:
                        await t
                    except Exception as e:
                        logger.error(f"Chat session task failed: {e}")

                # Check for stop signal
                task_obj = await AccountAgingTask.find_one(AccountAgingTask.user_id == user_id)
                if not task_obj or not task_obj.is_active:
                    # Cancel all pending
                    for t in active_sessions: t.cancel()
                    return

                # Wait before checking for free slots again
                await asyncio.sleep(2)
            
            await terminal_manager.log_event(user_id, "✅ Full Fleet Rotation Complete. Resting before next round...", "system", "aging", "SUCCESS")
            await asyncio.sleep(task_obj.max_delay) 

    except asyncio.CancelledError:
        logger.info(f"Aging loop for {user_id} cancelled.")
    except Exception as e:
        logger.error(f"Fatal error in aging loop for {user_id}: {e}")

async def run_chat_session(user_id: str, id_a: str, id_b: str, busy_set: set):
    """Handles a single back-and-forth session between two accounts."""
    busy_set.add(id_a)
    busy_set.add(id_b)
    
    try:
        task_obj = await AccountAgingTask.find_one(AccountAgingTask.user_id == user_id)
        if not task_obj: return

        acc_a = await TelegramAccount.get(id_a)
        acc_b = await TelegramAccount.get(id_b)
        if not acc_a or not acc_b: return
        
        num_messages = random.randint(2, 4)
        await terminal_manager.log_event(user_id, f"🗨️ Session Start: @{acc_a.username or acc_a.phone_number} ⬌ @{acc_b.username or acc_b.phone_number} ({num_messages} msgs)", "system", "aging", "INFO")
        
        # Mutual Contact Sync
        try:
            from telethon import functions
            c_a = await get_client(id_a)
            c_b = await get_client(id_b)
            if c_a: await c_a(functions.contacts.AddContactRequest(id=acc_b.username or acc_b.phone_number, first_name=acc_b.first_name or "Friend", last_name=acc_b.last_name or "", phone=acc_b.phone_number, add_phone_privacy_exception=True))
            if c_b: await c_b(functions.contacts.AddContactRequest(id=acc_a.username or acc_a.phone_number, first_name=acc_a.first_name or "Friend", last_name=acc_a.last_name or "", phone=acc_a.phone_number, add_phone_privacy_exception=True))
        except: pass

        for i in range(num_messages):
            is_a_sender = (i % 2 == 0)
            sender = acc_a if is_a_sender else acc_b
            receiver = acc_b if is_a_sender else acc_a
            
            client = await get_client(str(sender.id))
            if not client: continue
            
            target_peer = receiver.username if receiver.username else receiver.phone_number
            msg = random.choice(WARMUP_MESSAGES)
            await client.send_message(target_peer, msg)
            
            # Update stats atomically
            await AccountAgingTask.find_one(AccountAgingTask.user_id == user_id).update({"$inc": {"total_messages_sent": 1}, "$set": {"last_message_at": datetime.now(timezone.utc)}})
            
            disp_s = f"@{sender.username}" if sender.username else sender.phone_number
            disp_r = f"@{receiver.username}" if receiver.username else receiver.phone_number
            await terminal_manager.log_event(user_id, f"💬 AGING: {disp_s} -> {disp_r}: '{msg}'", str(sender.id), "aging", "SUCCESS")
            
            await asyncio.sleep(random.randint(4, 8))

        # Cool-down after session
        await asyncio.sleep(random.randint(task_obj.min_delay, task_obj.max_delay))

    except Exception as e:
        logger.error(f"Error in chat session {id_a}-{id_b}: {e}")
    finally:
        busy_set.discard(id_a)
        busy_set.discard(id_b)
