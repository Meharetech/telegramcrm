import asyncio
import httpx
import logging
import random
from datetime import datetime, timezone
from typing import Optional
from app.models.ai_agent import AiAgent
from app.models.ai_knowledge import AiKnowledgeSummary
from app.models.ai_settings import AiSettings
from app.models.ai_reply_log import AiReplyLog
from app.config import settings
from app.services.terminal_service import terminal_manager

logger = logging.getLogger(__name__)

# Simple in-memory cooldown: account_id:sender_id -> last replied timestamp
reply_cooldown = {}

def get_reply_delay(delay_type: str) -> float:
    if delay_type == "instant":
        return 0
    if delay_type == "slow":
        return 4.0 + random.random() * 4.0
    return 1.0 + random.random() * 2.0  # natural (default)

async def call_openrouter(message: str, summary_text: str, settings_db: AiSettings):
    model = settings_db.default_model if settings_db else "openai/gpt-4o-mini"
    api_key = (settings_db.openrouter_api_key if settings_db else None) or settings.OPENROUTER_API_KEY

    if not api_key:
        raise ValueError("OpenRouter API key not configured")

    system_prompt = (
        f"You are a customer support AI assistant representing the business described below. "
        f"You speak on behalf of this business — when someone asks 'your name', 'who are you', or similar, "
        f"answer using the name and details from the business info below.\n\n"
        f"Business info:\n{summary_text}\n\n"
        f"Rules:\n"
        f"- Answer questions using only the business info above.\n"
        f"- Speak as a representative of this business.\n"
        f"- If a question cannot be answered from the info above, say: 'Please contact support.'\n"
        f"- Be concise and friendly."
    ) if summary_text else "You are a helpful AI customer support agent. Be concise and friendly."

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://telegramcrmai.com", # Required by some models
                    "X-Title": "Telegram CRM AI",
                },
                json={
                    "model": model,
                    "temperature": 0.5,
                    "max_tokens": 250,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message},
                    ],
                },
                timeout=30.0
            )

            if resp.status_code != 200:
                try:
                    data = resp.json()
                    error_msg = data.get("error", {}).get("message", f"OpenRouter Error {resp.status_code}")
                except:
                    error_msg = f"OpenRouter returned status {resp.status_code}"
                raise Exception(error_msg)

            data = resp.json()
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not reply:
                raise Exception("OpenRouter returned empty reply")
            return reply
        except httpx.TimeoutException:
            raise Exception("Request to OpenRouter timed out. Please try again.")
        except Exception as e:
            raise Exception(f"AI Error: {str(e)}")

