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

    class Settings:
        name = "system_settings"
