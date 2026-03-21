from datetime import datetime, timezone
from typing import Optional
from beanie import Document
from pydantic import Field, BaseModel
from pymongo import IndexModel, ASCENDING
from bson import ObjectId

class User(Document):
    email: str
    phone: Optional[str] = None
    hashed_password: str
    full_name: Optional[str] = None
    is_active: bool = False
    is_admin: bool = False
    is_admin_active: bool = False
    is_super_admin: bool = False # New field for protected admin
    services_active: bool = True # Default to True now
    disabled_services: list[str] = [] # List of service names disabled for this user
    enabled_services: list[str] = [] # List of service names FORCE enabled for this user
    plan_id: Optional[str] = None  # Reference to Plan._id assigned by admin
    plan_expiry_at: Optional[datetime] = None
    billing_cycle: Optional[str] = None # 'monthly' or 'yearly'
    last_start_at: Optional[datetime] = None
    last_stop_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reset_code: Optional[str] = None
    reset_code_expiry: Optional[datetime] = None
    reg_otp: Optional[str] = None
    reg_otp_expiry: Optional[datetime] = None

    class UserShort(BaseModel):
        id: Optional[ObjectId] = Field(None, alias="_id")
        email: str
        phone: Optional[str] = None
        full_name: Optional[str]
        is_active: bool
        is_admin: bool
        is_super_admin: bool
        plan_id: Optional[str]
        plan_expiry_at: Optional[datetime]
        billing_cycle: Optional[str]
        services_active: bool
        disabled_services: list[str]
        enabled_services: list[str]
        created_at: datetime

        class Config:
            populate_by_name = True
            arbitrary_types_allowed = True
    
    class Settings:
        name = "users"
        # FIX: Unique index on email — prevents race condition duplicate registrations
        # at the DB level (Python-level check alone is not sufficient under concurrency)
        indexes = [
            IndexModel([("email", ASCENDING)], unique=True),
        ]

