from beanie import Document
from datetime import datetime, timezone
from typing import List, Optional, Dict
from pydantic import Field

class FolderCampaignJob(Document):
    user_id: str
    account_id: str
    phone_number: str = "Unknown"
    folder_id: str
    folder_name: str
    selected_group_ids: List[str]
    group_metadata: Dict[str, Dict] = {} # {group_id: {username, title, etc}}
    message_text: str
    min_delay: int
    max_delay: int
    repeat_interval: Optional[int] = None
    status: str = "running" # running, stopped, completed
    done_count: int = 0
    total_targets: int = 0
    logs: List[Dict] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "folder_campaign_jobs"
