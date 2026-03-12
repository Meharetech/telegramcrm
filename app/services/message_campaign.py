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

    async def add_log(self, event: str, message: str, level: str = "INFO", data: dict = None):
        async with self.lock:
            ts = datetime.now().strftime("%H:%M:%S")
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
            if not user or not user.services_active:
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
                if acc.flood_wait_until and acc.flood_wait_until > now_utc:
                    wait_left = (acc.flood_wait_until - now_utc).total_seconds()
                    await self.add_log("log", f"⏳ {acc.phone_number} on FloodWait for {int(wait_left)}s. Skipping.", "WARNING")
                    continue

                # Check Daily Limits
                if acc.last_contact_add_date and acc.last_contact_add_date.date() < now_utc.date():
                    acc.contacts_added_today = 0
                
                if acc.contacts_added_today >= acc.daily_contacts_limit:
                    await self.add_log("log", f"⚠️ {acc.phone_number} reached daily limit. Skipping.", "WARNING")
                    continue

                if acc.active_task_id and acc.active_task_id != self.job_id:
                    await self.add_log("log", f"⏳ {acc.phone_number} is already busy with task {acc.active_task_type}. Skipping.", "WARNING")
                    continue

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
                    # Lock account
                    acc.active_task_id = self.job_id
                    acc.active_task_type = "campaign"
                    await acc.save()
                    await self.add_log("log", f"✅ Account {acc.phone_number} ready. Goal: {config.count} messages.", "SUCCESS")
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

            if self.method == 'username':
                self.total_targets = min(len(self.username_list), sum(a["target_count"] for a in self.accounts_to_use))
            else:
                self.total_targets = sum(min(len(a["targets"]), a["target_count"]) for a in self.accounts_to_use)

            await self.add_log("status", f"📂 Campaign target: {self.total_targets} users. Starting rotation...", data={"total": self.total_targets})

            # ── Optimized Async Rotation Loop (Non-Blocking) ─────────────────
            import time
            from app.client_cache import is_user_active
            
            for acc in self.accounts_to_use:
                acc["next_work_at"] = 0

            while self.done_count < self.total_targets and not self.stop_requested:
                any_ready = False
                any_working = False
                
                # Check User Service Guard (Real-time)
                if not await is_user_active(self.user_id):
                    await self.add_log("error", "🛑 Task Aborted: User services were STOPPED by administrator.", "ERROR")
                    self.stop_requested = True
                    break

                for acc_task in self.accounts_to_use:
                    if self.stop_requested: break
                    if acc_task["failed"]: continue
                    if acc_task["this_task_done"] >= acc_task["target_count"]: continue
                    
                    any_working = True
                    now = time.time()
                    if now < acc_task["next_work_at"]:
                        continue # Cooling down
                        
                    any_ready = True

                    # ── Safety Check: Re-verify Daily Limit ───────────────────
                    db_acc = acc_task["db_acc"]
                    if db_acc.contacts_added_today >= db_acc.daily_contacts_limit:
                        acc_task["failed"] = True
                        await self.add_log("log", f"⚠️ {acc_task['phone']} hit daily limit mid-task. Retired.", "WARNING")
                        continue

                    # ── Target Selection ──────────────────────────────────────
                    target = None
                    if self.method == 'username':
                        if self.global_username_queue:
                            target = self.global_username_queue.pop(0)
                        else:
                            # Queue empty, this account is done but other methods might still run?
                            # For username method, once global queue is empty, everyone is done.
                            self.total_targets = self.done_count # Adjust total to match what we actually found
                            break 
                    elif self.method == 'contact':
                        if acc_task["targets"]:
                            target_ref = acc_task["targets"].pop(0)
                            target = target_ref['id']
                        else:
                            continue
                    
                    if not target: continue
                    
                    # ── Spintax Support (Randomization) ──────────────────────
                    import re
                    def parse_spintax(text):
                        pattern = re.compile(r'\{([^{}]*)\}')
                        while True:
                            match = pattern.search(text)
                            if not match: break
                            choices = match.group(1).split('|')
                            text = text.replace(match.group(0), random.choice(choices), 1)
                        return text

                    message_to_send = parse_spintax(self.message_text)
                    await self.add_log("log", f"⏳ {acc_task['phone']} sending to {target}...", "INFO")
                    
                    try:
                        await acc_task["client"].send_message(target, message_to_send)
                        
                        # ── Success Lifecycle ──────────────────────────────────
                        self.done_count += 1
                        acc_task["this_task_done"] += 1
                        
                        # Update DB counters
                        db_acc.contacts_added_today += 1
                        db_acc.last_contact_add_date = datetime.now(timezone.utc)
                        await db_acc.save()

                        await self.add_log("progress", f"✅ {acc_task['phone']} sent to {target}", "SUCCESS", data={"done": self.done_count})
                        await terminal_manager.log_event(self.user_id, f"✅ {acc_task['phone']} messaged {target}", acc_task["acc_id"], "msg_campaign", "SUCCESS")
                    
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
                            continue # Account will wait
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
                        self.errors_count += 1
                        acc_task["last_error_msg"] = str(e)
                        await self.add_log("log", f"❌ Error: {str(e)}", "ERROR")
                        if "privacy" in str(e).lower():
                            acc_task["next_work_at"] = time.time() + 60 # Penalty for privacy errors
                    except Exception as e:
                        self.errors_count += 1
                        await self.add_log("log", f"❌ Unexpected Error: {str(e)}", "ERROR")

                    # Per-step cooldown
                    delay = random.randint(self.min_delay, self.max_delay)
                    acc_task["next_work_at"] = time.time() + delay
                    
                    # Small throttle between accounts
                    await asyncio.sleep(0.3)

                if not any_working:
                    break

                if not any_ready:
                    # Wait for next available account
                    await asyncio.sleep(1)

            # ── Final Report ──────────────────────────────────────────────────
            status_event = "done"
            msg = f"🏁 Campaign Finished. Total: {self.done_count}/{self.total_targets} sent."
            if self.stop_requested:
                msg = f"🛑 Campaign Stopped by User. Final: {self.done_count} sent."
            elif not any_left and self.method == 'username' and self.global_username_queue:
                msg = f"⚠️ Campaign Finished prematurely: Accounts hit limits before queue was cleared."
            
            await self.add_log(status_event, msg, "SUCCESS" if self.done_count >= self.total_targets else "WARNING")
            await terminal_manager.log_event(self.user_id, f"🏁 Campaign Final: {self.done_count} sent.", module="msg_campaign", level="INFO")

        except Exception as e:
            await self.add_log("error", f"💥 CRITICAL: {str(e)}", "ERROR")
        finally:
            self.is_done = True
            self.status = "completed" if not self.stop_requested else "stopped"
            
            # Unlock accounts
            try:
                from bson import ObjectId
                acc_ids = [ObjectId(a["acc_id"]) for a in self.accounts_to_use]
                await TelegramAccount.find({"_id": {"$in": acc_ids}}).update({"$set": {"active_task_id": None, "active_task_type": None}})
            except: pass

            # Cleanup registry after cooldown
            await asyncio.sleep(600)
            if MESSAGE_CAMPAIGN_TASKS.get(self.user_id) == self:
                del MESSAGE_CAMPAIGN_TASKS[self.user_id]
