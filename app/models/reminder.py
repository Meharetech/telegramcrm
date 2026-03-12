from datetime import datetime
from typing import Optional
from beanie import Document, Indexed
from pydantic import Field

class Reminder(Document):
    user_id: str = Indexed()
    telegram_account_id: str
    chat_id: str
    chat_name: Optional[str] = None
    message: str
    media_path: Optional[str] = None
    telegram_media: Optional[dict] = None
    telegram_message_id: Optional[int] = None
    remind_at: datetime
    status: str = "pending"        # pending, sending, triggered, completed, error
    popup_status: str = "not_closed"  # not_closed, closed
    created_at: datetime = Field(default_factory=datetime.utcnow)
    triggered_at: Optional[datetime] = None
    # FIX: Fields for retry logic in reminder worker
    retry_count: int = 0
    error_message: Optional[str] = None


    class Settings:
        name = "reminders"
        # FIX: Indexes for fast per-user reminder queries and due-reminder polling
        indexes = [
            "user_id",
            [("user_id", 1), ("status", 1)],
            [("status", 1), ("remind_at", 1)],
            [("telegram_account_id", 1), ("status", 1)],
        ]
