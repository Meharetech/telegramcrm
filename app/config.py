from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # Server
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    DEBUG: bool = True
    ALLOWED_ORIGINS: str = "https://telegramcrmai.com,https://www.telegramcrmai.com,http://localhost:5173,https://telegramcrmai.shop"

    # Database
    MONGODB_URL: str = "mongodb://localhost:27017/telegram"
    DATABASE_NAME: str = "telegram"

    # Security
    SECRET_KEY: str = "" # MUST be set in .env
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 43200

    # Telegram
    DEFAULT_API_ID: Optional[int] = None
    DEFAULT_API_HASH: Optional[str] = None

    # Razorpay
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

    # Member Adding Settings
    MA_CONSECUTIVE_PRIVACY_THRESHOLD: int = 10
    MA_MAX_FLOOD_SLEEP_THRESHOLD: int = 300  # Max seconds to sleep before pausing account
    MA_ACCOUNT_LIMIT_CAP: int = 40  # Absolute max members per account per mission
    MA_COOLDOWN_MAX: int = 86400    # Absolute max cooldown for any error
    MA_COOLDOWN_24H: int = 86400

    # Email Settings (for OTP)
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_FROM: str = ""

    @field_validator("DEFAULT_API_ID", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "":
            return None
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
