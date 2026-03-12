from datetime import datetime
from typing import List, Optional
from beanie import Document
from pydantic import Field

class ReactionTask(Document):
    """
    Task for boosting reactions on a specific Telegram post.
    """
    user_id: str = "legacy_user"
    target_link: str             # Channel/Group username or private link
    message_id: Optional[int] = None   # Specific message ID (if None, can mean 'latest')
    
    emojis: List[str]            # List of emojis to pick from
    task_type: str = "one_time"  # "one_time" or "upcoming"
    
    # Execution State
    account_ids: List[str]       # List of TelegramAccount ObjectIDs to use
    processed_accounts: List[str] = [] # For one_time: Successfully reacted accounts. For upcoming: used as global log.
    failed_accounts: List[dict] = []  # [{"id": "...", "error": "..."}]
    
    # Tracking for 'upcoming' mode
    reacted_messages: List[int] = [] # List of message IDs already reacted to
    
    status: str = "pending"      # pending, running, completed, partially_completed, cancelled, monitoring
    is_active: bool = True       # Control for upcoming tasks
    
    # Timing
    min_delay: int = 5           # seconds
    max_delay: int = 20          # seconds
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "reaction_tasks"
        # FIX: Indexes — without these every find() is a full collection scan
        indexes = [
            "user_id",
            "status",
            [("user_id", 1), ("status", 1)],
            [("user_id", 1), ("created_at", -1)],
        ]
