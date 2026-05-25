import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Any
from bson import ObjectId

from telethon import TelegramClient, functions, types
from app.client_cache import get_client, is_user_active
from app.models.report_job import ReportJob
from app.models import TelegramAccount
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

# Global registry to hold in-memory report tasks: { user_id: ActiveReportCampaign }
REPORT_TASKS: Dict[str, "ActiveReportCampaign"] = {}

def get_telethon_reason(reason_str: str):
    """
    Map user selection strings to Telethon InputReportReason classes.
    """
    r = reason_str.lower().strip()
    if "spam" in r:
        return types.InputReportReasonSpam()
    elif "violence" in r:
        return types.InputReportReasonViolence()
    elif "porn" in r or "porno" in r:
        return types.InputReportReasonPornography()
    elif "abuse" in r or "child" in r:
        return types.InputReportReasonChildAbuse()
    elif "drug" in r or "drugs" in r:
        return types.InputReportReasonIllegalDrugs()
    elif "detail" in r or "personal" in r:
        return types.InputReportReasonPersonalDetails()
    elif "fake" in r:
        return types.InputReportReasonFake()
    elif "copyright" in r:
        return types.InputReportReasonCopyright()
    elif "geo" in r:
        return types.InputReportReasonGeoIrrelevant()
    else:
        return types.InputReportReasonSpam() # Default fallback

class ActiveReportCampaign:
    def __init__(
        self,
        user_id: str,
        target: str,
        reason: str,
        account_configs: List[Dict],
        messages: List[str],
        min_delay: int,
        max_delay: int,
        batch_size: int = 1
    ):
        self.user_id = user_id
        self.target = target.strip()
        self.reason = reason
        self.account_configs = account_configs
        self.messages = [m.strip() for m in messages if m.strip()]
        self.min_delay = max(5, min_delay)
        self.max_delay = max(self.min_delay, max_delay)
        self.batch_size = max(1, min(10, batch_size))
        
        self.job_id: str = ""
        self.status = "running"
        self.done_count = 0
        self.errors_count = 0
        self.total_count = len(account_configs)
        
        self.logs: List[Dict] = []
        self.stop_requested = False
        self.is_done = False
        self._is_syncing = False
        self._queue = asyncio.Queue()

    async def add_log(self, type_: str, msg: str, level: str = "INFO"):
        time_str = datetime.now().strftime("%H:%M:%S")
        log_entry = {
            "id": str(uuid.uuid4()),
            "time": time_str,
            "type": type_,
            "msg": msg,
            "level": level
        }
        self.logs.append(log_entry)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]
        
        # Put into the queue for SSE stream
        await self._queue.put(log_entry)
        # Trigger async sync to DB
        asyncio.create_task(self.sync_state())

    async def get_log_event(self):
        return await self._queue.get()

    async def sync_state(self):
        if self._is_syncing or not self.job_id:
            return
        self._is_syncing = True
        try:
            job = await ReportJob.get(self.job_id)
            if job:
                job.status = self.status
                job.done_count = self.done_count
                job.errors_count = self.errors_count
                job.logs = self.logs[-100:]
                job.updated_at = datetime.now(timezone.utc)
                await job.save()
        except Exception as e:
            logger.error(f"[report_service] DB Sync failed: {e}")
        finally:
            self._is_syncing = False

    async def report_single_account(self, cfg: Dict, index: int, reason_obj) -> None:
        acc_id = cfg.get("id")
        phone = cfg.get("phone", "Unknown")

        if not acc_id:
            return

        await self.add_log("log", f"⏳ [{phone}] Connecting client...", "INFO")

        try:
            acc = await TelegramAccount.get(acc_id)
            if not acc or not acc.is_active:
                await self.add_log("log", f"⚠️ Account {phone} is inactive. Skipping.", "WARNING")
                self.errors_count += 1
                return

            # Retrieve client
            client = await get_client(
                str(acc.id),
                acc.session_string,
                acc.api_id,
                acc.api_hash,
                device_model=acc.device_model
            )

            if not client or not await client.is_user_authorized():
                await self.add_log("log", f"❌ Account {phone} unauthorized. Skipping.", "ERROR")
                self.errors_count += 1
                return

            # Resolve target entity
            target_entity = await client.get_entity(self.target)

            # Select message from rotation or list
            msg_text = ""
            if self.messages:
                # Rotate sequentially based on index
                msg_text = self.messages[index % len(self.messages)]

            # Send Report Request
            await client(functions.account.ReportPeerRequest(
                peer=target_entity,
                reason=reason_obj,
                message=msg_text
            ))

            self.done_count += 1
            msg_info = f" with message: \"{msg_text}\"" if msg_text else ""
            await self.add_log("progress", f"✅ [{phone}] Successfully reported target{msg_info}.", "SUCCESS")

        except Exception as e:
            self.errors_count += 1
            err_msg = str(e)
            await self.add_log("log", f"❌ [{phone}] Failed to report: {err_msg}", "ERROR")
            logger.error(f"[report_service] Error using {phone}: {e}")

    async def run(self):
        try:
            # 1. Verify User Services are active
            if not await is_user_active(self.user_id):
                await self.add_log("error", "🛑 Task Aborted: User services are currently STOPPED.", "ERROR")
                self.status = "stopped"
                return

            await self.add_log("status", f"🚀 Initializing report campaign for target: {self.target} with {self.total_count} accounts (Batch Size: {self.batch_size}).")
            await terminal_manager.log_event(
                self.user_id,
                f"🚀 Starting Report Campaign targeting '{self.target}' with {self.total_count} accounts (Batch Size: {self.batch_size}).",
                module="report_service",
                level="INFO"
            )

            # Resolve Input Reason
            reason_obj = get_telethon_reason(self.reason)

            # 2. Process in batches
            batch_size = self.batch_size
            for i in range(0, len(self.account_configs), batch_size):
                if self.stop_requested:
                    await self.add_log("status", "🛑 Campaign stopped by user request.", "WARNING")
                    break

                batch = self.account_configs[i : i + batch_size]
                
                # Run batch concurrently
                tasks = []
                for offset, cfg in enumerate(batch):
                    global_index = i + offset
                    tasks.append(self.report_single_account(cfg, global_index, reason_obj))
                
                await asyncio.gather(*tasks)

                # Stagger delay between batches
                is_last_batch = (i + batch_size) >= len(self.account_configs)
                if not is_last_batch and not self.stop_requested:
                    delay = random.randint(self.min_delay, self.max_delay)
                    await self.add_log("log", f"💤 Sleeping for {delay} seconds before next batch...", "INFO")
                    
                    # Sleep in small chunks to detect stop request quickly
                    for _ in range(delay):
                        if self.stop_requested:
                            break
                        await asyncio.sleep(1)

            # Finish up
            if self.stop_requested:
                self.status = "stopped"
            else:
                self.status = "completed"
                await self.add_log("done", "🎉 Campaign completed successfully!", "SUCCESS")

            await terminal_manager.log_event(
                self.user_id,
                f"📊 Report campaign finished. Successful: {self.done_count}, Errors: {self.errors_count}",
                module="report_service",
                level="INFO"
            )

        except Exception as e:
            self.status = "stopped"
            await self.add_log("error", f"🚨 Fatal campaign error: {str(e)}", "ERROR")
            logger.exception("[report_service] Fatal Campaign error")
        finally:
            self.is_done = True
            await self.sync_state()
            # Clean up task registry
            REPORT_TASKS.pop(self.user_id, None)
