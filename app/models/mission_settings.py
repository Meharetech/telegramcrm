from typing import Optional
from beanie import Document
from pydantic import Field

class MemberAddSettings(Document):
    user_id: str
    consecutive_privacy_threshold: int = 10
    max_flood_sleep_threshold: int = 300
    account_limit_cap: int = 40
    cooldown_24h: int = 86400

    class Settings:
        name = "member_add_settings"
