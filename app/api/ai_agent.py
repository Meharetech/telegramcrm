from fastapi import APIRouter, Depends, HTTPException, Body
from typing import List, Optional
from app.api.auth_utils import get_current_user
from app.models.ai_agent import AiAgent, AiAgentConfig
from app.models.ai_knowledge import AiKnowledgeSummary
from app.models.ai_reply_log import AiReplyLog
from app.models.ai_settings import AiSettings
from app.services.ai_agent_service import call_openrouter
from app.config import settings
from pydantic import BaseModel
from datetime import datetime, timezone
from app.api.auto_reply.worker import _activate_worker

router = APIRouter()

class SummarizeRequest(BaseModel):
    text: str
    source_type: str = "text"

class TestChatRequest(BaseModel):
    agent_id: str
    message: str

@router.get("/list", response_model=List[AiAgent])
async def list_agents(user=Depends(get_current_user)):
    return await AiAgent.find(AiAgent.user_id == str(user.id)).to_list()

@router.get("/account/{account_id}")
async def get_agent_by_account(account_id: str, user=Depends(get_current_user)):
    agent = await AiAgent.find_one(AiAgent.account_id == account_id, AiAgent.user_id == str(user.id))
    if not agent:
        return None
    return {
        **agent.model_dump(mode="json"),
        "id": str(agent.id)
    }

@router.post("/upsert")
async def upsert_agent(agent_data: dict = Body(...), user=Depends(get_current_user)):
    account_id = agent_data.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")
    
    agent = await AiAgent.find_one(AiAgent.account_id == account_id, AiAgent.user_id == str(user.id))
    
    # ── Check Account Limit if activating ──
    is_activating = agent_data.get("is_active", False)
    if is_activating:
        from app.api.auth_utils import check_plan_limit
        # Count currently active agents (excluding this one if it's already active)
        active_count = await AiAgent.find(
            AiAgent.user_id == str(user.id), 
            AiAgent.is_active == True,
            AiAgent.account_id != account_id
        ).count()
        await check_plan_limit(user, "max_ai_agents", active_count)

    if agent:
        # Update existing
        for key, value in agent_data.items():
            if key not in ["_id", "id", "user_id", "account_id"]:
                setattr(agent, key, value)
        agent.updated_at = datetime.now(timezone.utc)
        await agent.save()
    else:
        # Create new
        agent_data["user_id"] = str(user.id)
        # Strip any stale id fields to avoid conflicts
        agent_data.pop("_id", None)
        agent_data.pop("id", None)
        agent = AiAgent(**agent_data)
        await agent.create()
    
    # Always return a plain dict with a proper string 'id'
    res = {
        **agent.model_dump(mode="json"),
        "id": str(agent.id)
    }

    # If active, ensure the worker is attached
    if agent.is_active:
        try:
            await _activate_worker(account_id)
        except Exception:
            pass

    return res

@router.post("/knowledge/summarize")
async def summarize_knowledge(req: SummarizeRequest, user=Depends(get_current_user)):
    settings_db = await AiSettings.find_one(AiSettings.key == "global")
    api_key = (settings_db.openrouter_api_key if settings_db else None) or settings.OPENROUTER_API_KEY
    if not api_key:
        raise HTTPException(status_code=400, detail="OpenRouter API key not configured")

    # Use OpenRouter to summarize the text
    summary_prompt = (
        "Summarize the following business information into a concise knowledge base for a customer support AI. "
        "Include key details like services, pricing, contact info, and policies. "
        "Keep it under 8000 characters.\n\n"
        f"Text to summarize:\n{req.text}"
    )

    try:
        summary = await call_openrouter(summary_prompt, "", settings_db)
        
        ks = AiKnowledgeSummary(
            user_id=str(user.id),
            source_type=req.source_type,
            summary=summary,
            model=settings_db.default_model if settings_db else "openai/gpt-4o-mini"
        )
        await ks.create()
        return ks
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/knowledge/list")
async def list_knowledge(user=Depends(get_current_user)):
    items = await AiKnowledgeSummary.find(AiKnowledgeSummary.user_id == str(user.id)).to_list()
    return [
        {**item.model_dump(mode="json"), "id": str(item.id)}
        for item in items
    ]

