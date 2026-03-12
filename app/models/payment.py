from datetime import datetime, timezone
from typing import Optional
from beanie import Document
from pydantic import Field

class Payment(Document):
    """
    Stores Razorpay payment transaction details
    """
    user_id: str
    user_email: str
    plan_id: str
    plan_name: str
    amount_inr: float
    
    razorpay_order_id: str
    razorpay_payment_id: str
    status: str = "success" # Usually we only record successful ones after verification
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    class Settings:
        name = "payments"
        indexes = [
            "user_id",
            [("razorpay_order_id", 1)], # Index for duplicate checks
        ]
