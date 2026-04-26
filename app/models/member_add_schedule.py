from datetime import datetime, timezone
from typing import List, Optional, Dict
from beanie import Document
from pydantic import Field

class MemberAddSchedule(Document):
    user_id: str
    label: str = "Daily Mission"
    destination_group: str # The group link (t.me/...) where members are added
    destination_group_name: Optional[str] = None # For UI display
    # Selected accounts and their per-account counts
    # List of { id: account_id, count: target_count }
    account_configs: List[Dict] = []
    source_type: str = "contacts" # contacts, custom_list
    member_list: List[str] = [] # List of usernames/IDs
    
    scheduled_time: str # "HH:MM" format (24h)
    min_delay: int = 30
    max_delay: int = 60
    
    is_active: bool = True
    last_run_date: Optional[str] = None # "YYYY-MM-DD"
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "member_add_schedules"
