import asyncio
import random
import logging
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from telethon import functions, types
from telethon.errors import (
    FloodWaitError, RPCError, PhoneNumberBannedError, AuthKeyUnregisteredError, 
    SessionRevokedError, PeerFloodError, InviteHashExpiredError, InviteHashInvalidError,
    ChannelInvalidError, UsernameInvalidError, UsernameNotOccupiedError,
    UserAlreadyParticipantError, ChannelsTooMuchError, UsersTooMuchError,
    ChannelPrivateError, ChatAdminRequiredError, UserBannedInChannelError,
    UserRestrictedError
)
from app.models import TelegramAccount, GroupJoinJob
from app.client_cache import get_client
from app.services.terminal_service import terminal_manager
from bson import ObjectId

logger = logging.getLogger(__name__)

GROUP_JOIN_TASKS: Dict[str, Dict[str, 'ActiveGroupJoiner']] = {}
GROUP_JOIN_MANAGERS: Dict[str, 'GroupJoinRotationManager'] = {}

class ActiveGroupJoiner:
    def __init__(self, user_id: str, account_id: str, phone_number: str, links: List[str], interval: int, batch_id: str = None, task_type: str = "join"):
        self.user_id = user_id
        self.batch_id = batch_id
        self.task_type = task_type
        self.account_id = account_id
        self.phone_number = phone_number
        self.links = links
        self.interval = interval
        
        self.status = "running"
        self.done_count = 0
        self.total_count = len(links)
        self.logs = []
        self.queues = []
        self.stop_requested = False
        self.is_done = False
        self.job_id = None
        self.lock = asyncio.Lock()
        self.client = None
        self.flood_wait_until = 0

    async def get_client(self):
        if self.client and self.client.is_connected():
            return self.client
        
        acc = await TelegramAccount.get(ObjectId(self.account_id))
        if not acc or not acc.is_active:
            self.status = "error"
            await self.add_log("error", "❌ Account not found or inactive.", "ERROR")
            return None
        
        self.client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash)
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def join_one(self, link: str, link_idx: int = None, total_links: int = None):
        if self.status != "running": return False
        
        clean_link = link.strip().replace("https://t.me/", "").replace("t.me/", "").replace("@", "")
        if not clean_link: return False

        idx_str = f"[{link_idx}/{total_links}] " if link_idx else ""
        try:
            verb_ing = "joining" if self.task_type == "join" else "leaving"
            await self.add_log("log", f"{idx_str}⏳ Attempting to {verb_ing.replace('ing', '')}: {clean_link}...", "INFO")
            
            client = await self.get_client()
            if not client:
                self.status = "error"
                return False

            # Join/Leave logic with timeout
            from telethon.tl.functions.messages import ImportChatInviteRequest
            from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
            
            try:
                if self.task_type == "join":
                    if "joinchat/" in link or "+" in link:
                        hash_code = link.split("/")[-1].replace("+", "").replace("https://t.me/", "").replace("t.me/", "")
                        await asyncio.wait_for(client(ImportChatInviteRequest(hash_code)), timeout=30)
                    else:
                        await asyncio.wait_for(client(JoinChannelRequest(clean_link)), timeout=30)
                    
                    self.done_count += 1
                    await self.add_log("progress", f"{idx_str}✅ Joined: {clean_link}", "SUCCESS", data={"done": self.done_count, "total": self.total_count})
                    return True
                else:
                    await asyncio.wait_for(client(LeaveChannelRequest(clean_link)), timeout=30)
                    self.done_count += 1
                    await self.add_log("progress", f"{idx_str}✅ Left: {clean_link}", "SUCCESS", data={"done": self.done_count, "total": self.total_count})
                    return True
                
            except asyncio.TimeoutError:
                await self.add_log("log", f"{idx_str}⚠️ Timeout {verb_ing} {clean_link}. Skipping...", "WARNING")
                return False
            except InviteHashExpiredError:
                await self.add_log("log", f"{idx_str}⚠️ Link expired: {clean_link}", "WARNING")
                return False
            except InviteHashInvalidError:
                await self.add_log("log", f"{idx_str}⚠️ Invalid link: {clean_link}", "WARNING")
                return False
            except ChannelInvalidError:
                await self.add_log("log", f"{idx_str}⚠️ Invalid Channel/Group: {clean_link}", "WARNING")
                return False
            except UsernameInvalidError:
                await self.add_log("log", f"{idx_str}⚠️ Incorrect link/username: {clean_link}", "WARNING")
                return False
            except UsernameNotOccupiedError:
                await self.add_log("log", f"{idx_str}⚠️ Username doesn't exist: {clean_link}", "WARNING")
                return False
            except UserAlreadyParticipantError:
                self.done_count += 1
                await self.add_log("progress", f"{idx_str}🤝 Already a member: {clean_link}", "SUCCESS", data={"done": self.done_count, "total": self.total_count})
                return True

            except UsersTooMuchError:
                await self.add_log("log", f"{idx_str}⚠️ Group full: {clean_link}", "WARNING")
                return False
            except ChannelPrivateError:
                await self.add_log("log", f"{idx_str}⚠️ Private group access denied: {clean_link}", "WARNING")
                return False
            except ChatAdminRequiredError:
                await self.add_log("log", f"{idx_str}⚠️ Admin permission required: {clean_link}", "WARNING")
                return False
            except UserBannedInChannelError:
                await self.add_log("log", f"{idx_str}⚠️ Banned from group: {clean_link}", "WARNING")
                return False
            except ChannelsTooMuchError:
                self.status = "error"
                await self.add_log("error", "❌ Joined too many groups. Stopping account.", "ERROR")
                return False
            except UserRestrictedError:
                self.status = "error"
                await self.add_log("error", "❌ Account restricted by Telegram. Stopping account.", "ERROR")
                return False
            except PeerFloodError:
                self.status = "error"
                await self.add_log("error", "❌ Peer Flood (Spam detection). Stopping account.", "ERROR")
                return False

        except FloodWaitError as e:
            await self.add_log("log", f"{idx_str}⏳ Rate Limited: Must wait {e.seconds}s", "WARNING")
            self.flood_wait_until = asyncio.get_event_loop().time() + e.seconds
            return False
        except (AuthKeyUnregisteredError, SessionRevokedError, PhoneNumberBannedError):
            from app.api.accounts.utils import handle_account_death
            self.status = "error"
            await self.add_log("error", "❌ Account died during task.", "ERROR")
            await handle_account_death(self.account_id, "DEAD_DURING_JOINER")
            return False
        except ConnectionError as e:
            self.status = "error"
            await self.add_log("error", f"❌ Connection Error: {str(e)}. Stopping account.", "ERROR")
            return False
        except RPCError as e:
            await self.add_log("log", f"{idx_str}❌ Telegram API Error: {str(e)}", "ERROR")
            await asyncio.sleep(5)
            return False
        except Exception as e:
            err_msg = str(e).lower()
            if any(x in err_msg for x in ["proxy", "connection", "timeout", "refused", "reach", "socket", "socks"]):
                self.status = "error"
                await self.add_log("error", f"❌ Connection/Proxy Error: {str(e)}. Stopping account.", "ERROR")
            else:
                await self.add_log("log", f"{idx_str}❌ Unexpected Error: {str(e)}", "ERROR")
                await asyncio.sleep(5)
            return False




    async def add_log(self, event: str, message: str, level: str = "INFO", data: dict = None):
        async with self.lock:
            ts = datetime.now().strftime("%I:%M:%S %p")
            log_entry = {
                "msg": message,
                "level": level,
                "time": ts,
                "account_id": self.account_id,
                "batch_id": self.batch_id,
                "phone": self.phone_number,
                "status": self.status,
                **(data or {})
            }

            self.logs.append(log_entry)
            if len(self.logs) > 50: self.logs.pop(0)
            
            for q in list(self.queues):
                try:
                    await q.put({"event": event, "data": json.dumps(log_entry)})
                    if event == "error":
                        # Send as log event so frontend logs can display it too
                        await q.put({"event": "log", "data": json.dumps(log_entry)})
                except:
                    pass
            
            # Also forward to manager if this task is part of a rotation
            if self.user_id in GROUP_JOIN_MANAGERS:
                mgr = GROUP_JOIN_MANAGERS[self.user_id]
                # We only forward if this task's batch matches the manager's batch
                if mgr.batch_id == self.batch_id:
                    await mgr.broadcast({"event": event, "data": json.dumps(log_entry)})
                    if event == "error":
                        await mgr.broadcast({"event": "log", "data": json.dumps(log_entry)})
                
            if event in ["status", "done", "error"] or event == "progress":
                await self.sync_to_db()

            if event in ["status", "done", "error"]:
                await terminal_manager.log_event(self.user_id, message, self.account_id, "group_join", level)

    async def sync_to_db(self):
        try:
            if not self.job_id:
                job = GroupJoinJob(
                    user_id=self.user_id,
                    batch_id=self.batch_id,
                    account_id=self.account_id,
                    phone_number=self.phone_number,
                    links=self.links,
                    interval=self.interval,
                    task_type=self.task_type,
                    status=self.status,
                    total_count=self.total_count,
                    done_count=self.done_count,
                    logs=self.logs[-30:]
                )
                await job.insert()
                self.job_id = str(job.id)
            else:
                job = await GroupJoinJob.get(ObjectId(self.job_id))
                if job:
                    job.status = self.status
                    job.done_count = self.done_count
                    job.logs = self.logs[-30:]
                    job.updated_at = datetime.now(timezone.utc)
                    await job.save()
        except Exception as e:
            logger.error(f"Error syncing group join to DB: {e}")

    async def run(self):
        # Legacy run method for single account tasks (if still used)
        try:
            from app.models.user import User
            user = await User.get(self.user_id)
            if not user or not user.services_active:
                self.status = "error"
                await self.add_log("error", "🛑 Task Aborted: User services are currently STOPPED.", "ERROR")
                return

            await self.add_log("status", f"🚀 Starting process for {self.total_count} groups...")
            for i, link in enumerate(self.links[self.done_count:]):
                if self.stop_requested: break
                await self.join_one(link, i + 1, len(self.links))
                if self.done_count < self.total_count:
                    await asyncio.sleep(self.interval)
            
            if self.stop_requested:
                self.status = "stopped"
                await self.add_log("done", "🛑 Stopped.", "WARNING")
            else:
                self.status = "completed"
                await self.add_log("done", "🏁 Finished.", "SUCCESS")
        finally:
            self.is_done = True
            await self.sync_to_db()

