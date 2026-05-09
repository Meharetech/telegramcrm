from datetime import datetime, timezone
from typing import Optional
from beanie import Document
from pydantic import Field

class AiKnowledgeSummary(Document):
    user_id: str
    source_type: str = "text"  # "text", "file"
    summary: str = Field(..., max_length=4000)
    model: str = "openai/gpt-4o-mini"
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "ai_knowledge_summaries"
        indexes = [
            "user_id",
        ]
