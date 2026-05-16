from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from telethon import functions, types
from app.models import TelegramAccount
from app.client_cache import get_client
from pydantic import BaseModel

router = APIRouter()

@router.get("/me/{account_id}")
async def get_profile_me(account_id: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        me = await client.get_me()
        full = await client(functions.users.GetFullUserRequest(id=me))
        bio = full.full_user.about or ""
    except:
        bio = ""
        
    return {
        "id": me.id,
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "username": me.username or "",
        "phone": me.phone or "",
        "bio": bio
    }

@router.post("/update-profile/{account_id}")
async def update_profile(
    account_id: str,
    first_name: str = Form(None),
    last_name: str = Form(None),
    bio: str = Form(None)
):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        await client(functions.account.UpdateProfileRequest(
            first_name=first_name,
            last_name=last_name,
            about=bio
        ))
        return {"status": "success"}
    except Exception as e:
        from app.client_cache import handle_session_security_error
        await handle_session_security_error(account_id, str(e))
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/update-username/{account_id}")
async def update_username(account_id: str, username: str = Form(...)):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        await client(functions.account.UpdateUsernameRequest(username=username))
        return {"status": "success"}
    except Exception as e:
        from app.client_cache import handle_session_security_error
        await handle_session_security_error(account_id, str(e))
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/update-photo/{account_id}")
async def update_photo(account_id: str, file: UploadFile = File(...)):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        file_bytes = await file.read()
        uploaded_file = await client.upload_file(file_bytes, file_name=file.filename)
        await client(functions.photos.UploadProfilePhotoRequest(file=uploaded_file))
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/2fa-status/{account_id}")
async def get_2fa_status(account_id: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    try:
        pwd = await client(functions.account.GetPasswordRequest())
        return {"has_2fa": pwd.has_password}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/update-2fa/{account_id}")
async def update_2fa(
    account_id: str,
    current_password: str = Form(None),
    new_password: str = Form(None) # Pass empty string to remove 2FA
):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        await client.edit_2fa(current_password=current_password, new_password=new_password)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
class RandomizeSettings(BaseModel):
    change_username: bool = True
    change_name: bool = True
    change_bio: bool = True
    change_birthday: bool = True
    change_photo: bool = True
    apply_privacy: bool = True
    privacy_phone: str = "nobody"   # everyone, contacts, nobody
    privacy_calls: str = "nobody"   # everyone, contacts, nobody
    privacy_groups: str = "contacts" # everyone, contacts, nobody

async def apply_account_privacy(client, key_type, value_str):
    """Helper to apply a single privacy rule."""
    from telethon import types, functions
    
    # Map key
    if key_type == "phone":
        key = types.InputPrivacyKeyPhoneNumber()
    elif key_type == "calls":
        key = types.InputPrivacyKeyPhoneCall()
    elif key_type == "groups":
        key = types.InputPrivacyKeyChatInvite()
    else:
        return
        
    # Map value
    if value_str == "everyone":
        value = [types.InputPrivacyValueAllowAll()]
    elif value_str == "contacts":
        value = [types.InputPrivacyValueAllowContacts()]
    else: # nobody
        value = [types.InputPrivacyValueDisallowAll()]
        
    try:
        await client(functions.account.SetPrivacyRequest(key=key, rules=value))
    except Exception as e:
        print(f"Failed to set privacy {key_type} to {value_str}: {e}")

@router.post("/random-username/{account_id}")
async def set_random_profile(account_id: str, settings: RandomizeSettings = RandomizeSettings()):
    import random
    import string
    import httpx
    import io
    from faker import Faker
    from telethon import types, functions
    from datetime import datetime, timedelta
    
    fake = Faker()
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.status == "frozen":
        raise HTTPException(status_code=403, detail="❄️ ACCOUNT FROZEN: This account is restricted by Telegram and cannot be updated.")
        
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        # 1. Update Username
        if settings.change_username:
            prefix = random.choice(string.ascii_lowercase)
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(11, 13)))
            new_username = f"{prefix}{suffix}"
            try:
                await client(functions.account.UpdateUsernameRequest(username=new_username))
                account.username = new_username
            except Exception as e:
                print(f"Username update failed: {e}")
        
        # 2. Update Name & Bio
        if settings.change_name or settings.change_bio:
            first_name = fake.first_name() if settings.change_name else None
            last_name = fake.last_name() if settings.change_name else None
            bio = fake.sentence(nb_words=6) if settings.change_bio else None
            
            await client(functions.account.UpdateProfileRequest(
                first_name=first_name if first_name else getattr(account, 'first_name', ''),
                last_name=last_name if last_name else getattr(account, 'last_name', ''),
                about=bio if bio else getattr(account, 'bio', '')
            ))
            
            if settings.change_name:
                account.first_name = first_name
                account.last_name = last_name
            if settings.change_bio:
                account.bio = bio
        
        # 3. Update Birthday (age 30-50)
        if settings.change_birthday:
            days_ago = random.randint(30*365, 50*365)
            bday_dt = datetime.now() - timedelta(days=days_ago)
            try:
                await client(functions.account.UpdateBirthdayRequest(
                    birthday=types.Birthday(day=bday_dt.day, month=bday_dt.month, year=bday_dt.year)
                ))
                account.birthday = bday_dt.strftime("%Y-%m-%d")
            except Exception as bday_err:
                print(f"Birthday update failed: {bday_err}")

        # 4. Update Profile Photo
        if settings.change_photo:
            try:
                # Get a random face from i.pravatar.cc (using account ID as seed for variety)
                avatar_url = f"https://i.pravatar.cc/500?u={account_id}"
                async with httpx.AsyncClient() as http:
                    resp = await http.get(avatar_url)
                    if resp.status_code == 200:
                        # Upload to Telegram
                        photo_file = await client.upload_file(resp.content, file_name="profile.jpg")
                        await client(functions.photos.UploadProfilePhotoRequest(file=photo_file))
            except Exception as photo_err:
                print(f"Photo update failed: {photo_err}")

        # 5. Apply Privacy Settings
        if settings.apply_privacy:
            await apply_account_privacy(client, "phone", settings.privacy_phone)
            await apply_account_privacy(client, "calls", settings.privacy_calls)
            await apply_account_privacy(client, "groups", settings.privacy_groups)
            # Save to database
            account.privacy_phone = settings.privacy_phone
            account.privacy_calls = settings.privacy_calls
            account.privacy_groups = settings.privacy_groups
            
        await account.save()
        
        return {
            "status": "success", 
            "username": account.username, 
            "name": f"{account.first_name} {account.last_name}" if account.first_name else None,
            "bio": account.bio,
            "privacy": {
                "phone": account.privacy_phone,
                "calls": account.privacy_calls,
                "groups": account.privacy_groups
            }
        }
    except Exception as e:
        from app.client_cache import handle_session_security_error
        await handle_session_security_error(account_id, str(e))
        import traceback
        error_msg = f"Telegram Error: {str(e)}"
        print(error_msg)
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=error_msg)

