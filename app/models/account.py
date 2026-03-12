from datetime import datetime
from typing import Optional, List
from beanie import Document, Indexed
from bson import ObjectId
from pydantic import Field, BaseModel

class TelegramAccount(Document):
    user_id: str = "legacy_user"  # Default for existing data
    phone_number: str
    api_id: int
    api_hash: str
    session_string: Optional[str] = None
    device_model: Optional[str] = "Telegram Android"
    password: Optional[str] = None  # Store 2FA password if provided
    is_active: bool = True
    status: str = "disconnected"  # disconnected, connecting, online, error
    daily_contacts_limit: int = 200
    contacts_added_today: int = 0
    last_contact_add_date: Optional[datetime] = None
    contact_count: int = 0
    unread_count: int = 0  # Global unread messages count
    last_message_at: Optional[datetime] = None # Timestamp of last incoming message
    last_sync_date: Optional[datetime] = None
    flood_wait_until: Optional[datetime] = None # For persistent flood management
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_check_status: Optional[str] = None
    last_check_time: Optional[datetime] = None
    
    # Task Tracking (Phase 2)
    active_task_id: Optional[str] = None
    active_task_type: Optional[str] = None # 'member_add', 'campaign', 'scrape'
    
    class AccountShort(BaseModel):
        id: Optional[ObjectId] = Field(None, alias="_id")
        phone_number: str
        status: str
        device_model: Optional[str]
        daily_contacts_limit: int
        contacts_added_today: int
        unread_count: int
        last_message_at: Optional[datetime]
        contact_count: int
        created_at: datetime
        last_check_status: Optional[str]
        last_check_time: Optional[datetime]
        last_sync_date: Optional[datetime]
        flood_wait_until: Optional[datetime]
        is_active: bool
        active_task_id: Optional[str]
        active_task_type: Optional[str]

        class Config:
            populate_by_name = True
            arbitrary_types_allowed = True
    
    class Settings:
        name = "telegram_accounts"
        # FIX: Indexes for fast per-user account lookups
        indexes = [
            "user_id",
            "phone_number",
            [("user_id", 1), ("is_active", 1)],
            [("user_id", 1), ("status", 1), ("is_active", 1)], # Optimize dashboard filters
            [("phone_number", 1), ("user_id", 1)], # Fast collision checks
        ]
