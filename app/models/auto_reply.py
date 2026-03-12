"""
auto_reply.py — MongoDB models for the Auto Responder system.
"""

from datetime import datetime
from typing import List, Optional
from beanie import Document
from pydantic import Field


class AutoReplyRule(Document):
    """
    One keyword-trigger rule per document.
    """
    user_id:       str = "legacy_user"
    account_id:    str
    name:          str          # display name, e.g. "Promo reply"
    is_enabled:    bool = True

    # Trigger
    trigger_type:  str = "keyword"   # "keyword" | "welcome" | "any"
    keywords:      List[str] = []    # only used when trigger_type == "keyword"
    match_mode:    str = "contains"  # "contains" | "exact" | "startswith"
    case_sensitive: bool = False

    # Reply
    reply_text:    str = ""
    media_paths:   List[str] = []
    tg_media:      List[dict] = []  # Telegram-hosted media references

    # Scope
    apply_to:      str = "both"   # "dm" | "group" | "both"
    group_reply_mode: str = "all"  # "all" | "selected"
    allowed_group_ids: List[str] = [] # whitelist for this specific rule

    # Delay
    delay_seconds: int = 3        # 0 = instant, default 3s

    created_at:    datetime = Field(default_factory=datetime.utcnow)
    updated_at:    datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "auto_reply_rules"
        # FIX: Index for fast per-account rule lookups (called on EVERY incoming message)
        indexes = [
            "account_id",
            [("account_id", 1), ("is_enabled", 1)],
            [("user_id", 1), ("account_id", 1)],
        ]


class AutoReplySettings(Document):
    """
    Per-account master settings for the auto responder.
    One document per account_id.
    """
    user_id:          str = "legacy_user"
    account_id:       str
    is_enabled:       bool = False   # master on/off switch

    # Standard welcome (fires on first DM)
    welcome_enabled:         bool = False
    welcome_mode:            str = "standard"  # "standard" | "time_based"
    welcome_message:         str = "👋 Hi! Thanks for reaching out. I'll get back to you soon."
    welcome_tg_media:        List[dict] = []   # Telegram-hosted media for welcome message

    # Night-shift message (used in time_based mode)
    night_shift_enabled:     bool = False   # dedicated Night Shift on/off
    welcome_message_night:   str = "😴 Good night! I'm currently away, but I'll get back to you soon."
    night_tg_media:          List[dict] = []   # Telegram-hosted media for night shift message
    night_allow_rules:       bool = False      # If True, keyword rules still work during night

    # Day window — Night is EVERYTHING OUTSIDE this window
    # Default: Day = 06:00 AM to 11:00 PM IST → Night = 11 PM to 6 AM
    day_start:               str = "06:00"   # HH:mm (12-hour clock, see ampm)
    day_start_ampm:          str = "AM"      # AM | PM
    day_end:                 str = "11:00"   # HH:mm (12-hour clock, see ampm)
    day_end_ampm:            str = "PM"      # AM | PM
    timezone:                str = "Asia/Kolkata"

    # Scope toggles
    dm_enabled:       bool = True
    group_enabled:    bool = False
    group_reply_mode: str  = "all"      # "all" | "selected"
    allowed_group_ids: List[str] = []   # only used if mode == "selected"

    # Defaults
    default_delay:    int = 3        # seconds

    created_at:       datetime = Field(default_factory=datetime.utcnow)
    updated_at:       datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "auto_reply_settings"
        # FIX: Index for fast per-account settings lookup (called on EVERY incoming message)
        indexes = [
            "account_id",
            [("account_id", 1), ("is_enabled", 1)],
            [("user_id", 1), ("account_id", 1)],
        ]
