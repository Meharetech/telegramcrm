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
from bson import ObjectId

logger = logging.getLogger(__name__)

# Global storage for background tasks
# { user_id: ActiveMemberAdder }
MEMBER_ADDER_TASKS: Dict[str, 'ActiveMemberAdder'] = {}


def _get_cfg_attr(cfg, key, default=None):
    """Safely get attribute from either a Pydantic model or a plain dict."""
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


class ActiveMemberAdder:
    def __init__(self, user_id: str, group_link: str, account_configs: List[any], min_delay: int, max_delay: int):
        self.user_id = user_id
        self.group_link = group_link
        self.account_configs = account_configs
        self.min_delay = min_delay
        self.max_delay = max_delay

        # Source configuration — set from outside before calling run()
        self.source_type = "contacts"   # "contacts" or "custom_list"
        self.member_list: List[str] = []  # populated if source_type == "custom_list"

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
        self.accounts_to_use = []
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
                if not hasattr(self, 'last_sync_time') or (now - self.last_sync_time).total_seconds() >= 5 or event in ["done", "error"]:
                    if not self._is_syncing:
                        self.last_sync_time = now
                        self._is_syncing = True
                        asyncio.create_task(self.sync_state())

    async def sync_state(self):
        """Persist the task state to MongoDB."""
        try:
            job = None
            if self.job_id and self.job_id != "None":
                job = await MemberAddJob.get(self.job_id)
            if not job:
                job = await MemberAddJob.find_one(
                    MemberAddJob.user_id == self.user_id,
                    MemberAddJob.status == "running"
                )
            if not job:
                serialized_configs = []
                for cfg in self.account_configs:
                    if hasattr(cfg, "model_dump"):
                        serialized_configs.append(cfg.model_dump())
                    elif hasattr(cfg, "__dict__"):
                        serialized_configs.append(cfg.__dict__)
                    elif isinstance(cfg, dict):
                        serialized_configs.append(cfg)
                    else:
                        serialized_configs.append(dict(cfg))

                job = MemberAddJob(
                    user_id=self.user_id,
                    group_link=self.group_link,
                    account_configs=serialized_configs,
                    min_delay=self.min_delay,
                    max_delay=self.max_delay,
                    source_type=self.source_type,
                    member_list=self.member_list,
                    status=self.status,
                    total_count=self.total_count
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
                    "privacy_errors": acc["consecutive_privacy_errors"],
                    "status": "failed" if acc["failed"] else ("done" if acc.get("logged_done") else "running"),
                    "last_error": acc.get("last_error_msg", "")
                }
            job.account_results = results
            job.logs = self.logs[-100:]
            await job.save()
        except Exception as e:
            logger.error(f"[member_adder] DB Sync failed: {e}")
        finally:
            self._is_syncing = False

    async def run(self):
        try:
            # ── Step 0: User Service Guard ────────────────────────────────────
            from app.models.user import User
            user = await User.get(self.user_id)
            if not user or not user.services_active:
                await self.add_log("error", "🛑 Task Aborted: User services are currently STOPPED.", "ERROR")
                return

            source_label = "Custom Username List" if self.source_type == "custom_list" else "Account Contacts"
            await self.add_log("status", f"🚀 Initializing task for {len(self.account_configs)} accounts | Source: {source_label}...")
            await terminal_manager.log_event(self.user_id, f"🚀 Starting Group Member Adding task [{source_label}].", module="member_adder", level="INFO")

            # ── Step 1: Load mission settings ────────────────────────────────
            m_settings = await MemberAddSettings.find_one(MemberAddSettings.user_id == self.user_id)
            if not m_settings:
                m_settings = MemberAddSettings(
                    user_id=self.user_id,
                    consecutive_privacy_threshold=settings.MA_CONSECUTIVE_PRIVACY_THRESHOLD,
                    max_flood_sleep_threshold=settings.MA_MAX_FLOOD_SLEEP_THRESHOLD,
                    account_limit_cap=settings.MA_ACCOUNT_LIMIT_CAP,
                    cooldown_24h=settings.MA_COOLDOWN_24H
                )

            # ── Step 2: Validate custom member list ───────────────────────────
            if self.source_type == "custom_list":
                if not self.member_list:
                    await self.add_log("error", "❌ Custom List mode selected but member list is EMPTY. Aborting.", "ERROR")
                    return
                await self.add_log("log", f"📋 Custom list loaded: {len(self.member_list)} members ready.", "INFO")

            # ── Step 3: Prepare accounts ──────────────────────────────────────
            self.accounts_to_use = []
            now_utc = datetime.now(timezone.utc)

            # Batch-fetch all accounts at once
            acc_ids = [ObjectId(_get_cfg_attr(cfg, "id")) for cfg in self.account_configs]
            all_accounts = await TelegramAccount.find({"_id": {"$in": acc_ids}}).to_list()
            acc_map = {str(a.id): a for a in all_accounts}

            for config in self.account_configs:
                if self.stop_requested:
                    break

                cfg_id = _get_cfg_attr(config, "id")
                cfg_count = _get_cfg_attr(config, "count", 25)

                acc = acc_map.get(cfg_id)
                if not acc or not acc.is_active:
                    continue

                # FloodWait check
                flood_until = acc.flood_wait_until
                if flood_until:
                    if flood_until.tzinfo is None:
                        flood_until = flood_until.replace(tzinfo=timezone.utc)
                    
                    if flood_until > now_utc:
                        wait_left = (flood_until - now_utc).total_seconds()
                        await self.add_log("log", f"⏳ {acc.phone_number} on FloodWait for {int(wait_left)}s. Skipping.", "WARNING")
                        continue

                # Daily limit check
                if acc.last_contact_add_date and acc.last_contact_add_date.date() < now_utc.date():
                    acc.contacts_added_today = 0
                if acc.contacts_added_today >= acc.daily_contacts_limit:
                    await self.add_log("log", f"⚠️ {acc.phone_number} reached daily limit ({acc.daily_contacts_limit}). Skipping.", "WARNING")
                    continue

                # Busy check
                if (acc.active_task_id and acc.active_task_id != self.job_id):
                    await self.add_log("log", f"ℹ️ {acc.phone_number} is currently busy with {acc.active_task_type}. Proceeding anyway.", "WARNING")
                    # No longer skipping. User requested to use busy accounts.

                try:
                    client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash, device_model=acc.device_model)

                    # Join target group if needed
                    from app.services.reaction.logic import ensure_joined_robust
                    await ensure_joined_robust(client, self.group_link)
                    target_group = await client.get_entity(self.group_link)

                    target_count = min(int(cfg_count), m_settings.account_limit_cap)

                    self.accounts_to_use.append({
                        "db_acc": acc,
                        "acc_id": str(acc.id),
                        "phone": acc.phone_number,
                        "client": client,
                        "target_group": target_group,
                        "target_count": target_count,
                        "this_task_done": 0,
                        "consecutive_privacy_errors": 0,
                        "failed": False,
                        "last_error_msg": "",
                        "contacts": []  # filled in Step 4
                    })

                    # Lock the account to this task
                    acc.active_task_id = self.job_id
                    acc.active_task_type = "member_add"
                    await acc.save()
                    await self.add_log("log", f"✅ Account {acc.phone_number} ready. Goal: {target_count} additions.", "SUCCESS")

                except Exception as e:
                    reason = str(e)
                    await self.add_log("log", f"❌ Account {acc.phone_number} setup error: {reason}", "ERROR")
                    if "auth" in reason.lower() or "revoked" in reason.lower() or "banned" in reason.lower():
                        acc.is_active = False
                        acc.status = "error"
                        await acc.save()
                    await terminal_manager.log_event(self.user_id, f"❌ Account {acc.phone_number} failed: {reason}", str(acc.id), "member_adder", "ERROR")

            if not self.accounts_to_use:
                await self.add_log("error", "❌ No accounts available to proceed. Check account status.", "ERROR")
                await terminal_manager.log_event(self.user_id, "❌ No available accounts to perform adding.", module="member_adder", level="ERROR")
                return

            # ── Step 4: Member Distribution & Queue Initialization ───────────────
            # For 1000+ member missions, we use a central queue to ensure smoothness.
            self.mission_queue = [] # This will hold the remaining members for the whole mission
            
            if self.source_type == "custom_list":
                await self.add_log("log", f"📂 Preparing global mission queue for {len(self.member_list)} usernames...", "INFO")
                for entry in self.member_list:
                    entry = str(entry).strip()
                    if not entry: continue
                    
                    stripped = entry.lstrip("-")
                    if stripped.isdigit():
                        self.mission_queue.append({"id": int(entry), "username": None, "phone": None})
                    else:
                        clean = entry.lstrip("@")
                        self.mission_queue.append({"id": clean, "username": clean, "phone": None})
            else:
                # For Contacts mode, we still fetch per-account as contacts are private to each session
                await self.add_log("log", "📂 Fetching contacts from individual Telegram accounts...", "INFO")
                for acc_task in self.accounts_to_use:
                    if acc_task["failed"]: continue
                    try:
                        res = await acc_task["client"](functions.contacts.GetContactsRequest(hash=0))
                        # Store contacts inside the account task specifically
                        acc_task["contacts"] = [
                            {"id": u.id, "username": u.username, "phone": u.phone}
                            for u in res.users if not u.bot
                        ]
                        await self.add_log("log", f"📋 {acc_task['phone']}: {len(acc_task['contacts'])} contacts fetched.", "INFO")
                    except Exception as e:
                        await self.add_log("log", f"⚠️ Failed to fetch contacts for {acc_task['phone']}: {str(e)}", "WARNING")
                        acc_task["contacts"] = []

            # Filter accounts
            if self.source_type == "custom_list":
                self.accounts_to_use = [a for a in self.accounts_to_use if not a["failed"]]
            else:
                self.accounts_to_use = [a for a in self.accounts_to_use if not a["failed"] and a["contacts"]]

            if not self.accounts_to_use or (self.source_type == "custom_list" and not self.mission_queue):
                await self.add_log("error", "❌ Mission Aborted: No members to add or no active accounts.", "ERROR")
                return

            self.total_count = len(self.mission_queue) if self.source_type == "custom_list" else sum(min(len(a["contacts"]), a["target_count"]) for a in self.accounts_to_use)
            await self.add_log("status", f"📂 Mission ready: {self.total_count} additions planned using {len(self.accounts_to_use)} accounts.", "SUCCESS", data={"total": self.total_count})
            await terminal_manager.log_event(self.user_id, f"📂 Member adding mission started. Total targets: {self.total_count}.", module="member_adder", level="INFO")

            # ── Step 5: Rotation Loop ─────────────────────────────────────────
            import time
            for acc in self.accounts_to_use:
                acc["next_work_at"] = 0

            while self.done_count < self.total_count and not self.stop_requested:
                # Real-time admin stop check
                from app.client_cache import is_user_active
                if not await is_user_active(self.user_id):
                    self.stop_requested = True
                    await self.add_log("error", "🛑 Mission Aborted: Services deactivated by administrator.", "ERROR")
                    break

                any_working = False
                any_ready = False

                for acc_task in self.accounts_to_use:
                    if self.stop_requested: break
                    if acc_task["failed"]: continue
                    
                    # Check account-specific target limit for THIS mission
                    if acc_task["this_task_done"] >= acc_task["target_count"]:
                        if not acc_task.get("logged_done"):
                            await self.add_log("log", f"🎯 {acc_task['phone']} goal reached. Retiring from mission.", "INFO")
                            acc_task["logged_done"] = True
                        continue

                    # If custom list, check if mission queue is empty
                    if self.source_type == "custom_list" and not self.mission_queue:
                        continue

                    # If contacts mode, check if account list is empty
                    if self.source_type == "contacts" and not acc_task["contacts"]:
                        continue

                    any_working = True
                    now = time.time()
                    if now < acc_task.get("next_work_at", 0):
                        continue  # Still in delay

                    any_ready = True
                    db_acc = acc_task["db_acc"]
                    
                    # Get next target
                    if self.source_type == "custom_list":
                        target = self.mission_queue.pop(0)
                    else:
                        target = acc_task["contacts"].pop(0)

                    # Determine display label and the actual user entity to invite
                    if target["username"]:
                        id_label = f"@{target['username']}"
                        user_input = target["username"]
                    elif isinstance(target["id"], int):
                        id_label = f"ID:{target['id']}"
                        user_input = target["id"]
                    elif target["phone"]:
                        id_label = f"+{target['phone']}"
                        user_input = target["phone"]
                    else:
                        id_label = str(target["id"])
                        user_input = target["id"]

                    await self.add_log("log", f"⏳ {acc_task['phone']} → {id_label}...", "INFO")

                    try:
                        # ── Connection Guard ──
                        from app.client_cache import touch
                        touch(acc_task["acc_id"])
                        
                        if not acc_task["client"].is_connected():
                            await self.add_log("log", f"🔄 {acc_task['phone']} disconnected. Reconnecting...", "WARNING")
                            acc_task["client"] = await get_client(acc_task["acc_id"])

                        # Resolve the entity first so Telethon knows what type it is
                        if self.stop_requested: break
                        resolved = await acc_task["client"].get_input_entity(user_input)
                        if self.stop_requested: break
                        await acc_task["client"](functions.channels.InviteToChannelRequest(
                            channel=acc_task["target_group"],
                            users=[resolved]
                        ))

                        self.done_count += 1
                        acc_task["this_task_done"] += 1
                        acc_task["consecutive_privacy_errors"] = 0
                        progress_str = f"[{acc_task['this_task_done']}/{acc_task['target_count']}]"
                        await self.add_log("progress", f"✅ {acc_task['phone']} {progress_str} added {id_label}", "SUCCESS", data={
                            "acc_id": acc_task["acc_id"],
                            "contacts_added_today": db_acc.contacts_added_today,
                            "done": self.done_count,
                            "added": 1
                        })
                        db_acc.contacts_added_today += 1
                        db_acc.last_contact_add_date = datetime.now(timezone.utc)
                        await db_acc.save()
                        await terminal_manager.log_event(self.user_id, f"✅ {acc_task['phone']} added {id_label}", acc_task["acc_id"], "member_adder", "SUCCESS")

                    except (UserPrivacyRestrictedError, UserNotMutualContactError):
                        acc_task["consecutive_privacy_errors"] += 1
                        acc_task["last_error_msg"] = "Privacy Restricted"
                        await self.add_log("log", f"ℹ️ Privacy restricted: {id_label}", "WARNING")
                        if acc_task["consecutive_privacy_errors"] >= m_settings.consecutive_privacy_threshold:
                            acc_task["failed"] = True
                            await self.add_log("log", f"⚠️ {acc_task['phone']} hit {m_settings.consecutive_privacy_threshold} consecutive privacy errors. Retired.", "ERROR")

                    except UserAlreadyParticipantError:
                        await self.add_log("log", f"ℹ️ Already in group: {id_label}", "WARNING")

                    except (UsernameNotOccupiedError, UsernameInvalidError, UserIdInvalidError, PeerIdInvalidError):
                        await self.add_log("log", f"⚠️ Invalid/not found: {id_label}. Skipping.", "WARNING")

                    except FloodWaitError as e:
                        acc_task["last_error_msg"] = f"FloodWait ({e.seconds}s)"
                        if e.seconds > m_settings.max_flood_sleep_threshold:
                            acc_task["failed"] = True
                            db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=e.seconds)
                            await db_acc.save()
                            await self.add_log("log", f"⚠️ High FloodWait ({e.seconds}s). Retiring account (threshold: {m_settings.max_flood_sleep_threshold}s).", "ERROR")
                        else:
                            await self.add_log("log", f"⏳ Short FloodWait ({e.seconds}s). Sleeping...", "WARNING")
                            await asyncio.sleep(e.seconds)

                    except PeerFloodError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "PeerFlood (Spam Warning)"
                        db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(hours=24)
                        await db_acc.save()
                        await self.add_log("log", f"🔴 PeerFlood on {acc_task['phone']}. 24h cooldown applied.", "ERROR")
                        await terminal_manager.log_event(self.user_id, f"🔴 PeerFlood on {acc_task['phone']}.", acc_task["acc_id"], "member_adder", "ERROR")

                    except (UserRestrictedError, UserBannedInChannelError, UserKickedError):
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "Account Restricted"
                        await self.add_log("log", f"🔴 {acc_task['phone']} restricted by Telegram. Retired.", "ERROR")

                    except PhoneNumberBannedError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "BANNED"
                        db_acc.is_active = False
                        db_acc.status = "banned"
                        await db_acc.save()
                        await self.add_log("log", f"❌ PERMANENT BAN: {acc_task['phone']}. Account retired.", "ERROR")

                    except AuthKeyUnregisteredError:
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "Session Expired"
                        db_acc.is_active = False
                        db_acc.status = "error"
                        await db_acc.save()
                        await self.add_log("log", f"❌ SESSION EXPIRED: {acc_task['phone']}. Re-auth needed.", "ERROR")

                    except (UserDeletedError, UserDeactivatedError, InputUserDeactivatedError):
                        await self.add_log("log", f"ℹ️ Target deleted/deactivated: {id_label}", "WARNING")

                    except UsersTooMuchError:
                        self.stop_requested = True
                        await self.add_log("error", "🛑 Group is full! Mission terminated.", "ERROR")

                    except (ChatAdminRequiredError, ChatWriteForbiddenError, ChannelPrivateError):
                        acc_task["failed"] = True
                        acc_task["last_error_msg"] = "No Permission"
                        await self.add_log("log", f"❌ Permission denied for {acc_task['phone']}. Is the account an admin?", "ERROR")

                    except (InviteHashExpiredError, InviteHashInvalidError):
                        self.stop_requested = True
                        await self.add_log("error", "🛑 Invalid or expired group invite link.", "ERROR")

                    except RPCError as e:
                        err_str = str(e)
                        if "CHAT_MEMBER_ADD_FAILED" in err_str:
                            acc_task["failed"] = True
                            acc_task["last_error_msg"] = "Add Failed (24h)"
                            db_acc.flood_wait_until = datetime.now(timezone.utc) + timedelta(hours=24)
                            await db_acc.save()
                            await self.add_log("log", f"🔴 {acc_task['phone']}: CHAT_MEMBER_ADD_FAILED. Account stopped for 24h.", "ERROR")
                            await terminal_manager.log_event(self.user_id, f"🔴 {acc_task['phone']}: Member Add Failed. 24h Cooldown.", acc_task["acc_id"], "member_adder", "ERROR")
                        else:
                            self.errors_count += 1
                            await self.add_log("log", f"❌ RPC Error: {err_str}", "ERROR", data={"errors": self.errors_count})

                    except Exception as e:
                        err_str = str(e)
                        if "privacy" in err_str.lower():
                            acc_task["consecutive_privacy_errors"] += 1
                            await self.add_log("log", f"ℹ️ Privacy restricted: {id_label}", "WARNING")
                            if acc_task["consecutive_privacy_errors"] >= m_settings.consecutive_privacy_threshold:
                                acc_task["failed"] = True
                                await self.add_log("log", f"⚠️ {acc_task['phone']} hit privacy threshold. Retired.", "ERROR")
                        else:
                            self.errors_count += 1
                            await self.add_log("log", f"❌ Unexpected error adding {id_label}: {err_str}", "ERROR", data={"errors": self.errors_count})

                    # Per-step delay
                    delay = random.randint(self.min_delay, self.max_delay)
                    acc_task["next_work_at"] = time.time() + delay
                    await asyncio.sleep(0.3)

                if not any_working:
                    break
                if not any_ready:
                    # Reduced to 0.1s for faster response to stop_requested
                    await asyncio.sleep(0.1)

            # ── Step 6: Mission Summary ────────────────────────────────────────
            if self.stop_requested:
                await self.add_log("done", f"🛑 Mission Stopped. Final: {self.done_count} added, {self.errors_count} errors.", "WARNING",
                                   data={"done": self.done_count, "total": self.total_count, "errors": self.errors_count})
            else:
                summary = f"🏁 Mission Complete! {self.done_count}/{self.total_count} members added."
                if self.done_count < self.total_count:
                    summary += " (Some accounts hit limits or list exhausted)"
                await self.add_log("done", summary, "SUCCESS",
                                   data={"done": self.done_count, "total": self.total_count, "errors": self.errors_count})

            await terminal_manager.log_event(self.user_id, f"🏁 Member adding finished: {self.done_count} added.", module="member_adder", level="INFO")

        except Exception as e:
            await self.add_log("error", f"💥 CRITICAL ERROR: {str(e)}", "ERROR")
            logger.exception(f"[member_adder] Critical error for user {self.user_id}: {e}")
        finally:
            self.is_done = True
            self.status = "completed" if not self.stop_requested else "stopped"
            await self.sync_state()

            # Unlock accounts
            try:
                acc_ids_to_unlock = [ObjectId(a["acc_id"]) for a in self.accounts_to_use]
                if acc_ids_to_unlock:
                    await TelegramAccount.find({"_id": {"$in": acc_ids_to_unlock}}).update(
                        {"$set": {"active_task_id": None, "active_task_type": None}}
                    )
            except Exception as e:
                logger.error(f"[member_adder] Failed to unlock accounts: {e}")

            # Keep in memory for 10 minutes so user can see final status
            await asyncio.sleep(600)
            if MEMBER_ADDER_TASKS.get(self.user_id) == self:
                del MEMBER_ADDER_TASKS[self.user_id]
