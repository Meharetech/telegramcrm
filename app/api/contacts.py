import asyncio
import logging
import csv
import io
import re
import uuid
import json
from datetime import datetime
from typing import List, Optional, Dict
from sse_starlette.sse import EventSourceResponse

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Query
from pydantic import BaseModel
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import (
    DeleteContactsRequest,
    GetContactsRequest,
    ImportContactsRequest,
)
from telethon.tl.types import InputPhoneContact

from app.api.auth_utils import get_current_user
from app.client_cache import get_client
from app.models import TelegramAccount, User

router = APIRouter()
logger = logging.getLogger(__name__)

BATCH_SIZE = 20       # per ImportContactsRequest call
BATCH_DELAY = 8       # seconds between batches (anti-flood)
DELETE_BATCH = 50
DELETE_DELAY = 4

# Global store for persistent distribution tasks
class ActiveDistribution:
    def __init__(self, task_id, user_id, body: "DistributeRequest"):
        self.task_id = task_id
        self.user_id = user_id
        self.body = body
        self.logs = []
        self.status = "running"
        self.total_added = 0
        self.done = False
        self.created_at = datetime.now()
        self.queues: List[asyncio.Queue] = []
        self.lock = asyncio.Lock()

    async def add_log(self, event, data):
        async with self.lock:
            # Ensure data is JSON string for SSE
            json_data = json.dumps(data) if isinstance(data, dict) else str(data)
            msg = {"event": event, "data": json_data}
            self.logs.append(msg)
            for q in self.queues:
                await q.put(msg)

    async def run(self):
        try:
            contact_pool = list(self.body.contacts)
            await self.add_log("status", {"message": "⚡ Distribution engine initialized..."})

            for config in self.body.account_configs:
                try:
                    acc = await TelegramAccount.get(config.id)
                    if not acc or str(acc.user_id) != self.user_id:
                        await self.add_log("warning", {"message": f"Account {config.id} not found or unauthorized"})
                        continue

                    acc_added = 0
                    target_for_this_acc = config.count
                    batch_idx = 0

                    await self.add_log("account_start", {"phone": acc.phone_number, "id": str(acc.id)})
                    
                    client = await get_client(
                        str(acc.id), acc.session_string, acc.api_id, acc.api_hash,
                        device_model=acc.device_model
                    )

                    while acc_added < target_for_this_acc and contact_pool:
                        batch = []
                        while len(batch) < 20 and contact_pool:
                            batch.append(contact_pool.pop(0))
                        
                        if not batch: break
                        
                        batch_idx += 1
                        tg_batch = []
                        for c_idx, c in enumerate(batch):
                            phone = _normalise_phone(c.get("phone", ""))
                            if not phone: continue
                            tg_batch.append(InputPhoneContact(
                                client_id=c_idx, phone=phone,
                                first_name=c.get("first_name") or "User",
                                last_name=c.get("last_name") or "",
                            ))
                        
                        if tg_batch:
                            try:
                                res = await client(ImportContactsRequest(tg_batch))
                                found_now = len(res.users)
                                acc_added += found_now
                                self.total_added += found_now
                                
                                acc.contact_count += len(res.imported)
                                acc.contacts_added_today += found_now
                                await acc.save()
                                
                                await self.add_log("batch_done", {
                                    "phone": acc.phone_number,
                                    "batch": batch_idx,
                                    "added": found_now,
                                    "total_so_far": acc_added,
                                    "target": target_for_this_acc
                                })
                                await asyncio.sleep(BATCH_DELAY if found_now > 0 else 2)
                            except FloodWaitError as fe:
                                await self.add_log("warning", {"phone": acc.phone_number, "message": f"FloodWait: Sleeping {fe.seconds}s"})
                                await asyncio.sleep(fe.seconds + 2)
                            except Exception as be:
                                await self.add_log("warning", {"phone": acc.phone_number, "message": str(be)})

                    await self.add_log("account_done", {"phone": acc.phone_number, "added": acc_added})

                except Exception as ae:
                    await self.add_log("warning", {"message": f"Error on account {config.id}: {str(ae)}"})

            await self.add_log("done", {"total_added": self.total_added})
            
        except Exception as e:
            await self.add_log("error", {"message": str(e)})
        finally:
            self.done = True
            # We keep it in the dict for a while so user can see 'Mission Complete' report
            await asyncio.sleep(300) # cleanup after 5 mins
            if self.task_id in DISTRIBUTION_TASKS:
                del DISTRIBUTION_TASKS[self.task_id]

