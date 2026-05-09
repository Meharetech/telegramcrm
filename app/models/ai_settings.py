from beanie import Document
from typing import Optional

class AiSettings(Document):
    key: str = "global"
    openrouter_api_key: Optional[str] = None
    default_model: str = "openai/gpt-4o-mini"

    class Settings:
        name = "ai_settings"
        indexes = [
            "key",
        ]
