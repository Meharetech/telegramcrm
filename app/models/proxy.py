from datetime import datetime
from typing import Optional
from beanie import Document
from pydantic import Field

class Proxy(Document):
    user_id: str = "legacy_user"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    protocol: str = "http" # or socks5
    
    # Track which Telegram Account this proxy is currently powering
    assigned_account_id: Optional[str] = None
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "proxies"
        indexes = [
            "user_id",
            "assigned_account_id",
        ]