DISTRIBUTION_TASKS: Dict[str, ActiveDistribution] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class DeleteRequest(BaseModel):
    user_ids: List[int]

class AddContactRequest(BaseModel):
    contacts: List[dict]   # [{first_name, last_name, phone}]

class AccountConfig(BaseModel):
    id: str
    count: int

class DistributeRequest(BaseModel):
    account_configs: List[AccountConfig]
    contacts: List[dict]
    per_account: int = 50

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_client_for_account(account_id: str, current_user: User):
    acc = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id),
    )
    if not acc:
        raise HTTPException(status_code=403, detail="Unauthorized account access")
    return await get_client(
        account_id,
        acc.session_string,
        acc.api_id,
        acc.api_hash,
        device_model=getattr(acc, "device_model", "Telegram Android"),
    )


def _normalise_phone(raw: str) -> str:
    """Strip everything except digits and leading +."""
    phone = re.sub(r"[^\d+]", "", raw.strip())
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    return phone


# ─────────────────────────────────────────────────────────────────────────────
# File parsers  (VCF / CSV / TXT)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_vcf(text: str) -> List[dict]:
    contacts, cur = [], {}
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        up = line.upper()
        if up == "BEGIN:VCARD":
            cur = {"first_name": "", "last_name": "", "phone": ""}
        elif up == "END:VCARD":
            if cur.get("phone"):
                if not cur.get("first_name"): cur["first_name"] = "Contact"
                contacts.append(cur)
            cur = {}
        elif ":" in line:
            key_val = line.split(":", 1)
            key, val = key_val[0].upper(), key_val[1].strip()
            if key.startswith("FN"):
                parts = val.split(" ", 1)
                cur["first_name"] = parts[0]
                cur["last_name"]  = parts[1] if len(parts) > 1 else ""
            elif key.startswith("N"):
                parts = val.split(";")
                if not cur.get("last_name"): cur["last_name"]  = parts[0].strip() if len(parts) > 0 else ""
                if not cur.get("first_name"): cur["first_name"] = parts[1].strip() if len(parts) > 1 else ""
            elif "TEL" in key:
                phone = _normalise_phone(val)
                if phone and not cur.get("phone"):
                    cur["phone"] = phone
    return contacts


def _parse_csv(text: str) -> List[dict]:
    """Try common column names: name/first_name/last_name/phone/number."""
    contacts = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        keys = {k.lower().strip(): v for k, v in row.items() if v}
        phone_raw = (
            keys.get("phone") or keys.get("number") or
            keys.get("mobile") or keys.get("tel") or ""
        )
        phone = _normalise_phone(phone_raw)
        if not phone:
            continue
        full = keys.get("name") or keys.get("full_name") or ""
        parts = full.split(" ", 1) if full else ["", ""]
        contacts.append({
            "first_name": keys.get("first_name") or parts[0],
            "last_name":  keys.get("last_name")  or (parts[1] if len(parts) > 1 else ""),
            "phone":      phone,
        })
    return contacts


def _parse_txt(text: str) -> List[dict]:
    """Plain text: one phone per line (optionally: name,phone)."""
    contacts = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "," in line:
            parts = line.split(",", 2)
            phone = _normalise_phone(parts[-1])
            name  = parts[0].strip().split(" ", 1)
            contacts.append({
                "first_name": name[0],
                "last_name":  name[1] if len(name) > 1 else "",
                "phone":      phone,
            })
        else:
            phone = _normalise_phone(line)
            if phone:
                contacts.append({"first_name": "Contact", "last_name": "", "phone": phone})
    return contacts


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# Telegram availability check
# ─────────────────────────────────────────────────────────────────────────────