class GroupJoinRotationManager:
    def __init__(self, user_id: str, accounts_info: List[dict], links: List[str], interval: int, batch_id: str, task_type: str = "join", join_mode: str = "rotation"):
        self.user_id = user_id
        self.links = links
        self.interval = interval
        self.batch_id = batch_id
        self.task_type = task_type
        self.join_mode = join_mode # rotation, mass, instant
        self.stop_requested = False
        self.batch_queues = []
        self.lock = asyncio.Lock()
        
        self.account_tasks: Dict[str, ActiveGroupJoiner] = {}

        acc_ids = [acc['id'] for acc in accounts_info]
        if self.join_mode == "mass":
            for acc in accounts_info:
                acc_id = acc['id']
                task = ActiveGroupJoiner(user_id, acc_id, acc['phone'], links, interval, batch_id, task_type)
                self.account_tasks[acc_id] = task
        elif self.join_mode == "instant":
            # Instant mode: each account gets ALL links (same as mass) but runs fully parallel
            for acc in accounts_info:
                acc_id = acc['id']
                task = ActiveGroupJoiner(user_id, acc_id, acc['phone'], links, 1, batch_id, task_type)
                self.account_tasks[acc_id] = task
        else:
            assigned_links = {acc_id: [] for acc_id in acc_ids}
            for i, link in enumerate(links):
                acc_id = acc_ids[i % len(acc_ids)]
                assigned_links[acc_id].append(link)
            for acc in accounts_info:
                acc_id = acc['id']
                task = ActiveGroupJoiner(user_id, acc_id, acc['phone'], assigned_links[acc_id], interval, batch_id, task_type)
                self.account_tasks[acc_id] = task

        for acc_id, task in self.account_tasks.items():
            # Register in global dict so streaming still works
            if user_id not in GROUP_JOIN_TASKS:
                GROUP_JOIN_TASKS[user_id] = {}
            GROUP_JOIN_TASKS[user_id][acc_id] = task

    async def broadcast(self, msg: dict):
        async with self.lock:
            for q in list(self.batch_queues):
                try: await q.put(msg)
                except: pass

    async def run(self):
        try:
            from app.models.user import User
            user = await User.get(self.user_id)
            if not user or not user.services_active:
                await self.broadcast({"event": "log", "data": json.dumps({
                    "msg": "🛑 Task Aborted: User services are currently STOPPED. Please turn on Terminal.",
                    "level": "ERROR",
                    "time": datetime.now().strftime("%I:%M:%S %p"),
                    "account_id": "SYSTEM"
                })})
                for task in self.account_tasks.values():
                    task.status = "error"
                    await task.add_log("error", "🛑 Task Aborted: User services are currently STOPPED.", "ERROR")
                    task.is_done = True
                    await task.sync_to_db()
                return

            acc_ids = list(self.account_tasks.keys())
            for task in self.account_tasks.values():
                mode_str = "Mass mode started. All" if self.join_mode == "mass" else ("⚡ Instant mode! All" if self.join_mode == "instant" else "Rotation started. Assigned")
                await task.add_log("status", f"🚀 {mode_str} {task.total_count} groups.", data={"total": task.total_count})

            if self.join_mode == "instant":
                # Instant mode: all accounts run their joins SIMULTANEOUSLY (parallel coroutines)
                await self.broadcast({"event": "log", "data": json.dumps({
                    "msg": f"⚡ INSTANT MODE: Launching {len(acc_ids)} accounts in parallel — maximum speed!",
                    "level": "SUCCESS",
                    "time": datetime.now().strftime("%I:%M:%S %p"),
                    "account_id": "SYSTEM"
                })})

                async def instant_worker(acc_id: str, stagger_delay: float):
                    """Run one account's full join list with tiny delays and optional stagger."""
                    task = self.account_tasks[acc_id]
                    if stagger_delay > 0:
                        await asyncio.sleep(stagger_delay)
                    for l_idx, link in enumerate(task.links):
                        if self.stop_requested or task.status != "running":
                            break
                        await self.broadcast({"event": "active_account", "data": json.dumps({
                            "id": acc_id, "phone": task.phone_number, "link": link,
                            "idx": l_idx + 1, "total": task.total_count
                        })})
                        await task.join_one(link, l_idx + 1, task.total_count)
                        if l_idx < len(task.links) - 1 and task.status == "running":
                            # Tiny 1-3s jitter between joins per account (safety)
                            await asyncio.sleep(random.uniform(1, 3))

                # Stagger account starts by 1.5s to avoid simultaneous login storm
                stagger_tasks = [
                    instant_worker(acc_id, i * 1.5)
                    for i, acc_id in enumerate(acc_ids)
                ]
                await asyncio.gather(*stagger_tasks)

            elif self.join_mode == "mass":
                # Mass mode: Every active account attempts to join every group
                execution_plan = []
                for l_idx, link in enumerate(self.links):
                    for acc_id in acc_ids:
                        execution_plan.append((link, acc_id, l_idx + 1, len(self.links)))

                for i, (link, acc_id, l_idx, l_total) in enumerate(execution_plan):
                    if self.stop_requested:
                        await self.broadcast({"event": "log", "data": json.dumps({
                            "msg": "🛑 Mission Aborted by User.",
                            "level": "WARNING",
                            "time": datetime.now().strftime("%I:%M:%S %p"),
                            "account_id": "SYSTEM"
                        })})
                        break

                    task = self.account_tasks[acc_id]
                    if task.status not in ["running"]: continue

                    await self.broadcast({"event": "active_account", "data": json.dumps({"id": acc_id, "phone": task.phone_number, "link": link, "idx": l_idx, "total": l_total})})

                    now = asyncio.get_event_loop().time()
                    if task.flood_wait_until > now:
                        wait_remaining = int(task.flood_wait_until - now)
                        await task.add_log("log", f"⏳ Account in FloodWait ({wait_remaining}s). Skipping this join.", "WARNING")
                        continue

                    await task.join_one(link, l_idx, l_total)

                    if i < len(execution_plan) - 1:
                        jitter = random.randint(-2, 2) if self.interval > 5 else 0
                        wait_sec = max(2, self.interval + jitter)
                        await self.broadcast({"event": "log", "data": json.dumps({
                            "msg": f"⏳ Global cooldown: Waiting {wait_sec}s before next action...",
                            "level": "INFO",
                            "time": datetime.now().strftime("%I:%M:%S %p"),
                            "account_id": "SYSTEM"
                        })})
                        await asyncio.sleep(wait_sec)
            else:
                # Rotation mode: One account joins each group. 
                # If an account has an error, we fall back to the next working account.
                for l_idx, link in enumerate(self.links):
                    if self.stop_requested:
                        await self.broadcast({"event": "log", "data": json.dumps({
                            "msg": "🛑 Mission Aborted by User.",
                            "level": "WARNING",
                            "time": datetime.now().strftime("%I:%M:%S %p"),
                            "account_id": "SYSTEM"
                        })})
                        break

                    l_number = l_idx + 1
                    l_total = len(self.links)

                    # Dynamic Round-Robin account selection
                    start_acc_idx = l_idx % len(acc_ids)
                    success = False

                    for attempt in range(len(acc_ids)):
                        curr_acc_idx = (start_acc_idx + attempt) % len(acc_ids)
                        curr_acc_id = acc_ids[curr_acc_idx]
                        task = self.account_tasks[curr_acc_id]

                        if task.status not in ["running"]:
                            continue

                        now = asyncio.get_event_loop().time()
                        if task.flood_wait_until > now:
                            wait_remaining = int(task.flood_wait_until - now)
                            await task.add_log("log", f"⏳ Account {task.phone_number} in FloodWait ({wait_remaining}s). Skipping to next phone...", "WARNING")
                            continue

                        await self.broadcast({"event": "active_account", "data": json.dumps({"id": curr_acc_id, "phone": task.phone_number, "link": link, "idx": l_number, "total": l_total})})

                        success = await task.join_one(link, l_number, l_total)
                        if success:
                            break
                        else:
                            await task.add_log("log", f"⚠️ Account {task.phone_number} failed to join {link}. Moving to next active phone...", "WARNING")

                    if l_idx < len(self.links) - 1:
                        jitter = random.randint(-2, 2) if self.interval > 5 else 0
                        wait_sec = max(2, self.interval + jitter)
                        await self.broadcast({"event": "log", "data": json.dumps({
                            "msg": f"⏳ Global cooldown: Waiting {wait_sec}s before next link...",
                            "level": "INFO",
                            "time": datetime.now().strftime("%I:%M:%S %p"),
                            "account_id": "SYSTEM"
                        })})
                        await asyncio.sleep(wait_sec)

            # Cleanup
            for task in self.account_tasks.values():
                if task.status == "running":
                    task.status = "completed"
                    await task.add_log("done", "🏁 Mission finished.", "SUCCESS")
                task.is_done = True
                await task.sync_to_db()

        except Exception as e:
            logger.error(f"Error in GroupJoinRotationManager: {e}")

        finally:
            if self.user_id in GROUP_JOIN_MANAGERS:
                del GROUP_JOIN_MANAGERS[self.user_id]
            # Clean up task refs from GROUP_JOIN_TASKS if they are done
            for acc_id in self.account_tasks:
                if self.user_id in GROUP_JOIN_TASKS and acc_id in GROUP_JOIN_TASKS[self.user_id]:
                    del GROUP_JOIN_TASKS[self.user_id][acc_id]

