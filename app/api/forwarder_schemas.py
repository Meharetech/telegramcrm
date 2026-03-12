from typing import List, Optional
from pydantic import BaseModel

class ForwarderRulePayload(BaseModel):
    name: str
    is_enabled: bool = True
    source_id: str
    target_ids: List[str]
    forward_mode: str = "copy"
    remove_caption: bool = False
    add_custom_text: Optional[str] = None
    keyword_filters: List[str] = []
    blacklist_keywords: List[str] = []
    word_replacements: List[dict] = []
    replace_usernames: Optional[str] = None
    replace_links: Optional[str] = None
    min_delay: int = 5
    max_delay: int = 30
