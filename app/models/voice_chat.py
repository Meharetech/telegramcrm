from datetime import datetime, timezone
from typing import Optional, List, Dict
from beanie import Document
from pydantic import Field

class VoiceChatHistory(Document):
    user_id: str
    group_link: str
    task_type: str # 'join' or 'leave'
    total_accounts: int
    success_count: int
    failed_count: int
    results: List[Dict] # [{account_id: str, phone: str, status: str, message: str}]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "voice_chat_history"
        indexes = ["user_id", "created_at"]
