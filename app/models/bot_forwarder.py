from datetime import datetime
from typing import List, Optional
from beanie import Document
from pydantic import Field

class BotForwarder(Document):
    """
    Configuration for a Telegram Bot API based forwarder.
    """
    user_id: str                      # Owner of the bot configuration
    name: str                         # Friendly name
    bot_token: str                    # Telegram Bot API Token
    admin_usernames: List[str] = []   # List of authorized @usernames (without leading @ is fine)
    target_chat_ids: List[str] = []   # List of Telegram Peer IDs/Usernames for targets
    is_enabled: bool = True           # Toggle for the bot listener
    
    proxy_id: Optional[str] = None    # Optional proxy to use for this bot
    
    # Optional logic settings (similar to User Forwarder)
    forward_mode: str = "forward"     # "forward" or "copy"
    keyword_filters: List[str] = []   # Only forward if text contains one of these
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_active: Optional[datetime] = None
    flood_wait_until: Optional[datetime] = None

    class Settings:
        name = "bot_forwarders"
        indexes = [
            "user_id",
            "bot_token",
            [("user_id", 1), ("is_enabled", 1)],
        ]
