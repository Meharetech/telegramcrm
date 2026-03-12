from beanie import Document, Indexed
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pydantic import Field

class MessageCampaignJob(Document):
    user_id: Indexed(str)
    status: str = "running" # running, stopped, completed, error
    method: str = "contact" # contact (existing contacts), username (username list)
    username_list: List[str] = []
    message_text: str = ""
    
    per_account_limit: int = 25
    min_delay: int = 30
    max_delay: int = 60
    
    done_count: int = 0
    errors_count: int = 0
    total_targets: int = 0
    
    # Stores results per account: { "acc_id": { "phone": "...", "done": 0, "status": "active/failed", "last_err": "..." } }
    account_results: Dict[str, Any] = {}
    
    logs: List[Dict[str, Any]] = []
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "message_campaign_jobs"
        indexes = ["user_id", "status", "created_at"]
