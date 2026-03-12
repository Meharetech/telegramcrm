import os
from datetime import datetime, timedelta
from typing import Optional, Union
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from app.models.user import User
from app.models.plan import Plan
from bson import ObjectId

from app.config import settings

# Configuration from environment
SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/users/login")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="api/users/login", auto_error=False)

async def get_current_user_optional(token: str = Depends(oauth2_scheme_optional)):
    if not token: return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id: return None
        return await User.get(user_id)
    except:
        return None

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_user_from_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id: return None
        return await User.get(user_id)
    except:
        return None

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    user = await User.get(user_id)
    if user is None:
        raise credentials_exception
        
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Your account has been deactivated. Please contact support.")
        
    return user

async def check_plan_limit(user: User, field: str, current_count: Optional[int] = None):
    """
    Checks if a user has access to a feature or if they've exceeded a quantity limit.
    - If current_count is None: checks if the boolean 'field' is True.
    - If current_count is provided: checks if current_count < plan[field] (where -1 is unlimited).
    - If user has NO plan: everything (except basic dashboard) is restricted.
    """
    if user.is_admin:
        return True # Admin always bypasses limits

    if not user.plan_id:
        raise HTTPException(status_code=403, detail="No active subscription plan found. Please purchase a plan.")

    # Check for expiry
    if user.plan_expiry_at:
        # Normalize to UTC for comparison
        from datetime import timezone
        now = datetime.now(timezone.utc)
        # Ensure user.plan_expiry_at is timezone-aware if the comparison demands it, 
        # or compare naive if both are naive. Beanie usually stores as UTC/aware.
        expiry = user.plan_expiry_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
            
        if now > expiry:
            raise HTTPException(status_code=403, detail="Your subscription plan has expired. Please renew to continue using this feature.")

    plan = await Plan.get(ObjectId(user.plan_id))
    if not plan:
        raise HTTPException(status_code=403, detail="Assigned plan no longer exists. Please contact admin.")

    if not plan.is_active:
        raise HTTPException(status_code=403, detail="Your current plan is inactive. Contact admin.")

    val = getattr(plan, field, None)

    # ── Service Overrides Check ──────────────────────────────────────────────
    # If the user has manual force-enable/disable for the service corresponding to this field
    service_map = {
        "max_auto_replies": "auto_reply",
        "access_group_scraping": "scraper",
        "access_member_adding": "member_adding",
        "max_reaction_channels": "reactions",
        "max_forwarder_channels": "forwarder",
        "access_message_sender": "campaign",
        "access_creative_tools": "creative",
        "access_ban_checker": "ban_checker",
        "access_terminal": "terminal",
        "access_contacts_manager": "contacts",
        "access_reminders": "reminders"
    }
    
    svc_id = service_map.get(field)
    if svc_id:
        disabled = getattr(user, "disabled_services", [])
        enabled = getattr(user, "enabled_services", [])
        
        if svc_id in disabled:
            raise HTTPException(status_code=403, detail=f"The '{svc_id}' service has been disabled for your account by an administrator.")
        
        if svc_id in enabled:
            # If forced enabled, we bypass plan-level booleans or restricted counts
            if current_count is None:
                return True # Forced boolean access
            if val == 0: # If plan specifically turned it off but admin forced it on
                val = 100 # Default allowance for forced services
            # Continue to quantity check with the modified val

    # Boolean access check
    if current_count is None:
        if val is not True:
            raise HTTPException(status_code=403, detail=f"Your plan does not include access to this feature: {field}")
        return True

    # Quantity limit check
    if val == -1:
        return True # Unlimited
    
    if current_count >= val:
        raise HTTPException(status_code=403, detail=f"Plan limit reached: {val} {field}. Upgrade your plan for more.")
    
    return True