@router.delete("/knowledge/{summary_id}")
async def delete_knowledge(summary_id: str, user=Depends(get_current_user)):
    from bson import ObjectId
    ks = await AiKnowledgeSummary.get(ObjectId(summary_id))
    if not ks or ks.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Summary not found")
    
    await ks.delete()
    return {"status": "success"}

@router.post("/test-chat")
async def test_chat(req: TestChatRequest, user=Depends(get_current_user)):
    # Try to find by agent_id (string ObjectId) first, then fall back to account_id match
    agent = None
    try:
        from bson import ObjectId
        agent = await AiAgent.get(ObjectId(req.agent_id))
    except Exception:
        pass
    
    if not agent:
        # Fallback: find by account_id in case agent_id is actually the account_id
        agent = await AiAgent.find_one(AiAgent.account_id == req.agent_id, AiAgent.user_id == str(user.id))
    
    if not agent or agent.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Agent not found. Please click SYNC AGENT first to save your configuration.")

    settings_db = await AiSettings.find_one(AiSettings.key == "global")
    api_key = (settings_db.openrouter_api_key if settings_db else None) or settings.OPENROUTER_API_KEY
    if not api_key:
        raise HTTPException(status_code=400, detail="OpenRouter API key not configured in backend .env")

    summaries = []
    ks_ids = getattr(agent, "knowledge_summary_ids", [])
    if not ks_ids and getattr(agent, "knowledge_summary_id", None):
        ks_ids = [agent.knowledge_summary_id]
        
    if ks_ids:
        from bson import ObjectId as ObjId
        for kid in ks_ids:
            try:
                ks = await AiKnowledgeSummary.get(ObjId(kid))
                if ks:
                    summaries.append(ks.summary)
            except Exception:
                pass
    summary_text = "\n---\n".join(summaries)

    from app.api.auth_utils import check_plan_limit
    all_agents = await AiAgent.find(AiAgent.user_id == str(user.id)).to_list()
    total_replies = sum(getattr(a, "reply_count", 0) for a in all_agents)
    await check_plan_limit(user, "ai_chatbot_limit", total_replies)

    try:
        reply = await call_openrouter(req.message, summary_text, settings_db)
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/logs/{account_id}", response_model=List[AiReplyLog])
async def get_logs(account_id: str, user=Depends(get_current_user)):
    return await AiReplyLog.find(
        AiReplyLog.account_id == account_id, 
        AiReplyLog.user_id == str(user.id)
    ).sort("-created_at").limit(50).to_list()

@router.get("/settings")
async def get_ai_settings(user=Depends(get_current_user)):
    # Only admins should see the actual API key?
    # For now, let's just return if it's configured
    settings_db = await AiSettings.find_one(AiSettings.key == "global")
    configured = (settings_db and settings_db.openrouter_api_key) or settings.OPENROUTER_API_KEY
    return {
        "configured": bool(configured),
        "model": settings_db.default_model if settings_db else "openai/gpt-4o-mini"
    }

# Admin route to update settings
@router.post("/settings")
async def update_ai_settings(data: dict = Body(...), user=Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    settings = await AiSettings.find_one(AiSettings.key == "global")
    if not settings:
        settings = AiSettings(key="global")
    
    if "openrouter_api_key" in data:
        settings.openrouter_api_key = data["openrouter_api_key"]
    if "default_model" in data:
        settings.default_model = data["default_model"]
    
    await settings.save()
    return {"status": "success"}

@router.get("/stats")
async def get_ai_stats(user=Depends(get_current_user)):
    all_agents = await AiAgent.find(AiAgent.user_id == str(user.id)).to_list()
    total_used = sum(getattr(a, "reply_count", 0) for a in all_agents)
    active_agents = [a for a in all_agents if a.is_active]
    
    return {
        "total_used": total_used,
        "active_count": len(active_agents),
        "active_account_ids": [getattr(a, "account_id", "") for a in active_agents]
    }
