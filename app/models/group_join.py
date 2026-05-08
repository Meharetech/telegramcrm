from beanie import Document
from pydantic import Field
from datetime import datetime, timezone
from typing import List, Dict, Optional

class GroupJoinJob(Document):
    user_id: str
    batch_id: Optional[str] = None
    account_id: str
    phone_number: str
    links: List[str]
    interval: int # in seconds
    task_type: str = "join" # join, leave
    status: str = "running" # running, stopped, completed
    done_count: int = 0
    total_count: int = 0
    logs: List[Dict] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "group_join_jobs"
