from beanie import Document
from typing import List, Optional
from datetime import datetime
from pydantic import Field

class AccountAgingTask(Document):
    user_id: str
    selected_account_ids: List[str] = []
    
    # Configuration
    min_delay: int = 10  # seconds
    max_delay: int = 14  # seconds
    parallel_sessions: int = 10 # Number of concurrent chat sessions
    use_max_parallelism: bool = False # If true, use all accounts in pairs instantly
    is_active: bool = False
    
    # Stats
    total_messages_sent: int = 0
    last_message_at: Optional[datetime] = None
    
    class Settings:
        name = "account_aging_tasks"
