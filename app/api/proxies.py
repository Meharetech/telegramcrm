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
    inserted_proxies = await Proxy.find(Proxy.user_id == user_id_str).to_list()
    
    affected_accounts = []
    
    # Use BulkWriter for O(1) high-speed MongoDB commit
    async with BulkWriter() as bulk:
        for idx, account in enumerate(accounts):
            if idx < len(inserted_proxies):
                proxy = inserted_proxies[idx]
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