async def fetch_privacy_rule(client, key):
    """Helper to get privacy rule text from Telegram."""
    try:
        rules = await client(functions.account.GetPrivacyRequest(key=key))
        # rules.rules is a list of PrivacyRule objects
        # We look for AllowAll, AllowContacts, or DisallowAll
        for r in rules.rules:
            if isinstance(r, types.PrivacyValueAllowAll): return "everyone"
            if isinstance(r, types.PrivacyValueAllowContacts): return "contacts"
            if isinstance(r, types.PrivacyValueDisallowAll): return "nobody"
        return "nobody" # Default fallback
    except:
        return None

@router.post("/check-profile/{account_id}")
async def check_profile(account_id: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.status == "frozen":
        raise HTTPException(status_code=403, detail="❄️ ACCOUNT FROZEN: This account is restricted and cannot be synced currently.")
        
    try:
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
        me = await client.get_me()
        
        # Sync all details
        account.username = me.username if me.username else None
        account.first_name = me.first_name if me.first_name else None
        account.last_name = me.last_name if me.last_name else None
        
        # Bio requires FullUser
        try:
            full_user = await client(functions.users.GetFullUserRequest(id=me.id))
            account.bio = full_user.full_user.about if full_user.full_user.about else None
        except:
            pass
            
        # Sync Privacy Settings
        account.privacy_phone = await fetch_privacy_rule(client, types.InputPrivacyKeyPhoneNumber())
        account.privacy_calls = await fetch_privacy_rule(client, types.InputPrivacyKeyPhoneCall())
        account.privacy_groups = await fetch_privacy_rule(client, types.InputPrivacyKeyChatInvite())
            
        await account.save()
        
        return {
            "status": "success", 
            "username": account.username,
            "first_name": account.first_name,
            "last_name": account.last_name,
            "bio": account.bio,
            "privacy": {
                "phone": account.privacy_phone,
                "calls": account.privacy_calls,
                "groups": account.privacy_groups
            }
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
