import os
import shutil
import uuid
from fastapi import APIRouter, HTTPException, UploadFile, File, Depends
from app.models import TelegramAccount, User
from app.client_cache import get_client
from app.api.auth_utils import get_current_user
from bson import ObjectId

router = APIRouter()

UPLOAD_DIR = "uploads/auto_reply"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@router.post("/upload")
async def upload_media(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Upload a file to be attached to an auto-reply rule."""
    ext = os.path.splitext(file.filename)[1]
    filename = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    abs_path = os.path.abspath(file_path)
    return {
        "file_path": abs_path,
        "filename": file.filename,
        "url_path": f"/uploads/auto_reply/{filename}" 
    }

@router.post("/upload-tg/{account_id}")
async def upload_to_telegram(account_id: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """
    Upload a file directly to Telegram (Saved Messages) and return its media reference.
    This makes auto-replies much faster.
    """
    account = await TelegramAccount.find_one(
        TelegramAccount.id == ObjectId(account_id),
        TelegramAccount.user_id == str(current_user.id)
    )
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
        
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
    
    try:
        temp_path = f"temp_{uuid.uuid4()}_{file.filename}"
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
            
        msg = await client.send_file('me', temp_path)
        
        if os.path.exists(temp_path):
            os.remove(temp_path)
        
        if not msg.media:
            raise HTTPException(status_code=500, detail="Failed to get media reference from Telegram")
            
        return {
            "filename": file.filename,
            "media": {
                "type": "saved_msg",
                "msg_id": msg.id
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Telegram upload failed: {str(e)}")