async def handle_ai_message(account_id: str, event, client):
    """
    Handle incoming Telegram message for AI Agent.
    """
    msg_text = (event.raw_text or "").strip()
    if not msg_text:
        return False

    # ONLY reply to private messages (user DMs), not groups/channels
    if not event.is_private:
        return False

    sender_id = str(event.sender_id)
    cooldown_key = f"{account_id}:{sender_id}"
    last_reply = reply_cooldown.get(cooldown_key, 0)
    
    # 3 second cooldown — skip, but return False so engine doesn't stop other flows
    if (datetime.now().timestamp() - last_reply) < 3:
        return False

    try:
        # 1. Find active agent
        agent = await AiAgent.find_one(AiAgent.account_id == account_id, AiAgent.is_active == True)
        if not agent:
            return False

        # 2. Check trigger condition
        condition = agent.config.trigger.condition
        if condition == "keywords":
            keywords = [k.strip().lower() for k in agent.config.trigger.keywords.split(",") if k.strip()]
            if keywords:
                lower_text = msg_text.lower()
                if not any(k in lower_text for k in keywords):
                    return False
        elif condition == "new":
            # Logic for 'new' trigger (first message) could be added here
            # For now, let's treat it as all or handle it similarly to welcome messages
            pass

        # 3. Check escalation keyword
        escalate_word = (agent.config.reply.escalate or "").strip().lower()
        if escalate_word and escalate_word in msg_text.lower():
            await terminal_manager.log_event(agent.user_id, f"🚨 AI Hand-off: Escalation keyword '{escalate_word}' matched.", account_id, "ai-agent", "INFO")
            return False

        # 4. Get OpenRouter settings
        settings_db = await AiSettings.find_one(AiSettings.key == "global")
        api_key = (settings_db.openrouter_api_key if settings_db else None) or settings.OPENROUTER_API_KEY
        if not api_key:
            await terminal_manager.log_event(agent.user_id, "❌ AI Error: OpenRouter API key not configured.", account_id, "ai-agent", "ERROR")
            return

        # 4.5 — QUOTA GATE: Hard-stop before any AI work if limit is exceeded
        from app.api.auth_utils import check_plan_limit
        from bson import ObjectId
        from app.models.user import User
        user_obj = await User.get(ObjectId(agent.user_id))
        
        # If we can't load the user, we BLOCK as a safety measure
        if not user_obj:
            logger.warning(f"[AI Agent] Could not load user {agent.user_id} — blocking reply for safety.")
            return False

        try:
            # 1. Check if user has access to AI Agent at all
            await check_plan_limit(user_obj, "access_ai_agent")
            
            # 2. Sum up all replies from all agents of this user to check against total limit
            all_agents = await AiAgent.find(AiAgent.user_id == agent.user_id).to_list()
            total_replies = sum(getattr(a, "reply_count", 0) for a in all_agents)
            
            # Fetch the limit manually for better logging
            limit_val = -1
            from app.models.plan import Plan
            if user_obj.plan_id:
                p = await Plan.get(ObjectId(user_obj.plan_id))
                if p: 
                    limit_val = getattr(p, "ai_chatbot_limit", -1)
            else:
                from app.models.system_settings import SystemSettings
                sys_s = await SystemSettings.find_one()
                if sys_s:
                    limit_val = getattr(sys_s, "demo_ai_chatbot_limit", 200)
                else:
                    limit_val = 200

            limit_str = "∞" if limit_val == -1 else str(limit_val)
            await terminal_manager.log_event(agent.user_id, f"📊 Quota Check: {total_replies}/{limit_str} replies used.", account_id, "ai-agent", "DEBUG")
            
            await check_plan_limit(user_obj, "ai_chatbot_limit", total_replies)

        except Exception as limit_err:
            detail = getattr(limit_err, "detail", str(limit_err))
            await terminal_manager.log_event(agent.user_id, f"🚨 Quota Exhausted: {detail}. AI Agent auto-deactivated.", account_id, "ai-agent", "ERROR")
            # Auto-deactivate the agent — no more messages will be processed
            agent.is_active = False
            await agent.save()
            return False

        # 5. Get knowledge summaries
        summaries = []
        # Support both new list and old single field for resilience
        ks_ids = getattr(agent, "knowledge_summary_ids", [])
        if not ks_ids and getattr(agent, "knowledge_summary_id", None):
            ks_ids = [agent.knowledge_summary_id]
            
        if ks_ids:
            from bson import ObjectId
            for kid in ks_ids:
                try:
                    ks = await AiKnowledgeSummary.get(ObjectId(kid))
                    if ks:
                        summaries.append(ks.summary)
                except Exception as e:
                    logger.error(f"[AI Agent] Knowledge fetch error for {kid}: {e}")
        
        summary_text = "\n---\n".join(summaries)

        # 6. Apply reply delay
        wait_sec = get_reply_delay(agent.config.reply.delay)
        if wait_sec > 0:
            await asyncio.sleep(wait_sec)

        # 7. Call OpenRouter
        await terminal_manager.log_event(agent.user_id, f"🤖 AI Processing: '{msg_text[:40]}...'", account_id, "ai-agent", "DEBUG")

        # Get the already-resolved chat entity from the event (avoids entity cache errors)
        try:
            input_chat = await event.get_input_chat()
        except Exception:
            input_chat = None

        # Call OpenRouter (show typing if we can, otherwise skip)
        if input_chat:
            try:
                async with client.action(input_chat, 'typing'):
                    reply = await call_openrouter(msg_text, summary_text, settings_db)
            except Exception:
                # Typing failed — still get the reply
                reply = await call_openrouter(msg_text, summary_text, settings_db)
        else:
            reply = await call_openrouter(msg_text, summary_text, settings_db)

        # 8. Send reply
        reply_cooldown[cooldown_key] = datetime.now().timestamp()
        await event.reply(reply)
        await terminal_manager.log_event(agent.user_id, f"📤 AI Reply Sent: '{reply[:40]}...'", account_id, "ai-agent", "SUCCESS")

        # 9. Persist stats + log
        agent.reply_count += 1
        agent.last_replied_at = datetime.now(timezone.utc)
        await agent.save()

        log = AiReplyLog(
            agent_id=str(agent.id),
            user_id=agent.user_id,
            account_id=account_id,
            sender_id=sender_id,
            inbound_text=msg_text,
            reply_text=reply
        )
        await log.create()
        return True

    except Exception as e:
        logger.error(f"[AI Agent] Error on account {account_id}: {e}", exc_info=True)
        # Also try to log to terminal so user can see the error in the dashboard
        try:
            agent_obj = await AiAgent.find_one(AiAgent.account_id == account_id)
            if agent_obj:
                await terminal_manager.log_event(
                    agent_obj.user_id,
                    f"❌ AI Agent Error: {str(e)}",
                    account_id, "ai-agent", "ERROR"
                )
        except Exception:
            pass
        return False
