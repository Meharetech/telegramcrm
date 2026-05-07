import asyncio
import random
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from telethon import functions, types
from telethon.errors import (
    FloodWaitError, PeerFloodError, PeerIdInvalidError,
    UsernameInvalidError, UsernameNotOccupiedError,
    PhoneNumberBannedError, UserRestrictedError,
    AuthKeyUnregisteredError, UserPrivacyRestrictedError,
    RPCError
)
from app.models import TelegramAccount, MessageCampaignJob
from app.client_cache import get_client
from app.services.terminal_service import terminal_manager
from app.config import settings
from bson import ObjectId

logger = logging.getLogger(__name__)

# Global storage for background tasks
# { user_id: ActiveMessageCampaign }
MESSAGE_CAMPAIGN_TASKS: Dict[str, 'ActiveMessageCampaign'] = {}

class ActiveMessageCampaign:
    def __init__(self, user_id: str, method: str, message_text: str, account_configs: List[Any], 
                 min_delay: int, max_delay: int, username_list: List[str] = []):
        self.user_id = user_id
        self.method = method # 'contact' or 'username'
        self.message_text = message_text
        self.account_configs = account_configs
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.username_list = username_list
        
        self.status = "running"
        self.done_count = 0
        self.errors_count = 0
        self.total_targets = 0
        self.is_done = False
        self.logs = []
        self.queues: List[asyncio.Queue] = []
        self.lock = asyncio.Lock()
        self.stop_requested = False
        self.job_id: Optional[str] = None
        self.accounts_to_use = []
        self.global_username_queue = list(username_list) if method == 'username' else []
        self._is_syncing = False
        self.is_scheduled = False  # Set to True when triggered by scheduler

    async def add_log(self, event: str, message: str, level: str = "INFO", data: dict = None):
        async with self.lock:
            ts = datetime.now().strftime("%I:%M:%S %p")
            log_entry = {
                "msg": message,
                "level": level,
                "time": ts,
                **(data or {})
            }
            sse_msg = {"event": event, "data": json.dumps(log_entry)}
            self.logs.append(log_entry)
            if len(self.logs) > 100:
                self.logs.pop(0)
            for q in self.queues:
                await q.put(sse_msg)
            if event in ["status", "progress", "done", "error"]:
                now = datetime.now()
                # Only sync DB every 5 seconds to reduce load, unless it's a final event
                if not hasattr(self, 'last_sync_time') or (now - self.last_sync_time).total_seconds() >= 5 or event in ["done", "error"]:
                    if not self._is_syncing:
                        self.last_sync_time = now
                        self._is_syncing = True
                        asyncio.create_task(self.sync_state())

    async def sync_state(self):
        try:
            job = None
            if self.job_id and self.job_id != "None":
                job = await MessageCampaignJob.get(self.job_id)
            if not job:
                job = await MessageCampaignJob.find_one(
                    MessageCampaignJob.user_id == self.user_id,
                    MessageCampaignJob.status == "running"
                )
            if not job:
                job = MessageCampaignJob(
                    user_id=self.user_id,
                    method=self.method,
                    message_text=self.message_text,
                    username_list=self.username_list,
                    min_delay=self.min_delay,
                    max_delay=self.max_delay,
                    status=self.status,
                    total_targets=self.total_targets
                )
                await job.insert()
            self.job_id = str(job.id)
            job.done_count = self.done_count
            job.errors_count = self.errors_count
            job.status = self.status
            job.updated_at = datetime.now(timezone.utc)
            
            results = {}
            for acc in self.accounts_to_use:
                results[acc["acc_id"]] = {
                    "phone": acc["phone"],
                    "done": acc["this_task_done"],
                    "status": "failed" if acc["failed"] else ("done" if acc.get("logged_done") else "running"),
                    "last_error": acc.get("last_error_msg", "")
                }
            job.account_results = results
            job.logs = self.logs[-100:]
            await job.save()
        except Exception as e:
            logger.error(f"[msg_campaign] DB Sync failed: {e}")
        finally:
            self._is_syncing = False

    async def run(self):
        try:
            # ── Step 0: User Service Guard ───────────────────────────────────
            from app.models.user import User
            user = await User.get(self.user_id)
            if not user:
                await self.add_log("error", "🛑 Task Aborted: User not found.", "ERROR")
                return
            # Only block if manually launched AND services are stopped.
            # Scheduled tasks bypass this check.
            if not self.is_scheduled and not user.services_active:
                await self.add_log("error", "🛑 Task Aborted: User services are currently STOPPED.", "ERROR")
                return

            await self.add_log("status", f"🚀 Initializing Message Campaign for {len(self.account_configs)} accounts...")
            await terminal_manager.log_event(self.user_id, f"🚀 Starting Bulk Messaging Campaign.", module="msg_campaign", level="INFO")

            now_utc = datetime.now(timezone.utc)
            self.accounts_to_use = []
            
            # ── OPTIMIZED: Batch Fetch All Target Accounts ──────────────
            acc_ids = [ObjectId(cfg.id) for cfg in self.account_configs]
            all_accounts = await TelegramAccount.find({"_id": {"$in": acc_ids}}).to_list()
            acc_map = {str(a.id): a for a in all_accounts}

            for config in self.account_configs:
                if self.stop_requested: break
                acc = acc_map.get(config.id)
                if not acc or not acc.is_active: continue
                
                # Check for persistent FloodWait
                flood_until = acc.flood_wait_until
                if flood_until:
                    if flood_until.tzinfo is None:
                        flood_until = flood_until.replace(tzinfo=timezone.utc)
                    
                    if flood_until > now_utc:
                        wait_left = (flood_until - now_utc).total_seconds()
                        await self.add_log("log", f"⏳ {acc.phone_number} on FloodWait for {int(wait_left)}s. Skipping.", "WARNING")
                        continue

                # Check Daily Limits
                if acc.last_message_sent_date and acc.last_message_sent_date.date() < now_utc.date():
                    acc.messages_sent_today = 0
                
                if acc.messages_sent_today >= acc.daily_messages_limit:
                    await self.add_log("log", f"⚠️ {acc.phone_number} reached daily message limit. Skipping.", "WARNING")
                    continue

                if acc.active_task_id and acc.active_task_id != self.job_id:
                    await self.add_log("log", f"ℹ️ {acc.phone_number} is currently busy with {acc.active_task_type}. Proceeding anyway.", "WARNING")
                    # No longer skipping. User requested to use busy accounts.

                try:
                    client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash, device_model=acc.device_model)
                    
                    targets = []
                    if self.method == 'contact':
                        res = await client(functions.contacts.GetContactsRequest(hash=0))
                        # Only targets that haven't been contacted yet in this task
                        targets = [{"id": u.id, "username": u.username, "phone": u.phone} for u in res.users if not u.bot]
                        if not targets:
                            await self.add_log("log", f"ℹ️ {acc.phone_number} has no fresh contacts. Skipping.", "WARNING")
                            continue
                    
                    self.accounts_to_use.append({
                        "db_acc": acc,
                        "acc_id": str(acc.id),
                        "phone": acc.phone_number,
                        "client": client,
                        "targets": targets,
                        "target_count": config.count,
                        "this_task_done": 0,
                        "failed": False,
                        "last_error_msg": ""
                    })
                except Exception as e:
                    reason = str(e)
                    await self.add_log("log", f"❌ Account {acc.phone_number} error: {reason}", "ERROR")
                    if any(x in reason.lower() for x in ["auth", "revoked", "banned"]):
                        acc.is_active = False
                        acc.status = "error"
                        await acc.save()

            if not self.accounts_to_use:
                await self.add_log("error", "❌ No accounts available to proceed.", "ERROR")
                return

            # ── Step 0.5: Batch Lock All Accounts ───────────────────────────
            acc_ids_to_lock = [ObjectId(a["acc_id"]) for a in self.accounts_to_use]
            await TelegramAccount.find({"_id": {"$in": acc_ids_to_lock}}).update({
                "$set": {
                    "active_task_id": self.job_id,
                    "active_task_type": "campaign"
                }
            })
            for a in self.accounts_to_use:
                target_info = f" (Available Targets: {len(a.get('targets', []))})" if self.method == 'contact' else ""
                await self.add_log("log", f"✅ Account {a['phone']} ready. Goal: {a['target_count']}{target_info}", "SUCCESS")

            if self.method == 'username':
                self.total_targets = min(len(self.username_list), sum(a["target_count"] for a in self.accounts_to_use))
                if not self.global_username_queue:
                    # Sync queue if it was somehow empty
                    self.global_username_queue = list(self.username_list)
            else:
                self.total_targets = sum(min(len(a["targets"]), a["target_count"]) for a in self.accounts_to_use)

            if self.total_targets <= 0:
                await self.add_log("error", f"❌ No targets found to message. (Method: {self.method})", "ERROR")
                return

            await self.add_log("status", f"📂 Campaign target: {self.total_targets} users. Starting rotation...", data={"total": self.total_targets})

            # ── Optimized Async Rotation Loop (Sequential & Distributed) ─────
            import time
            import re
            import random
            from app.client_cache import is_user_active

            def parse_spintax(text):
                pattern = re.compile(r'\{([^{}]*)\}')
                while True:
                    match = pattern.search(text)
                    if not match: break
                    choices = match.group(1).split('|')
                    text = text.replace(match.group(0), random.choice(choices), 1)
                return text
            
            for acc in self.accounts_to_use:
                acc["next_work_at"] = 0

            # Safety for delay values
            min_d = min(self.min_delay, self.max_delay)
            max_d = max(self.min_delay, self.max_delay)
            if min_d == max_d: max_d += 1

            any_working = True
            while self.done_count < self.total_targets and not self.stop_requested:
                # Check User Service Guard (Real-time) — skip for scheduled tasks
                if not self.is_scheduled and not await is_user_active(self.user_id):
                    await self.add_log("error", "🛑 Task Aborted: User services were STOPPED by administrator.", "ERROR")
                    self.stop_requested = True
                    break

                any_working = False
                found_ready = False
                
                # Sort accounts by next_work_at to always pick the one that has waited longest
                self.accounts_to_use.sort(key=lambda x: x.get("next_work_at", 0))

                for acc_task in self.accounts_to_use:
                    if self.stop_requested: break
                    if acc_task["failed"]: continue
                    if acc_task["this_task_done"] >= acc_task["target_count"]: continue
                    
                    any_working = True
                    now = time.time()
                    if now < acc_task["next_work_at"]:
                        continue # Still cooling down
                        
                    found_ready = True

                    # ── Safety Check: Re-verify Daily Limit ───────────────────
                    db_acc = acc_task["db_acc"]
                    if db_acc.messages_sent_today >= db_acc.daily_messages_limit:
                        acc_task["failed"] = True
                        await self.add_log("log", f"⚠️ {acc_task['phone']} hit daily limit mid-task. Retired.", "WARNING")
                        continue

                    # ── Target Selection ──────────────────────────────────────
                    target = None
                    if self.method == 'username':
                        if self.global_username_queue:
                            target = self.global_username_queue.pop(0)
                        else:
                            self.total_targets = self.done_count 
                            break 
                    elif self.method == 'contact':
                        if acc_task["targets"]:
                            target_ref = acc_task["targets"].pop(0)
                            target = target_ref['id']
                        else:
                            continue
                    
                    if not target: continue
                    
                    message_to_send = parse_spintax(self.message_text)
                    await self.add_log("log", f"⏳ {acc_task['phone']} sending to {target}...", "INFO")
                    
                    try:
                        # ── Connection Guard ──
                        from app.client_cache import touch
                        touch(acc_task["acc_id"])
                        
                        if not acc_task["client"].is_connected():
                            await self.add_log("log", f"🔄 {acc_task['phone']} disconnected. Reconnecting...", "WARNING")
                            acc_task["client"] = await get_client(acc_task["acc_id"])

                        if self.stop_requested: break
                        await acc_task["client"].send_message(target, message=message_to_send)
                        
                        # ── Success Lifecycle ──────────────────────────────────
                        self.done_count += 1
                        acc_task["this_task_done"] += 1
                        
                        # Update DB counters using partial update ($inc/$set) for high performance
                        await TelegramAccount.find_one({"_id": db_acc.id}).update({
                            "$inc": {"messages_sent_today": 1},
                            "$set": {"last_message_sent_date": datetime.now(timezone.utc)}
                        })
                        db_acc.messages_sent_today += 1 # Sync local object

                        # Per-step cooldown
                        delay = random.randint(min_d, max_d)
                        acc_task["next_work_at"] = time.time() + delay
                        
                        pending = self.total_targets - self.done_count
                        await self.add_log("progress", f"✅ {acc_task['phone']} sent to {target} (Pending: {pending})", "SUCCESS", data={
                            "acc_id": acc_task["acc_id"],
                            "messages_sent_today": db_acc.messages_sent_today,
                            "done": self.done_count,
                            "pending": pending,
                            "total": self.total_targets,
                            "next_delay": delay
                        })
                        await terminal_manager.log_event(self.user_id, f"✅ {acc_task['phone']} messaged {target} (Pending: {pending})", acc_task["acc_id"], "msg_campaign", "SUCCESS")
                    
                    # ── Telegram Error Resilience ─────────────────────────────
                    except FloodWaitError as e:
                        acc_task["last_error_msg"] = f"FloodWait ({e.seconds}s)"
                        if e.seconds > 300: 
                            acc_task["failed"] = True
                            db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=e.seconds)
                            await db_acc.save()
                            await self.add_log("log", f"⚠️ High FloodWait. Stopping {acc_task['phone']}.", "ERROR")
                        else:
                            await self.add_log("log", f"⏳ Short FloodWait ({e.seconds}s) for {acc_task['phone']}. Cooling down.", "WARNING")
                            acc_task["next_work_at"] = time.time() + e.seconds
                            # Put target back so someone else can take it
                            if self.method == 'username' and target:
                                self.global_username_queue.insert(0, target)
                            elif self.method == 'contact' and target:
                                acc_task["targets"].insert(0, {"id": target, "username": "", "phone": ""})
                            continue 
                    except PeerFloodError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "PeerFlood (Spam Warning)"
                        db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(hours=24)
                        await db_acc.save()
                        await self.add_log("log", f"🔴 PeerFlood on {acc_task['phone']}. Account restricted.", "ERROR")
                    except (PhoneNumberBannedError, AuthKeyUnregisteredError):
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "BANNED/EXPIRED"
                        db_acc.is_active = False
                        await db_acc.save()
                        await self.add_log("log", f"❌ {acc_task['phone']} is banned/expired.", "ERROR")
                    except RPCError as e:
                        err_str = str(e)
                        if any(x in err_str for x in ["CHAT_MEMBER_ADD_FAILED", "PEER_FLOOD", "USER_BANNED_IN_CHANNEL"]):
                            acc_task["failed"] = True
                            acc_task["last_error_msg"] = "Limit Hit (24h)"
                            db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(hours=24)
                            await db_acc.save()
                            await self.add_log("log", f"🔴 {acc_task['phone']}: Critical Error ({err_str}). Account stopped for 24h.", "ERROR")
                            # Put target back
                            if self.method == 'username' and target:
                                self.global_username_queue.insert(0, target)
                            elif self.method == 'contact' and target:
                                acc_task["targets"].insert(0, {"id": target, "username": "", "phone": ""})
                        else:
                            self.errors_count += 1
                            await self.add_log("log", f"❌ RPC Error: {err_str}", "ERROR")
                        acc_task["last_error_msg"] = str(e)
                        await self.add_log("log", f"❌ Error: {str(e)}", "ERROR")
                        if "privacy" in str(e).lower():
                            acc_task["next_work_at"] = time.time() + 60 
                    except Exception as e:
                        self.errors_count += 1
                        await self.add_log("log", f"❌ Unexpected Error with {acc_task['phone']}: {str(e)}", "ERROR")
                        # Put target back if it failed
                        if self.method == 'username' and target:
                            self.global_username_queue.insert(0, target)
                        elif self.method == 'contact' and target:
                            # Re-add to this account's local targets
                            acc_task["targets"].insert(0, {"id": target, "username": "", "phone": ""})

                    # Per-step cooldown
                    delay = random.randint(min_d, max_d)
                    acc_task["next_work_at"] = time.time() + delay
                    
                    await self.add_log("log", f"💤 {acc_task['phone']} waiting for {delay}s...", "INFO", data={"next_delay": delay})
                    
                    # BREAK after processing ONE account to ensure the next loop iteration 
                    # picks the next available account in rotation (after sorting)
                    break

                if not any_working:
                    break

                if not found_ready:
                    # Optimized sleep to yield control in high-concurrency environments
                    # Reduced to 0.1s for faster response to stop_requested
                    await asyncio.sleep(0.1) 

            # ── Final Report ──────────────────────────────────────────────────
            status_event = "done"
            msg = f"🏁 Campaign Finished. Total: {self.done_count}/{self.total_targets} sent."
            if self.stop_requested:
                msg = f"🛑 Campaign Stopped by User. Final: {self.done_count} sent."
            elif not any_working and self.method == 'username' and self.global_username_queue:
                msg = f"⚠️ Campaign Finished prematurely: Accounts hit limits before queue was cleared."
            
            await self.add_log(status_event, msg, "SUCCESS" if self.done_count >= self.total_targets else "WARNING")
            await terminal_manager.log_event(self.user_id, f"🏁 Campaign Final: {self.done_count} sent.", module="msg_campaign", level="INFO")

        except Exception as e:
            await self.add_log("error", f"💥 CRITICAL: {str(e)}", "ERROR")
        finally:
            self.is_done = True
            self.status = "completed" if not self.stop_requested else "stopped"
            
            # Ensure final state is saved to DB before exiting
            await self.sync_state()
            
            # Unlock accounts in batch
            try:
                acc_ids = [ObjectId(a["acc_id"]) for a in self.accounts_to_use]
                await TelegramAccount.find({"_id": {"$in": acc_ids}}).update({"$set": {"active_task_id": None, "active_task_type": None}})
            except: pass

            # Cleanup registry after cooldown
            await asyncio.sleep(600)
            if MESSAGE_CAMPAIGN_TASKS.get(self.user_id) == self:
                del MESSAGE_CAMPAIGN_TASKS[self.user_id]
