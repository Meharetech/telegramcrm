import asyncio
import random
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from telethon import functions, types
from telethon.errors import (
    FloodWaitError, PeerFloodError, RPCError,
    PhoneNumberBannedError, AuthKeyUnregisteredError,
    SessionRevokedError, UserDeactivatedBanError, SessionExpiredError
)
from app.models import TelegramAccount
from app.client_cache import get_client
from app.services.terminal_service import terminal_manager
from bson import ObjectId

logger = logging.getLogger(__name__)

# Global storage for active folder campaigns: { user_id: { account_id: ActiveFolderCampaign } }
FOLDER_CAMPAIGN_TASKS: Dict[str, Dict[str, 'ActiveFolderCampaign']] = {}

from app.models.folder_campaign import FolderCampaignJob

class ActiveFolderCampaign:
    def __init__(self, user_id: str, account_id: str, phone_number: str, folder_id: str, folder_name: str, selected_group_ids: List[str], 
                 message_text: str, min_delay: int, max_delay: int, repeat_interval: Optional[int] = None, group_metadata: Dict[str, Dict] = None):
        self.user_id = user_id
        self.account_id = account_id
        self.phone_number = phone_number
        self.folder_id = folder_id
        self.folder_name = folder_name
        self.selected_group_ids = selected_group_ids
        self.group_metadata = group_metadata or {}
        self.message_text = message_text
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.repeat_interval = repeat_interval # in minutes
        
        self.status = "running"
        self.done_count = 0
        self.total_targets = len(selected_group_ids)
        self.is_done = False
        self.logs = []
        self.queues: List[asyncio.Queue] = []
        self.lock = asyncio.Lock()
        self.stop_requested = False
        self.job_id: Optional[str] = None

    async def add_log(self, event: str, message: str, level: str = "INFO", data: dict = None):
        async with self.lock:
            ts = datetime.now().strftime("%I:%M:%S %p")
            log_entry = {"msg": message, "level": level, "time": ts, **(data or {})}
            sse_msg = {"event": event, "data": json.dumps(log_entry)}
            self.logs.append(log_entry)
            if len(self.logs) > 100: self.logs.pop(0)
            
            # Use list copy for safe iteration while broadcasting
            active_queues = list(self.queues)
            for q in active_queues: 
                try:
                    await q.put(sse_msg)
                except: pass
            
            # ── Optimized Syncing ──
            # Only sync to DB on major events OR every 5 successful messages to reduce DB load
            if event in ["status", "done", "error"] or (event == "progress" and self.done_count % 5 == 0):
                await self.sync_to_db()

            # ── Terminal Integration ──
            if event in ["status", "done", "error"]:
                from app.services.terminal_service import terminal_manager
                await terminal_manager.log_event(
                    user_id=self.user_id,
                    message=message,
                    account_id=self.account_id,
                    module="folder_campaign",
                    level=level
                )

    async def sync_to_db(self):
        try:
            if not self.job_id:
                # ── Double Start Protection ──
                # Ensure we don't create multiple 'running' jobs for the same account
                existing = await FolderCampaignJob.find_one(
                    FolderCampaignJob.user_id == self.user_id,
                    FolderCampaignJob.account_id == self.account_id,
                    FolderCampaignJob.status == "running"
                )
                if existing:
                    self.job_id = str(existing.id)
                else:
                    job = FolderCampaignJob(
                        user_id=self.user_id,
                        account_id=self.account_id,
                        phone_number=self.phone_number,
                        folder_id=self.folder_id,
                        folder_name=self.folder_name,
                        selected_group_ids=self.selected_group_ids,
                        message_text=self.message_text,
                        min_delay=self.min_delay,
                        max_delay=self.max_delay,
                        repeat_interval=self.repeat_interval,
                        group_metadata=self.group_metadata,
                        status=self.status,
                        total_targets=self.total_targets
                    )
                    await job.insert()
                    self.job_id = str(job.id)
            
            # Perform update
            job = await FolderCampaignJob.get(ObjectId(self.job_id))
            if job:
                job.status = self.status
                job.done_count = self.done_count
                job.logs = self.logs[-50:]
                job.updated_at = datetime.now(timezone.utc)
                await job.save()
        except Exception as e:
            logger.error(f"Error syncing folder campaign to DB: {e}")

    async def run(self):
        # ── Concurrency Check ──
        # Ensure only one task runs for this account in memory
        async with self.lock:
            if self.is_done: return
        
        try:
            await self.add_log("status", f"🚀 Initializing Folder Campaign...")
            
            acc = await TelegramAccount.get(ObjectId(self.account_id))
            if not acc or not acc.is_active:
                await self.add_log("error", "❌ Source account not found or inactive.", "ERROR")
                return

            client = await get_client(str(acc.id), acc.session_string, acc.api_id, acc.api_hash)
            
            while not self.stop_requested:
                await self.add_log("log", f"📂 Starting new cycle for {len(self.selected_group_ids)} groups...")
                
                for group_id in self.selected_group_ids:
                    if self.stop_requested: break
                    
                    try:
                        await self.add_log("log", f"⏳ Resolving and sending to {group_id}...", "INFO")
                        
                        # ── Advanced Entity Resolution ──
                        # We try resolving via ID, then Username if ID fails.
                        try:
                            # Try ID first (most precise if cached)
                            target = await client.get_entity(int(group_id) if group_id.isdigit() or group_id.startswith('-') else group_id)
                        except Exception:
                            # If ID fails, try Username/Link from metadata
                            meta = self.group_metadata.get(str(group_id), {})
                            username = meta.get('username') or meta.get('link')
                            if username:
                                try:
                                    target = await client.get_entity(username)
                                except Exception as ent_err:
                                    await self.add_log("log", f"⚠️ Skipping {group_id}: Could not resolve via ID or Username. ({ent_err})", "WARNING")
                                    continue
                            else:
                                await self.add_log("log", f"⚠️ Skipping {group_id}: Could not resolve entity (No username/metadata).", "WARNING")
                                continue

                        await client.send_message(target, self.message_text)
                        
                        self.done_count += 1
                        delay = random.randint(self.min_delay, self.max_delay)
                        
                        await self.add_log("progress", f"✅ Message sent to {group_id}", "SUCCESS", data={
                            "done": self.done_count,
                            "total": self.total_targets,
                            "next_delay": delay
                        })
                        
                        # Interruptible sleep for random delay
                        for _ in range(delay):
                            if self.stop_requested: break
                            await asyncio.sleep(1)

                    except (FloodWaitError, PeerFloodError) as e:
                        wait_time = getattr(e, 'seconds', 300)
                        await self.add_log("log", f"⏳ Rate Limit: Waiting {wait_time}s", "WARNING")
                        for _ in range(wait_time):
                            if self.stop_requested: break
                            await asyncio.sleep(1)
                    except (AuthKeyUnregisteredError, PhoneNumberBannedError, SessionRevokedError, UserDeactivatedBanError, SessionExpiredError) as e:
                        from app.api.accounts.utils import handle_account_death
                        await self.add_log("error", f"❌ Account DEAD: {type(e).__name__}. Stopping campaign.", "ERROR")
                        await handle_account_death(self.account_id, reason=type(e).__name__)
                        self.stop_requested = True
                        break
                    except ConnectionError:
                        await self.add_log("log", "⚠️ Connection error (Proxy issues?). Retrying in 30s...", "WARNING")
                        await asyncio.sleep(30)
                    except Exception as e:
                        await self.add_log("log", f"❌ Error sending to {group_id}: {str(e)}", "ERROR")

                if self.repeat_interval and not self.stop_requested:
                    # ── Randomized Cycle Break ──
                    # Instead of a fixed minute, we add a random jitter (+/- 20 seconds) 
                    # to make the account behavior look human and avoid Telegram detection.
                    base_wait = self.repeat_interval * 60
                    jitter = random.randint(-20, 20)
                    wait_seconds = max(15, base_wait + jitter) 
                    
                    await self.add_log("log", f"💤 Cycle finished. Waiting {wait_seconds} seconds before next run (Randomized)...", "INFO")
                    for _ in range(wait_seconds):
                        if self.stop_requested: break
                        await asyncio.sleep(1)
                else:
                    break

            if self.stop_requested:
                # If this was a global "Stop All" from the terminal, 
                # we keep the status as "running" so it resumes later.
                # If it was a manual stop from the campaign page, it will be "stopped".
                if not hasattr(self, 'is_manual_stop'):
                    self.status = "running"
                    await self.add_log("done", "⏸️ System Paused. Campaign will resume on next Launch.", "WARNING")
                else:
                    self.status = "stopped"
                    await self.add_log("done", "🛑 Campaign Stopped by User.", "WARNING")
            else:
                self.status = "completed"
                await self.add_log("done", "🏁 Folder Campaign Finished.", "SUCCESS")
                
        except Exception as e:
            self.status = "error"
            await self.add_log("error", f"💥 Critical Error: {str(e)}", "ERROR")
        finally:
            self.is_done = True
            await self.sync_to_db() # Final sync
            # Clean up global tasks
            if self.user_id in FOLDER_CAMPAIGN_TASKS:
                if self.account_id in FOLDER_CAMPAIGN_TASKS[self.user_id]:
                    del FOLDER_CAMPAIGN_TASKS[self.user_id][self.account_id]
                if not FOLDER_CAMPAIGN_TASKS[self.user_id]:
                    del FOLDER_CAMPAIGN_TASKS[self.user_id]
