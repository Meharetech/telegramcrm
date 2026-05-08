import asyncio
import random
import logging
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from telethon import functions, types
from telethon.errors import FloodWaitError, RPCError, PhoneNumberBannedError, AuthKeyUnregisteredError, SessionRevokedError
from app.models import TelegramAccount, GroupJoinJob
from app.client_cache import get_client
from app.services.terminal_service import terminal_manager
from bson import ObjectId

logger = logging.getLogger(__name__)

GROUP_JOIN_TASKS: Dict[str, Dict[str, 'ActiveGroupJoiner']] = {}

class ActiveGroupJoiner:
    def __init__(self, user_id: str, account_id: str, phone_number: str, links: List[str], interval: int):
        self.user_id = user_id
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

    async def add_log(self, event: str, message: str, level: str = "INFO", data: dict = None):
        async with self.lock:
            ts = datetime.now().strftime("%I:%M:%S %p")
            log_entry = {"msg": message, "level": level, "time": ts, **(data or {})}
            sse_msg = {"event": event, "data": json.dumps(log_entry)}
            self.logs.append(log_entry)
            if len(self.logs) > 50: self.logs.pop(0)
            
            for q in list(self.queues):
                try: await q.put(sse_msg)
                except: pass
                
            if event in ["status", "done", "error"] or event == "progress":
                await self.sync_to_db()

            if event in ["status", "done", "error"]:
                await terminal_manager.log_event(self.user_id, message, self.account_id, "group_join", level)

    async def sync_to_db(self):
        try:
            if not self.job_id:
                job = GroupJoinJob(
                    user_id=self.user_id,
                    account_id=self.account_id,
                    phone_number=self.phone_number,
                    links=self.links,
                    interval=self.interval,
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
        try:
            await self.add_log("status", f"🚀 Starting Group Joiner for {self.total_count} links...")
            
            acc = await TelegramAccount.get(ObjectId(self.account_id))
            if not acc or not acc.is_active:
                await self.add_log("error", "❌ Account not found or inactive.", "ERROR")
                return

            client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash)
            
            for link in self.links[self.done_count:]:
                if self.stop_requested: break
                
                clean_link = link.strip().replace("https://t.me/", "").replace("t.me/", "").replace("@", "")
                if not clean_link: continue

                try:
                    await self.add_log("log", f"⏳ Attempting to join: {clean_link}...", "INFO")
                    
                    # Ensure client is connected
                    if not client.is_connected():
                        await client.connect()
                    
                    # Join logic with timeout
                    from telethon.tl.functions.messages import ImportChatInviteRequest
                    from telethon.tl.functions.channels import JoinChannelRequest
                    
                    try:
                        if "joinchat/" in link or "+" in link:
                            hash_code = link.split("/")[-1].replace("+", "").replace("https://t.me/", "").replace("t.me/", "")
                            await asyncio.wait_for(client(ImportChatInviteRequest(hash_code)), timeout=30)
                        else:
                            await asyncio.wait_for(client(JoinChannelRequest(clean_link)), timeout=30)
                        
                        self.done_count += 1
                        await self.add_log("progress", f"✅ Joined: {clean_link}", "SUCCESS", data={"done": self.done_count, "total": self.total_count})
                    except asyncio.TimeoutError:
                        await self.add_log("log", f"⚠️ Timeout joining {clean_link}. Skipping...", "WARNING")
                    
                    if self.done_count < self.total_count:
                        # Randomize interval slightly (+/- 15s)
                        wait_sec = (self.interval * 60) + random.randint(-15, 15)
                        wait_sec = max(20, wait_sec)
                        await self.add_log("log", f"💤 Delay: {wait_sec}s until next link...", "INFO")
                        for _ in range(wait_sec):
                            if self.stop_requested: break
                            await asyncio.sleep(1)

                except FloodWaitError as e:
                    await self.add_log("log", f"⏳ Rate Limited: Must wait {e.seconds}s", "WARNING")
                    for _ in range(e.seconds):
                        if self.stop_requested: break
                        await asyncio.sleep(1)
                except (AuthKeyUnregisteredError, SessionRevokedError, PhoneNumberBannedError):
                    from app.api.accounts.utils import handle_account_death
                    await self.add_log("error", "❌ Account died during joiner task.", "ERROR")
                    await handle_account_death(self.account_id, "DEAD_DURING_JOINER")
                    break
                except Exception as e:
                    await self.add_log("log", f"❌ Error joining {clean_link}: {str(e)}", "ERROR")
                    # Still wait even on error to be safe
                    await asyncio.sleep(10)

            if self.stop_requested:
                self.status = "stopped"
                await self.add_log("done", "🛑 Group Joiner Stopped.", "WARNING")
            else:
                self.status = "completed"
                await self.add_log("done", "🏁 Group Joiner Finished all links.", "SUCCESS")
                
        except Exception as e:
            self.status = "error"
            await self.add_log("error", f"💥 Critical Error: {str(e)}", "ERROR")
        finally:
            self.is_done = True
            await self.sync_to_db()
            if self.user_id in GROUP_JOIN_TASKS:
                if self.account_id in GROUP_JOIN_TASKS[self.user_id]:
                    del GROUP_JOIN_TASKS[self.user_id][self.account_id]
