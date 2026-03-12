from datetime import datetime
from beanie import Document
from pydantic import Field

class TelegramAPI(Document):
    user_id: str
    api_id: int
    api_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "telegram_api_credentials" # Separate collection
