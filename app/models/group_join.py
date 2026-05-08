from beanie import Document
from pydantic import Field
from datetime import datetime, timezone
from typing import List, Dict

class GroupJoinJob(Document):
    user_id: str
    account_id: str
    phone_number: str
    links: List[str]
    interval: int # in minutes
    status: str = "running" # running, stopped, completed
    done_count: int = 0
    total_count: int = 0
    logs: List[Dict] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "group_join_jobs"
