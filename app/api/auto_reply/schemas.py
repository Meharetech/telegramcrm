from typing import List, Optional
from pydantic import BaseModel

class SettingsPayload(BaseModel):
    is_enabled:            bool = False
    welcome_enabled:       bool = False
    welcome_mode:          str  = "standard"      # standard | time_based
    welcome_message:       str  = "👋 Hi! Thanks for reaching out. I'll get back to you soon."
    welcome_tg_media:      List[dict] = []        # Telegram-hosted media for welcome
    night_shift_enabled:   bool = False
    welcome_message_night: str  = "😴 Good night! I'm currently away, but I'll get back to you soon."
    night_tg_media:        List[dict] = []        # Telegram-hosted media for night shift
    night_allow_rules:      bool = False

    # Day window (Night = everything outside)
    day_start:             str  = "06:00"
    day_start_ampm:        str  = "AM"
    day_end:               str  = "11:00"
    day_end_ampm:          str  = "PM"
    timezone:              str  = "Asia/Kolkata"

    dm_enabled:            bool = True
    group_enabled:         bool = False
    group_reply_mode:      str  = "all"
    allowed_group_ids:     List[str] = []
    default_delay:         int  = 2


class RulePayload(BaseModel):
    name:           str
    is_enabled:     bool         = True
    trigger_type:   str          = "keyword"   # keyword | welcome | any
    keywords:       List[str]    = []
    match_mode:     str          = "contains"  # contains | exact | startswith
    case_sensitive: bool         = False
    reply_text:     str          = ""
    media_paths:    List[str]    = []
    tg_media:       List[dict]   = []
    apply_to:       str          = "both"      # dm | group | both
    group_reply_mode: str        = "all"
    allowed_group_ids: List[str] = []
    delay_seconds:  int          = 2
