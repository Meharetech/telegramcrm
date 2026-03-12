import logging
from fastapi import APIRouter, HTTPException, Depends
from typing import List
from app.models.forwarder import ForwarderRule
from app.models.user import User
from app.api.auth_utils import get_current_user
from app.models import TelegramAccount
from app.services.forwarder.logic import start_forwarder_for_account, stop_forwarder_for_rule
from app.api.forwarder_schemas import ForwarderRulePayload
from bson import ObjectId

router = APIRouter()
logger = logging.getLogger(__name__)

def _rule_to_dict(rule: ForwarderRule):
    try:
        d = rule.model_dump()
        d["id"] = str(rule.id)
        return d
    except Exception as e:
        logger.error(f"Error converting rule to dict: {e}")
        # Manual fallback
        return {
            "id": str(rule.id),
            "name": rule.name,
            "is_enabled": rule.is_enabled,
            "source_id": rule.source_id,
            "target_ids": rule.target_ids,
            "forward_mode": rule.forward_mode,
            "min_delay": rule.min_delay,
            "max_delay": rule.max_delay,
            "keyword_filters": rule.keyword_filters,
            "word_replacements": rule.word_replacements
        }

@router.get("/rules/{account_id}")
async def get_rules(account_id: str, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status_code=400, detail="Invalid account ID")
    rules = await ForwarderRule.find(
        ForwarderRule.account_id == account_id,
        ForwarderRule.user_id == str(current_user.id)
    ).to_list()
    return [_rule_to_dict(r) for r in rules]

@router.post("/rules/{account_id}")
async def create_rule(account_id: str, payload: ForwarderRulePayload, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status_code=400, detail="Invalid account ID")
    # Verify account ownership
    acc = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not acc:
        raise HTTPException(status_code=403, detail="Unauthorized account access")

    from app.api.auth_utils import check_plan_limit
    rule_count = await ForwarderRule.find(ForwarderRule.user_id == str(current_user.id)).count()
    await check_plan_limit(current_user, "max_forwarder_channels", rule_count)

    try:
        rule = ForwarderRule(
            account_id=account_id, 
            user_id=str(current_user.id),
            **payload.model_dump()
        )
        await rule.insert()
        try:
            await start_forwarder_for_account(account_id)
        except Exception as e:
            logger.error(f"Error starting forwarder for account {account_id}: {e}")
            # We still return the rule because it was inserted
        return _rule_to_dict(rule)
    except Exception as e:
        logger.error(f"CRITICAL: Failed to create rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/rules/{account_id}/{rule_id}")
async def update_rule(account_id: str, rule_id: str, payload: ForwarderRulePayload, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(account_id) or not ObjectId.is_valid(rule_id):
        raise HTTPException(status_code=400, detail="Invalid account or rule ID")
    rule = await ForwarderRule.find_one(
        ForwarderRule.id == ObjectId(rule_id),
        ForwarderRule.user_id == str(current_user.id)
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    # Update fields
    data = payload.model_dump()
    for key, value in data.items():
        setattr(rule, key, value)
    
    await rule.save()
    await start_forwarder_for_account(account_id)
    return _rule_to_dict(rule)

@router.delete("/rules/{account_id}/{rule_id}")
async def delete_rule(account_id: str, rule_id: str, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(account_id) or not ObjectId.is_valid(rule_id):
        raise HTTPException(status_code=400, detail="Invalid account or rule ID")
    rule = await ForwarderRule.find_one(
        ForwarderRule.id == ObjectId(rule_id),
        ForwarderRule.user_id == str(current_user.id)
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    await rule.delete()
    
    # Detach handler if active
    await stop_forwarder_for_rule(account_id, rule_id)

    return {"status": "success"}

@router.post("/activate/{account_id}")
async def activate_forwarder(account_id: str, current_user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status_code=400, detail="Invalid account ID")
    # Verify account ownership
    acc = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not acc:
        raise HTTPException(status_code=403, detail="Unauthorized account access")
    await start_forwarder_for_account(account_id)
    return {"status": "success", "message": "Forwarder activated"}
