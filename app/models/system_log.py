from beanie import Document, Indexed
from datetime import datetime, timezone
from typing import Optional
from pydantic import Field
from pymongo import IndexModel

class SystemLog(Document):
    user_id: Indexed(str)
    account_id: Optional[str] = None
    module: str # "auto-reply", "forwarder", "proxy", "scraper", "reaction"
    level: str # "INFO", "SUCCESS", "ERROR", "WARNING"
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "system_logs"
        indexes = [
            [("user_id", 1), ("timestamp", -1)],
            # Automatically delete logs older than 48 hours to prevent DB bloat
            IndexModel([("timestamp", 1)], expireAfterSeconds=172800),
        ]