async def _check_availability(client, contacts: List[dict]) -> List[dict]:
    """
    Import contacts temporarily to see which have Telegram accounts.
    Returns list of contacts enriched with telegram_id / username if found.
    """
    available = []
    for i in range(0, len(contacts), 50):
        batch = contacts[i:i + 50]
        tg_batch = [
            InputPhoneContact(
                client_id=i + idx,
                phone=c["phone"],
                first_name=c.get("first_name") or "User",
                last_name=c.get("last_name") or "",
            )
            for idx, c in enumerate(batch)
        ]
        try:
            res = await client(ImportContactsRequest(tg_batch))
            # Build lookup: client_id → user
            user_map = {}
            for u in res.users:
                user_map[u.phone] = u

            for idx, c in enumerate(batch):
                phone_clean = c["phone"].lstrip("+")
                # Telegram strips leading + in returned phone
                match = None
                for uphone, u in user_map.items():
                    if uphone and phone_clean.endswith(uphone) or uphone.endswith(phone_clean):
                        match = u
                        break
                if match:
                    available.append({
                        **c,
                        "telegram_id": match.id,
                        "username":    match.username or "",
                        "tg_name":     f"{match.first_name or ''} {match.last_name or ''}".strip(),
                    })

            # Clean up: delete the temp imports
            if res.users:
                try:
                    input_users = [await client.get_input_entity(u.id) for u in res.users]
                    await client(DeleteContactsRequest(id=input_users))
                except Exception:
                    pass

            if i + 50 < len(contacts):
                await asyncio.sleep(4)

        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 2)
        except Exception as ex:
            logger.warning(f"[contacts] availability check error: {ex}")

    return available


# ─────────────────────────────────────────────────────────────────────────────
# Distribution Stream Logic (Must be above /{account_id} to avoid collision)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/prepare-distribute")
async def prepare_distribute(
    body: DistributeRequest,
    current_user: User = Depends(get_current_user),
):
    """Start background distribution task."""
    # Check if user already has an active task
    for t in DISTRIBUTION_TASKS.values():
        if t.user_id == str(current_user.id) and not t.done:
             raise HTTPException(status_code=400, detail="You already have a distribution mission active.")

    task_id = str(uuid.uuid4())
    task = ActiveDistribution(task_id, str(current_user.id), body)
    DISTRIBUTION_TASKS[task_id] = task
    
    # Start in background
    asyncio.create_task(task.run())
    
    return {"task_id": task_id}

@router.get("/active-task")
async def get_active_task(current_user: User = Depends(get_current_user)):
    """Check if user has an active distribution task."""
    for t in DISTRIBUTION_TASKS.values():
        if t.user_id == str(current_user.id) and not t.done:
            return {
                "task_id": t.task_id,
                "status": t.status
            }
    return {"task_id": None}

@router.get("/stream-distribute/{task_id}")
async def stream_distribute(
    task_id: str,
    token: str,
):
    from app.api.auth_utils import get_user_from_token
    user = await get_user_from_token(token)
    if not user:
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "Invalid token"})}])
    
    task = DISTRIBUTION_TASKS.get(task_id)
    if not task or task.user_id != str(user.id):
        return EventSourceResponse([{"event": "error", "data": json.dumps({"message": "Task not found"})}])

    async def event_generator():
        # 1. Send historical logs
        for log in task.logs:
            yield log
        
        if task.done:
            return

        # 2. Subscribe to new logs
        q = asyncio.Queue()
        async with task.lock:
            task.queues.append(q)
        
        try:
            while True:
                msg = await q.get()
                yield msg
                if msg["event"] in ["done", "error"]:
                    break
        finally:
            async with task.lock:
                if q in task.queues:
                    task.queues.remove(q)

    return EventSourceResponse(event_generator())


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/overview")
async def get_contacts_overview(
    current_user: User = Depends(get_current_user),
    skip: Optional[int] = Query(None, ge=0),
    limit: Optional[int] = Query(None, le=1000),
    search: Optional[str] = None,
    status: Optional[str] = None
):
    """Paginated and searchable account overview with legacy array support."""
    user_id_str = str(current_user.id)
    query = TelegramAccount.find(
        TelegramAccount.user_id == user_id_str,
        TelegramAccount.is_active == True
    )

    if search:
        query = query.find({"phone_number": {"$regex": search, "$options": "i"}})
    
    if status:
        query = query.find(TelegramAccount.status == status)

    is_paginated = skip is not None or limit is not None or search is not None
    
    if is_paginated:
        total = await query.count()
        # USE PROJECTION: Fetch only needed metadata for overview
        accounts = await query.skip(skip or 0).limit(limit or 500).project(TelegramAccount.AccountShort).to_list()
    else:
        accounts = await query.project(TelegramAccount.AccountShort).to_list()
    
    result_list = [
        {
            "id": str(acc.id),
            "phone": acc.phone_number,
            "contact_count": acc.contact_count,
            "daily_limit": acc.daily_contacts_limit,
            "added_today": acc.contacts_added_today,
            "status": acc.status,
            "last_sync": acc.last_sync_date,
            "flood_wait_until": acc.flood_wait_until,
            "is_active": acc.is_active
        }
        for acc in accounts
    ]

    if is_paginated:
        return {"total": total, "accounts": result_list}
    return result_list

