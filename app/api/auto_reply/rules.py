from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from app.models.auto_reply import AutoReplyRule
from app.models import TelegramAccount, User
from app.api.auth_utils import get_current_user
from bson import ObjectId
from .schemas import RulePayload
from app.services.auto_reply.cache import invalidate_rules_cache

router = APIRouter()

def _rule_dict(rule: AutoReplyRule) -> dict:
    d = rule.model_dump(exclude={"revision_id"})
    d["id"] = str(rule.id)
    return d

@router.get("/rules/{account_id}")
async def list_rules(account_id: str, current_user: User = Depends(get_current_user)):
    rules = await AutoReplyRule.find(
        AutoReplyRule.account_id == account_id,
        AutoReplyRule.user_id == str(current_user.id)
    ).sort("-created_at").to_list()
    return [_rule_dict(r) for r in rules]

@router.post("/rules/{account_id}")
async def create_rule(account_id: str, payload: RulePayload, current_user: User = Depends(get_current_user)):
    # Verify account ownership
    acc = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not acc:
        raise HTTPException(status_code=403, detail="Unauthorized account access")

    from app.api.auth_utils import check_plan_limit
    rule_count = await AutoReplyRule.find(AutoReplyRule.user_id == str(current_user.id)).count()
    await check_plan_limit(current_user, "max_auto_replies", rule_count)

    rule = AutoReplyRule(
        account_id=account_id, 
        user_id=str(current_user.id),
        **payload.model_dump()
    )
    await rule.insert()
    invalidate_rules_cache(account_id)  # ← force engine to reload fresh rules
    return _rule_dict(rule)

@router.put("/rules/{account_id}/{rule_id}")
async def update_rule(account_id: str, rule_id: str, payload: RulePayload, current_user: User = Depends(get_current_user)):
    rule = await AutoReplyRule.find_one(
        AutoReplyRule.id == ObjectId(rule_id),
        AutoReplyRule.user_id == str(current_user.id)
    )
    if not rule or rule.account_id != account_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    for k, v in payload.model_dump().items():
        setattr(rule, k, v)
    rule.updated_at = datetime.utcnow()
    await rule.save()
    invalidate_rules_cache(account_id)  # ← force engine to reload fresh rules
    return _rule_dict(rule)

@router.delete("/rules/{account_id}/{rule_id}")
async def delete_rule(account_id: str, rule_id: str, current_user: User = Depends(get_current_user)):
    rule = await AutoReplyRule.find_one(
        AutoReplyRule.id == ObjectId(rule_id),
        AutoReplyRule.user_id == str(current_user.id)
    )
    if not rule or rule.account_id != account_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    await rule.delete()
    invalidate_rules_cache(account_id)  # ← force engine to reload fresh rules
    return {"status": "deleted"}
