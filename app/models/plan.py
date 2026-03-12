from datetime import datetime, timezone
from typing import Optional
from beanie import Document
from pydantic import Field


class Plan(Document):
    """
    Admin-created subscription plan that governs what a user can access.
    Limits of -1 mean "unlimited".
    """
    name: str
    description: Optional[str] = None
    price_inr: float = 0.0          # Price in INR monthly
    price_yearly_inr: float = 0.0   # Price in INR yearly (discounted)
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ── Quantity Limits ──────────────────────────────────────────────────────
    max_accounts: int = 10          # -1 = unlimited
    max_api_keys: int = 10          # -1 = unlimited
    max_proxies: int = 10           # -1 = unlimited
    max_auto_replies: int = 0       # -1 = unlimited
    max_reaction_channels: int = 0  # -1 = unlimited
    max_forwarder_channels: int = 0 # -1 = unlimited

    # ── Feature Toggles ─────────────────────────────────────────────────────
    access_chat_message: bool = False      # Bulk/schedule message sending
    access_member_adding: bool = False     # Member adding / scraping
    access_message_sender: bool = False    # Campaign message sender
    access_group_scraping: bool = False    # Group scraper
    access_connect: bool = True            # Allow connecting accounts at all
    access_ban_checker: bool = False       # Ban filter checker tool
    access_creative_tools: bool = False    # Create groups & channels
    access_contacts_manager: bool = False  # Contacts management
    access_reminders: bool = False         # Scheduled reminders
    access_terminal: bool = False          # Terminal access toggle

    class Settings:
        name = "plans"