@router.post("/refresh-overview")
async def refresh_contacts_overview(current_user: User = Depends(get_current_user)):
    """Heavy: Sync all accounts with Telegram to update contact counts in DB."""
    accounts = await TelegramAccount.find(
        TelegramAccount.user_id == str(current_user.id),
        TelegramAccount.is_active == True
    ).to_list()
    
    results = []
    for acc in accounts:
        try:
            client = await get_client(
                str(acc.id), acc.session_string, acc.api_id, acc.api_hash,
                device_model=acc.device_model
            )
            res = await client(GetContactsRequest(hash=0))
            
            # Update DB
            acc.contact_count = len(res.users)
            acc.last_sync_date = datetime.utcnow()
            await acc.save()
            
            results.append({
                "id": str(acc.id),
                "phone": acc.phone_number,
                "contact_count": acc.contact_count,
                "status": acc.status
            })
            # Small throttle to avoid flooding Telegram
            await asyncio.sleep(1.0)
        except Exception as e:
            results.append({
                "id": str(acc.id),
                "phone": acc.phone_number,
                "error": str(e),
                "status": "error"
            })
            
    return {"status": "success", "synced": results}

@router.get("/{account_id}")
async def get_contacts(account_id: str, current_user: User = Depends(get_current_user)):
    try:
        client = await _get_client_for_account(account_id, current_user)
        result = await client(GetContactsRequest(hash=0))
        return {
            "total": len(result.users),
            "contacts": [
                {
                    "id":         u.id,
                    "access_hash": u.access_hash,
                    "first_name": u.first_name or "",
                    "last_name":  u.last_name  or "",
                    "username":   u.username   or "",
                    "phone":      u.phone      or "",
                    "is_bot":     u.bot,
                }
                for u in result.users
            ],
        }
    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"Flood limit — wait {e.seconds}s")
    except Exception as e:
        logger.error(f"[contacts] fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Parse + availability check ────────────────────────────────────────────────
@router.post("/parse-file")
async def parse_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Upload VCF / CSV / TXT file.
    Returns parsed contacts + those found on Telegram.
    """
    raw = await file.read()
    text = _decode(raw)
    fname = (file.filename or "").lower()

    if fname.endswith((".vcf", ".vcard")):
        contacts = _parse_vcf(text)
    elif fname.endswith(".csv"):
        contacts = _parse_csv(text)
    else:
        contacts = _parse_txt(text)

    if not contacts:
        raise HTTPException(status_code=400, detail="No valid phone numbers found in file")

    return {
        "parsed_total":    len(contacts),
        "available_total": len(contacts),
        "available":       contacts,    # Now includes everyone
        "all_parsed":      contacts,
    }


# ── Add contacts manually ─────────────────────────────────────────────────────
@router.post("/{account_id}/add")
async def add_contacts(
    account_id: str,
    body: AddContactRequest,
    current_user: User = Depends(get_current_user),
):
    if not body.contacts:
        raise HTTPException(status_code=400, detail="No contacts provided")
    client = await _get_client_for_account(account_id, current_user)

    # Fetch account to update stats
    acc = await TelegramAccount.find_one(TelegramAccount.id == ObjectId(account_id))

    added, errors = 0, []
    total_processed = 0 # Match behavior with distribution
    for i in range(0, len(body.contacts), BATCH_SIZE):
        batch = body.contacts[i:i + BATCH_SIZE]
        tg_batch = []
        for idx, c in enumerate(batch):
            phone = _normalise_phone(c.get("phone", ""))
            if not phone:
                errors.append({"index": i + idx, "error": "missing phone"})
                continue
            tg_batch.append(InputPhoneContact(
                client_id=i + idx,
                phone=phone,
                first_name=c.get("first_name") or "Unknown",
                last_name=c.get("last_name") or "",
            ))
        if tg_batch:
            try:
                res = await client(ImportContactsRequest(tg_batch))
                added_now = len(res.imported)
                found_now = len(res.users)
                added += added_now
                total_processed += found_now
                
                if acc:
                    acc.contact_count += added_now
                    acc.contacts_added_today += found_now
                    await acc.save()

            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 2)
            except Exception as ex:
                errors.append({"batch": i, "error": str(ex)})
        if i + BATCH_SIZE < len(body.contacts):
            await asyncio.sleep(BATCH_DELAY)

    return {"status": "done", "added": total_processed, "errors": errors}


@router.post("/{account_id}/update-limit")
async def update_account_limit(
    account_id: str,
    body: Dict[str, int],
    current_user: User = Depends(get_current_user),
):
    acc = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id),
    )
    if not acc:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    limit = body.get("limit")
    if limit is None or limit < 0:
        raise HTTPException(status_code=400, detail="Invalid limit")
    
    acc.daily_contacts_limit = limit
    await acc.save()
    return {"status": "success", "new_limit": limit}

# ── Delete contacts ───────────────────────────────────────────────────────────
@router.post("/{account_id}/delete")
async def delete_contacts(
    account_id: str,
    body: DeleteRequest,
    current_user: User = Depends(get_current_user),
):
    if not body.user_ids:
        raise HTTPException(status_code=400, detail="No user IDs provided")
    client = await _get_client_for_account(account_id, current_user)
    total_deleted, errors = 0, []

    for i in range(0, len(body.user_ids), DELETE_BATCH):
        batch = body.user_ids[i:i + DELETE_BATCH]
        input_users = []
        for uid in batch:
            try:
                input_users.append(await client.get_input_entity(uid))
            except Exception as ex:
                errors.append({"id": uid, "error": str(ex)})
        if input_users:
            try:
                await client(DeleteContactsRequest(id=input_users))
                total_deleted += len(input_users)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 2)
            except Exception as ex:
                errors.append({"batch": i, "error": str(ex)})
        if i + DELETE_BATCH < len(body.user_ids):
            await asyncio.sleep(DELETE_DELAY)

    return {"status": "done", "deleted": total_deleted, "errors": errors}

@router.post("/{account_id}/clear-all")
async def clear_all_contacts(
    account_id: str,
    current_user: User = Depends(get_current_user)
):
    """Recursively delete ALL contacts from a single account using bulk MTProto calls."""
    client = await _get_client_for_account(account_id, current_user)
    
    # 1. Fetch full synced contact list
    res = await client(GetContactsRequest(hash=0))
    if not res.users:
        return {"status": "done", "deleted": 0}
        
    user_ids = [u.id for u in res.users]
    total_to_delete = len(user_ids)
    
    # 2. Bulk Delete - Telegram allows large batches, we'll use 500 for maximum safety & speed
    for i in range(0, len(user_ids), 500):
        batch = user_ids[i:i + 500]
        try:
            # Note: client.get_input_entity can be slow for 500 items, 
            # but DeleteContactsRequest accepts a list of IDs directly in modern Telethon 
            # if they are already in the internal cache from GetContactsRequest.
            await client(DeleteContactsRequest(id=batch))
        except Exception as e:
            logger.warning(f"Batch delete failed: {e}")
            # Fallback to single entities if cache is cold
            try:
                input_users = [await client.get_input_entity(uid) for uid in batch]
                await client(DeleteContactsRequest(id=input_users))
            except: pass
            
        if i + 500 < len(user_ids):
            await asyncio.sleep(2.0) # Respectful delay between huge batches
            
    # 3. Update DB cache immediately
    acc = await TelegramAccount.get(account_id)
    if acc:
        acc.contact_count = 0
        await acc.save()
        
    return {"status": "done", "deleted": total_to_delete}
