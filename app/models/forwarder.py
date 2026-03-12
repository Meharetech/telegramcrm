from datetime import datetime
from typing import List, Optional
from beanie import Document
from pydantic import Field

class ForwarderRule(Document):
    """
    Configuration for forwarding messages from a source to multiple targets.
    """
    user_id: str = "legacy_user"
    account_id: Optional[str] = None
    name: str
    is_enabled: bool = True
    
    # Source & Targets
    source_id: str             # Telegram Peer ID (e.g. -100...)
    target_ids: List[str]      # List of Telegram Peer IDs
    
    # Forwarding Logic
    forward_mode: str = "copy" # "forward" (original tag) or "copy" (re-send content only)
    remove_caption: bool = False
    add_custom_text: Optional[str] = None # Text to append/prepend
    
    # Filters
    keyword_filters: List[str] = []    # Only if contains these (OR)
    blacklist_keywords: List[str] = [] # Skip if contains these (OR)
    word_replacements: List[dict] = [] # {"word": "a", "replace": "b"}
    replace_usernames: Optional[str] = None  # Replace ALL @usernames with this
    replace_links: Optional[str] = None      # Replace ALL http(s) links with this
    
    # Anti-Ban / Rate Limiting
    min_delay: int = 5         # seconds
    max_delay: int = 30        # seconds
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "forwarder_rules"
        # FIX: Indexes for fast per-account/per-user rule lookups
        indexes = [
            "account_id",
            "user_id",
            [("account_id", 1), ("is_enabled", 1)],
            [("user_id", 1), ("account_id", 1)],
        ]
