from datetime import datetime, timezone
from typing import List, Optional
from beanie import Document
from pydantic import Field, BaseModel

class AiAgentConfigTrigger(BaseModel):
    condition: str = "all"  # "all", "new", "keywords"
    keywords: str = ""

class AiAgentConfigReply(BaseModel):
    delay: str = "natural"  # "instant", "natural", "slow"
    escalate: str = ""

class AiAgentConfig(BaseModel):
    trigger: AiAgentConfigTrigger = Field(default_factory=AiAgentConfigTrigger)
    reply: AiAgentConfigReply = Field(default_factory=AiAgentConfigReply)

class AiAgent(Document):
    user_id: str
    account_id: str
    agent_name: str = "AI Auto-Reply Agent"
    is_active: bool = True
    knowledge_summary_ids: List[str] = Field(default_factory=list)
    reply_count: int = 0
    last_replied_at: Optional[datetime] = None
    auto_reply_enabled: bool = False
    auto_reply_started_at: Optional[datetime] = None
    config: AiAgentConfig = Field(default_factory=AiAgentConfig)
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "ai_agents"
        indexes = [
            "account_id",
            [("user_id", 1), ("account_id", 1)],
            [("account_id", 1), ("is_active", 1)],
        ]
