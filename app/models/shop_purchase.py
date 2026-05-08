from datetime import datetime, timezone
from typing import Optional
from beanie import Document, Indexed
from pydantic import Field

class ShopPurchase(Document):
    user_id: str
    account_id: str
    phone_number: str
    price: float
    status: str = "pending"  # pending, success, expired, cancelled
    purchase_type: str = "otp" # otp, direct
    otp_message: Optional[str] = None
    otp_received_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "shop_purchases"
        indexes = [
            "user_id",
            "account_id",
            "status",
            "created_at"
        ]
