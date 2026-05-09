from datetime import datetime, timezone
from typing import Optional
from beanie import Document
from pydantic import Field

class AiReplyLog(Document):
    agent_id: str
    user_id: str
    account_id: str
    sender_id: str
    inbound_text: str
    reply_text: str
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "ai_reply_logs"
        indexes = [
            "agent_id",
            "user_id",
            "account_id",
            "created_at",
        ]
