from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from app.models import Proxy, TelegramAccount, User
from app.api.auth_utils import get_current_user
from app.client_cache import invalidate
from beanie import BulkWriter
import asyncio

router = APIRouter()

class BatchProxyRequest(BaseModel):
    # Expects multi-line string: IP:PORT:USER:PASS
    raw_proxies: str
    protocol: str = "http"

@router.get("/list")
async def list_proxies(current_user: User = Depends(get_current_user)):
    proxies = await Proxy.find(Proxy.user_id == str(current_user.id)).to_list()
    # Mask password for security
    return [
        {
            "id": str(p.id),
            "host": p.host,
            "port": p.port,
            "username": p.username,
            "protocol": p.protocol,
            "assigned_account_id": p.assigned_account_id,
        }
        for p in proxies
    ]

@router.post("/batch-add")
async def batch_add_proxies(req: BatchProxyRequest, current_user: User = Depends(get_current_user)):
    user_id_str = str(current_user.id)
    
    # 1. Parse the incoming text
    lines = [L.strip() for L in req.raw_proxies.split("\n") if L.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="No valid proxies provided.")

    from app.api.auth_utils import check_plan_limit
    # Check if the number of proxies being added exceeds the limit
    # Note: we clear old ones so we only check the new count
    await check_plan_limit(current_user, "max_proxies", len(lines))

    new_proxies = []
    for line in lines:
        parts = line.split(":")
        if len(parts) >= 2:
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                continue
            
            user = parts[2] if len(parts) > 2 else None
            password = parts[3] if len(parts) > 3 else None
            
            new_proxies.append(Proxy(
                user_id=user_id_str,
                host=host,
                port=port,
                username=user,
                password=password,
                protocol=req.protocol,
                assigned_account_id=None
            ))

    if not new_proxies:
        raise HTTPException(status_code=400, detail="Could not parse any provided proxies.")

    # 2. Clear old proxies
    await Proxy.find(Proxy.user_id == user_id_str).delete()
    
    # 3. Insert new proxies
    await Proxy.insert_many(new_proxies)
    
    # 4. Auto-assign to existing accounts
    accounts = await TelegramAccount.find(TelegramAccount.user_id == user_id_str).to_list()
    # Fetch all proxies (including those just inserted)
    all_proxies = await Proxy.find(Proxy.user_id == user_id_str).to_list()
    
    affected_accounts = []
    
    # Simple assignment: Assign each account a proxy until we run out of proxies or accounts
    async with BulkWriter() as bulk:
        for idx, account in enumerate(accounts):
            if idx < len(all_proxies):
                proxy = all_proxies[idx]
                proxy.assigned_account_id = str(account.id)
                await proxy.save(bulk_writer=bulk)
                affected_accounts.append(str(account.id))
            
    # 5. Disconnect affected clients so they reconnect via the new proxy on their next action
    for acc_id in affected_accounts:
        await invalidate(acc_id)

    return {
        "status": "success",
        "message": f"Successfully imported {len(new_proxies)} proxies and assigned to {len(affected_accounts)} existing accounts."
    }

@router.delete("/{proxy_id}")
async def delete_proxy(proxy_id: str, current_user: User = Depends(get_current_user)):
    from bson import ObjectId
    proxy = await Proxy.find_one(
        Proxy.id == ObjectId(proxy_id), 
        Proxy.user_id == str(current_user.id)
    )
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
        
    acc_id = proxy.assigned_account_id
    await proxy.delete()
    
    # Disconnect client so it reconnets directly without proxy
    if acc_id:
        # Attempt to auto-assign a free proxy to the orphaned account
        free_proxy = await Proxy.find_one(
            Proxy.user_id == str(current_user.id), 
            Proxy.assigned_account_id == None
        )
        if free_proxy:
            free_proxy.assigned_account_id = acc_id
            await free_proxy.save()
            
        await invalidate(acc_id)
        
    return {"status": "success"}

@router.delete("/clear/all")
async def clear_all_proxies(current_user: User = Depends(get_current_user)):
    proxies = await Proxy.find(Proxy.user_id == str(current_user.id)).to_list()
    affected_accounts = [p.assigned_account_id for p in proxies if p.assigned_account_id]
    
    await Proxy.find(Proxy.user_id == str(current_user.id)).delete()
    
    for acc_id in affected_accounts:
        await invalidate(acc_id)
        
    return {"status": "success"}
@router.post("/auto-assign")
async def auto_assign_proxies(current_user: User = Depends(get_current_user)):
    user_id_str = str(current_user.id)
    
    # 1. Get all accounts and proxies for this user
    accounts = await TelegramAccount.find(TelegramAccount.user_id == user_id_str).to_list()
    proxies = await Proxy.find(Proxy.user_id == user_id_str).to_list()
    
    # 2. Identify which accounts are currently using a proxy
    assigned_acc_ids = {p.assigned_account_id for p in proxies if p.assigned_account_id}
    
    # 3. Filter for accounts that are "Direct Connection" (no assigned proxy)
    accounts_to_assign = [a for a in accounts if str(a.id) not in assigned_acc_ids]
    
    # 4. Filter for "Idle" proxies (no assigned account)
    idle_proxies = [p for p in proxies if not p.assigned_account_id]
    
    if not accounts_to_assign:
        return {"status": "success", "message": "All accounts already have proxies assigned."}
    if not idle_proxies:
        raise HTTPException(status_code=400, detail="No idle proxies available for assignment.")
    
    assigned_count = 0
    async with BulkWriter() as bulk:
        for idx, account in enumerate(accounts_to_assign):
            if idx < len(idle_proxies):
                proxy = idle_proxies[idx]
                proxy.assigned_account_id = str(account.id)
                await proxy.save(bulk_writer=bulk)
                assigned_count += 1
                # Invalidate cache so client reconnects with proxy
                await invalidate(str(account.id))
                
    return {
        "status": "success", 
        "message": f"Successfully assigned {assigned_count} idle proxies to 'Direct Connection' accounts."
    }
