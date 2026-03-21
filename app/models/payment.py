from datetime import datetime, timezone
from typing import Optional
from beanie import Document
from pydantic import Field

class Payment(Document):
    """
    Stores payment transaction details (Razorpay, Manual, Crypto)
    """
    user_id: str
    user_email: str
    user_phone: Optional[str] = None
    plan_id: str
    plan_name: str
    amount: float
    currency: str = "INR"
    
    gateway: str = "razorpay" # "razorpay", "manual", "crypto"
    sub_gateway: Optional[str] = None # e.g. "PhonePe", "USDT (TRC20)"
    status: str = "success" # "pending", "success", "rejected"
    
    # Gateway specific references
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    
    transaction_ref: Optional[str] = None # For manual/crypto (UPITransaction ID or Hash)
    proof_image_url: Optional[str] = None # URL to uploaded screenshot
    
    billing_cycle: str = "monthly"
    admin_note: Optional[str] = None
    verified_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    class Settings:
        name = "payments"
        indexes = [
            "user_id",
            "status",
            [("razorpay_order_id", 1)],
        ]
