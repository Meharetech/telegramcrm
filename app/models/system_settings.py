from typing import List, Optional
from beanie import Document
from pydantic import BaseModel, Field

class ManualGateway(BaseModel):
    name: str
    qr_code_url: Optional[str] = None
    upi_id: Optional[str] = None
    instructions: Optional[str] = None
    is_active: bool = True

class CryptoGateway(BaseModel):
    name: str
    symbol: str  # e.g., USDT
    network: str # e.g., TRC20
    wallet_address: str
    qr_code_url: Optional[str] = None
    is_active: bool = True

class SystemSettings(Document):
    # Master toggles
    razorpay_enabled: bool = True
    manual_payment_enabled: bool = True
    crypto_payment_enabled: bool = True
    
    # Razorpay Credentials
    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    
    # Gateways
    manual_gateways: List[ManualGateway] = []
    crypto_gateways: List[CryptoGateway] = []

    # Shop Settings
    shop_account_price: float = 45.0
    shop_otp_timeout_mins: int = 3

    # Demo / Free Tier Limits
    demo_max_accounts: int = 5
    demo_max_proxies: int = 50
    demo_max_api_keys: int = 50
    demo_daily_contacts_limit: int = 0
    demo_can_auto_reply: bool = False
    demo_can_forward: bool = False
    demo_can_react: bool = False
    demo_access_member_adding: bool = False
    demo_access_group_joiner: bool = False
    demo_access_group_scraping: bool = False
    demo_access_message_sender: bool = False
    demo_access_terminal: bool = False
    demo_access_contacts_manager: bool = False
    demo_access_reminders: bool = False
    demo_access_bot_hub: bool = False
    demo_access_folder_campaign: bool = False
    demo_access_folder_scraper: bool = False
    demo_access_creative_tools: bool = False
    demo_access_ban_checker: bool = False
    demo_access_ai_agent: bool = False
    demo_access_account_aging: bool = False
    
    # Demo Quantity Limits
    demo_max_auto_replies: int = 1
    demo_max_reaction_channels: int = 1
    demo_max_forwarder_channels: int = 1
    demo_max_bots: int = 1
    demo_ai_chatbot_limit: int = 200
    demo_max_ai_agents: int = 1

    # Demo Default Resources
    demo_raw_proxies: Optional[str] = None
    demo_raw_apis: Optional[str] = None

    # Admin Agent Permissions
    agent_can_manage_plans: bool = True
    agent_can_manage_demo_settings: bool = True
    agent_can_manage_aging: bool = True

    class Settings:
        name = "system_settings"
