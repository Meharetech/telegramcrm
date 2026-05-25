from beanie import Document
from pydantic import Field
from datetime import datetime, timezone
from typing import List, Dict, Optional

class ReportJob(Document):
    user_id: str
    target: str # username or link of bot/group/channel/user
    reason: str # spam, violence, etc.
    account_configs: List[Dict] # [{"id": "acc_id", "phone": "phone_num"}]
    messages: List[str] # List of message templates to rotate
    min_delay: int
    max_delay: int
    batch_size: int = 1
    status: str = "running" # running, stopped, completed
    done_count: int = 0
    total_count: int = 0
    errors_count: int = 0
    logs: List[Dict] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "report_jobs"
