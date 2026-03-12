from datetime import datetime, timezone
from typing import List, Optional, Dict
from beanie import Document
from pydantic import Field

class MemberAddJob(Document):
    user_id: str
    group_link: str
    status: str = "running" # running, completed, stopped, failed
    done_count: int = 0
    total_count: int = 0
    errors_count: int = 0
    
    # Store the account-specific progress/configs
    # Config for reconstruction
    account_configs: List[Dict] = []
    # Results per account in this job
    # { account_id: { phone, done, errors, privacy_errors, status, last_error } }
    account_results: Dict[str, Dict] = {}
    min_delay: int = 30
    max_delay: int = 60
    
    # Store last 100 logs for history tracking in DB
    logs: List[Dict] = []
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "member_add_jobs"
