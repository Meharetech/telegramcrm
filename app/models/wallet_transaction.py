from datetime import datetime, timezone
from typing import Optional
from beanie import Document
from pydantic import Field

class WalletTransaction(Document):
    """
    Tracks all wallet credits and debits.
    """
    user_id: str
    amount: float
    type: str  # "credit" (top-up) or "debit" (purchase)
    description: str
    reference_id: Optional[str] = None # Payment ID or Account ID
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "wallet_transactions"
        indexes = [
            "user_id",
            "type",
            "created_at"
        ]
