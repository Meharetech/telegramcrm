from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from telethon import functions, types
from app.models import TelegramAccount
from app.client_cache import get_client

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
