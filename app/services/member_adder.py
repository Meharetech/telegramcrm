import asyncio
import random
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from telethon import functions, types
from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError, 
    UserAlreadyParticipantError, PeerFloodError,
    UserIdInvalidError, UserNotMutualContactError,
    PhoneNumberBannedError, UserRestrictedError,
    AuthKeyUnregisteredError, FloodTestPhoneWaitError,
    UserDeletedError, UserDeactivatedError,
    InputUserDeactivatedError, UserBannedInChannelError,
    UserKickedError, UsersTooMuchError,
    UserChannelsTooMuchError, ChatAdminRequiredError,
    ChatWriteForbiddenError,
    ChannelPrivateError, ChannelInvalidError,
    InviteHashExpiredError, InviteHashInvalidError,
    PeerIdInvalidError, UsernameInvalidError,
    UsernameNotOccupiedError, RPCError
)
from app.models import TelegramAccount, MemberAddSettings, MemberAddJob
from app.client_cache import get_client
from app.services.terminal_service import terminal_manager
from app.config import settings

logger = logging.getLogger(__name__)

# Global storage for background tasks
# { user_id: ActiveMemberAdder }
MEMBER_ADDER_TASKS: Dict[str, 'ActiveMemberAdder'] = {}

class ActiveMemberAdder:
    def __init__(self, user_id: str, group_link: str, account_configs: List[any], min_delay: int, max_delay: int):
        self.user_id = user_id
        self.group_link = group_link
        self.account_configs = account_configs
        self.min_delay = min_delay
        self.max_delay = max_delay
        
        self.status = "running"
        self.done_count = 0
        self.total_count = 0
        self.errors_count = 0
        self.is_done = False
        self.logs = []
        self.queues: List[asyncio.Queue] = []
        self.lock = asyncio.Lock()
        self.stop_requested = False
        self.job_id: Optional[str] = None
        self.accounts_to_use = [] # Track stats here for DB sync
        self._is_syncing = False  # Guard against parallel MongoDB bombardment

    async def add_log(self, event: str, message: str, level: str = "INFO", data: dict = None):
        async with self.lock:
            ts = datetime.now().strftime("%H:%M:%S")
            log_entry = {
                "msg": message,
                "level": level,
                "time": ts,
                **(data or {})
            }
            
            # For SSE, we wrap it
            sse_msg = {"event": event, "data": json.dumps(log_entry)}
            self.logs.append(log_entry)
            
            # Keep last 100 logs in RAM
            if len(self.logs) > 100:
                self.logs.pop(0)
                
            for q in self.queues:
                await q.put(sse_msg)
            
            # Sync to DB on major events but at most every 5 seconds to reduce load
            if event in ["status", "progress", "done", "error"]:
                now = datetime.now()
                if not hasattr(self, 'last_sync_time') or (now - self.last_sync_time).total_seconds() >= 5 or event in ["done", "error"]:
                    if not self._is_syncing:
                        self.last_sync_time = now
                        self._is_syncing = True
                        asyncio.create_task(self.sync_state())

    async def sync_state(self):
        """Persist the task state to MongoDB for recovery and monitoring."""
        try:
            job = None
            if self.job_id and self.job_id != "None":
                job = await MemberAddJob.get(self.job_id)
            
            if not job:
                # Find most recent job for this user or create a new one
                job = await MemberAddJob.find_one(
                    MemberAddJob.user_id == self.user_id,
                    MemberAddJob.status == "running"
                )
            
            if not job:
                # Convert Pydantic objects or any non-dict to plain dict for storage
                serialized_configs = []
                for cfg in self.account_configs:
                    if hasattr(cfg, "model_dump"): serialized_configs.append(cfg.model_dump())
                    elif hasattr(cfg, "__dict__"): serialized_configs.append(cfg.__dict__)
                    else: serialized_configs.append(dict(cfg))

                job = MemberAddJob(
                    user_id=self.user_id,
                    group_link=self.group_link,
                    account_configs=serialized_configs,
                    min_delay=self.min_delay,
                    max_delay=self.max_delay,
                    status=self.status,
                    total_count=self.total_count
                )
                await job.insert()
            
            self.job_id = str(job.id)
            job.done_count = self.done_count
            job.errors_count = self.errors_count
            job.status = self.status
            job.updated_at = datetime.now(timezone.utc)
            
            # Detailed per-account health sync
            results = {}
            for acc in self.accounts_to_use:
                results[acc["acc_id"]] = {
                    "phone": acc["phone"],
                    "done": acc["this_task_done"],
                    "privacy_errors": acc["consecutive_privacy_errors"],
                    "status": "failed" if acc["failed"] else ("done" if acc.get("logged_done") else "running"),
                    "last_error": acc.get("last_error_msg", "")
                }
            job.account_results = results
            
            # Optionally sync last logs (keep more logs in DB as requested)
            job.logs = self.logs[-100:] 
            
            await job.save()
        except Exception as e:
            logger.error(f"[member_adder] DB Sync failed: {e}")
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

            await self.add_log("status", f"🚀 Initializing task for {len(self.account_configs)} accounts...")
            await terminal_manager.log_event(self.user_id, f"🚀 Starting Group Member Adding task.", module="member_adder", level="INFO")

            # Load User-Specific Mission Metadata/Settings
            m_settings = await MemberAddSettings.find_one(MemberAddSettings.user_id == self.user_id)
            if not m_settings:
                # Use global defaults if no personal settings found
                m_settings = MemberAddSettings(
                    user_id=self.user_id,
                    consecutive_privacy_threshold=settings.MA_CONSECUTIVE_PRIVACY_THRESHOLD,
                    max_flood_sleep_threshold=settings.MA_MAX_FLOOD_SLEEP_THRESHOLD,
                    account_limit_cap=settings.MA_ACCOUNT_LIMIT_CAP,
                    cooldown_24h=settings.MA_COOLDOWN_24H
                )
            
            # Prepare target group and accounts
            self.accounts_to_use = []
            now_utc = datetime.now(timezone.utc)
            
            # ── OPTIMIZED: Batch Fetch All Target Accounts ──────────────
            from bson import ObjectId
            acc_ids = [ObjectId(cfg.id) for cfg in self.account_configs]
            all_accounts = await TelegramAccount.find({"_id": {"$in": acc_ids}}).to_list()
            acc_map = {str(a.id): a for a in all_accounts}

            for config in self.account_configs:
                if self.stop_requested: break
                acc = acc_map.get(config.id)
                if not acc or not acc.is_active:
                    continue
                
                # Check for persistent FloodWait
                if acc.flood_wait_until and acc.flood_wait_until > now_utc:
                    wait_left = (acc.flood_wait_until - now_utc).total_seconds()
                    await self.add_log("log", f"⏳ {acc.phone_number} is still on FloodWait for {int(wait_left)}s. Skipping.", "WARNING")
                    continue

                # Reset daily counter if it's a new day
                if acc.last_contact_add_date and acc.last_contact_add_date.date() < now_utc.date():
                    acc.contacts_added_today = 0
                
                if acc.contacts_added_today >= acc.daily_contacts_limit:
                    await self.add_log("log", f"⚠️ {acc.phone_number} reached safety limit ({acc.daily_contacts_limit}). Skipping.", "WARNING")
                    continue
                
                if acc.active_task_id and acc.active_task_id != self.job_id:
                    await self.add_log("log", f"⏳ {acc.phone_number} is already busy with task {acc.active_task_type}. Skipping.", "WARNING")
                    continue
                
                try:
                    client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash, device_model=acc.device_model)
                    # Join group first if needed
                    # Join group first if needed (robustly)
                    from app.services.reaction.logic import ensure_joined_robust
                    await ensure_joined_robust(client, self.group_link)
                    target_group = await client.get_entity(self.group_link)
                    
                    # Fetch contacts for this account
                    res = await client(functions.contacts.GetContactsRequest(hash=0))
                    contacts = [
                        {"id": u.id, "username": u.username, "phone": u.phone} 
                        for u in res.users if not u.bot
                    ]
                    
                    if contacts:
                        self.accounts_to_use.append({
                            "db_acc": acc,
                            "acc_id": str(acc.id),
                            "phone": acc.phone_number,
                            "client": client,
                            "target_group": target_group,
                            "contacts": contacts,
                            "target_count": min(config.count, m_settings.account_limit_cap),
                            "this_task_done": 0,
                            "consecutive_privacy_errors": 0,
                            "failed": False,
                            "last_error_msg": ""
                        })
                        # Lock the account
                        acc.active_task_id = self.job_id
                        acc.active_task_type = "member_add"
                        await acc.save()
                        await self.add_log("log", f"✅ Account {acc.phone_number} ready. Goal: {config.count} additions.", "SUCCESS")
                except Exception as e:
                    reason = str(e)
                    await self.add_log("log", f"❌ Account {acc.phone_number} error: {reason}", "ERROR")
                    if "auth" in reason.lower() or "revoked" in reason.lower() or "banned" in reason.lower():
                        acc.is_active = False
                        acc.status = "error"
                        await acc.save()
                    await terminal_manager.log_event(self.user_id, f"❌ Account {acc.phone_number} failed: {reason}", str(acc.id), "member_adder", "ERROR")

            if not self.accounts_to_use:
                await self.add_log("error", "❌ No accounts available to proceed.", "ERROR")
                await terminal_manager.log_event(self.user_id, "❌ No available accounts to perform adding.", module="member_adder", level="ERROR")
                return

            self.total_count = sum(min(len(a["contacts"]), a["target_count"]) for a in self.accounts_to_use)
            await self.add_log("status", f"📂 Rotation target: {self.total_count} members. Starting cycle...", data={"total": self.total_count})
            await terminal_manager.log_event(self.user_id, f"📂 Rotation started for target: {self.total_count}.", module="member_adder", level="INFO")

            # 2. Optimized Async Rotation Loop
            import time
            for acc in self.accounts_to_use:
                acc["next_work_at"] = 0

            while self.done_count < self.total_count and not self.stop_requested:
                # ── Real-time User Guard (Admin Stop) ─────────────────
                from app.client_cache import is_user_active
                if not await is_user_active(self.user_id):
                    self.stop_requested = True
                    await self.add_log("error", "🛑 Mission Aborted: Services deactivated by administrator.", "ERROR")
                    break

                any_ready = False
                any_working = False
                
                for i, acc_task in enumerate(self.accounts_to_use):
                    if self.stop_requested: break
                    if acc_task["failed"] or not acc_task["contacts"]:
                        continue
                    
                    if acc_task["this_task_done"] >= acc_task["target_count"]:
                        if acc_task.get("logged_done") is not True:
                            await self.add_log("log", f"🎯 {acc_task['phone']} goal reached ({acc_task['target_count']}). Account retired.", "INFO")
                            await terminal_manager.log_event(self.user_id, f"🎯 Account {acc_task['phone']} reached goal.", acc_task["acc_id"], "member_adder", "INFO")
                            acc_task["logged_done"] = True
                        continue
                        
                    any_working = True
                    now = time.time()
                    if now < acc_task.get("next_work_at", 0):
                        continue # Cooling down
                        
                    any_ready = True
                    # Refresh DB object to check current limit during long tasks
                    db_acc = acc_task["db_acc"]
                    if db_acc.contacts_added_today >= db_acc.daily_contacts_limit:
                        acc_task["failed"] = True
                        await self.add_log("log", f"⚠️ {acc_task['phone']} reached daily safety limit. Account retired.", "WARNING")
                        continue

                    target = acc_task["contacts"].pop(0)
                    id_label = f"@{target['username']}" if target['username'] else f"+{target['phone']}" if target['phone'] else f"ID:{target['id']}"
                    
                    await self.add_log("log", f"⏳ {acc_task['phone']} processing: {id_label}...", "INFO")
                    
                    try:
                        await acc_task["client"](functions.channels.InviteToChannelRequest(
                            channel=acc_task["target_group"],
                            users=[target["id"]]
                        ))
                        
                        self.done_count += 1
                        acc_task["this_task_done"] += 1
                        progress_str = f"[{acc_task['this_task_done']}/{acc_task['target_count']}]"
                        await self.add_log("progress", f"✅ {acc_task['phone']} {progress_str} successfully added {id_label}", "SUCCESS", data={
                            "done": self.done_count,
                            "added": 1
                        })
                        
                        # Update DB
                        db_acc.contacts_added_today += 1
                        db_acc.last_contact_add_date = datetime.now(timezone.utc)
                        await db_acc.save()
                        
                        await terminal_manager.log_event(self.user_id, f"✅ {acc_task['phone']} added {id_label}", acc_task["acc_id"], "member_adder", "SUCCESS")
                        acc_task["consecutive_privacy_errors"] = 0
                    
                    except (UserPrivacyRestrictedError, UserNotMutualContactError) as e:
                        acc_task["consecutive_privacy_errors"] += 1
                        acc_task["last_error_msg"] = "Privacy Restricted"
                        await self.add_log("log", f"ℹ️ Privacy restricted: {id_label}", "WARNING")
                        if acc_task["consecutive_privacy_errors"] >= m_settings.consecutive_privacy_threshold:
                            acc_task["failed"] = True
                            await self.add_log("log", f"⚠️ {acc_task['phone']} hit {m_settings.consecutive_privacy_threshold} consecutive privacy errors. Stopping account for safety.", "ERROR")

                    except UserAlreadyParticipantError:
                        await self.add_log("log", f"ℹ️ Already in group: {id_label}", "WARNING")

                    except FloodWaitError as e:
                        acc_task["last_error_msg"] = f"FloodWait ({e.seconds}s)"
                        if e.seconds > m_settings.max_flood_sleep_threshold:
                            acc_task["failed"] = True
                            # Persist this wait time to DB
                            db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=e.seconds)
                            await db_acc.save()
                            await self.add_log("log", f"⚠️ High FloodWait ({e.seconds}s). Stopping account for safety (Threshold: {m_settings.max_flood_sleep_threshold}s).", "ERROR")
                        else:
                            await self.add_log("log", f"⏳ Short FloodWait ({e.seconds}s). Sleeping...", "WARNING")
                            await asyncio.sleep(e.seconds)

                    except PeerFloodError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "PeerFlood (Spam Warning)"
                        db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(hours=24) # 24h default for PeerFlood
                        await db_acc.save()
                        
                        cooldown_hrs = 24
                        await self.add_log("log", f"🔴 CRITICAL: PeerFloodError detected. Stopping {acc_task['phone']} ({cooldown_hrs}h cooldown recommended).", "ERROR")
                        await terminal_manager.log_event(self.user_id, f"🔴 PeerFlood on {acc_task['phone']}. Node stopped.", acc_task["acc_id"], "member_adder", "ERROR")

                    except UserRestrictedError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "Account Restricted"
                        await self.add_log("log", f"🔴 CRITICAL: Account restricted by Telegram. Stopping {acc_task['phone']}.", "ERROR")

                    except PhoneNumberBannedError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "BANNED"
                        db_acc.is_active = False
                        db_acc.status = "banned"
                        await db_acc.save()
                        await self.add_log("log", f"❌ PERMANENT BAN: {acc_task['phone']} is banned. Account retired.", "ERROR")
                        await terminal_manager.log_event(self.user_id, f"❌ PERMANENT BAN: {acc_task['phone']}", acc_task["acc_id"], "member_adder", "ERROR")

                    except AuthKeyUnregisteredError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "Session Expired"
                        db_acc.is_active = False
                        db_acc.status = "error"
                        await db_acc.save()
                        await self.add_log("log", f"❌ SESSION EXPIRED: {acc_task['phone']} needs re-auth. Stopping account.", "ERROR")

                    except (UserDeletedError, UserDeactivatedError, InputUserDeactivatedError):
                        await self.add_log("log", f"ℹ️ Target deleted/deactivated: {id_label}", "WARNING")

                    except UsersTooMuchError:
                        self.stop_requested = True
                        await self.add_log("error", "🛑 Group is full. Mission terminated.", "ERROR")

                    except (ChatAdminRequiredError, ChatWriteForbiddenError, ChannelPrivateError):
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "No Permission"
                        await self.add_log("log", f"❌ Permission denied for {acc_task['phone']}. Check if admin/joined.", "ERROR")

                    except (InviteHashExpiredError, InviteHashInvalidError):
                        self.stop_requested = True
                        await self.add_log("error", "🛑 Invalid or expired group link.", "ERROR")

                    except Exception as e:
                        err_str = str(e)
                        if "privacy" in err_str.lower():
                            acc_task["consecutive_privacy_errors"] += 1
                            await self.add_log("log", f"ℹ️ Privacy restricted: {id_label}", "WARNING")
                            if acc_task["consecutive_privacy_errors"] >= m_settings.consecutive_privacy_threshold:
                                acc_task["failed"] = True
                                await self.add_log("log", f"⚠️ {acc_task['phone']} hit consecutive privacy errors threshold. Stopping account.", "ERROR")
                        else:
                            self.errors_count += 1
                            await self.add_log("log", f"❌ Error adding {id_label}: {err_str}", "ERROR", data={"errors": self.errors_count})

                    # Per-step rotation delay (Non-blocking)
                    delay = random.randint(self.min_delay, self.max_delay)
                    acc_task["next_work_at"] = time.time() + delay
                    
                    # Small throttle between different accounts to look more organic
                    await asyncio.sleep(0.3)

                if not any_working:
                    break

                if not any_ready:
                    # No account is ready yet, wait a bit
                    await asyncio.sleep(1)

            if self.stop_requested:
                await self.add_log("done", f"🛑 Mission Stopped. Final Sync: {self.done_count} added, {self.errors_count} errors.", "WARNING", data={"done": self.done_count, "total": self.total_count, "errors": self.errors_count})
            else:
                summary = f"🏁 Task completed. Final: {self.done_count}/{self.total_count} members added."
                if self.done_count < self.total_count:
                    summary += " (Some accounts hit limits or were finished)"
                await self.add_log("done", summary, "SUCCESS", data={"done": self.done_count, "total": self.total_count, "errors": self.errors_count})
            
            await terminal_manager.log_event(self.user_id, f"🏁 Group Adding mission finished: {self.done_count} added.", module="member_adder", level="INFO")
            
            await terminal_manager.log_event(self.user_id, "🏁 Member adding task status final.", module="member_adder", level="SUCCESS")

        except Exception as e:
            await self.add_log("error", f"💥 CRITICAL ERROR: {str(e)}", "ERROR")
        finally:
            self.is_done = True
            self.status = "completed" if not self.stop_requested else "stopped"
            
            # Unlock accounts
            try:
                from bson import ObjectId
                acc_ids = [ObjectId(a["acc_id"]) for a in self.accounts_to_use]
                await TelegramAccount.find({"_id": {"$in": acc_ids}}).update({"$set": {"active_task_id": None, "active_task_type": None}})
            except: pass

            # Keep in global tasks for 10 minutes so user can see final status
            await asyncio.sleep(600)
            if MEMBER_ADDER_TASKS.get(self.user_id) == self:
                del MEMBER_ADDER_TASKS[self.user_id]
