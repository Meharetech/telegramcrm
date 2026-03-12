import os
import shutil
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File
from app.models.reminder import Reminder
from app.models.account import TelegramAccount
from app.api.auth_utils import get_current_user
from app.models.user import User
from bson import ObjectId

router = APIRouter()

UPLOAD_DIR = "uploads/reminders"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@router.post("/create")
async def create_reminder(
    account_id: str = Form(...),
    chat_id: str = Form(...),
    message: str = Form(...),
    remind_at: str = Form(...), # ISO string
    chat_name: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user)
):
    media_path = None
    telegram_media = None
    telegram_message_id = None
    if image:
        file_extension = os.path.splitext(image.filename)[1]
        temp_filename = f"temp_{user.id}_{datetime.utcnow().timestamp()}{file_extension}"
        temp_path = os.path.join(UPLOAD_DIR, temp_filename)
        # ── ASYNC FILE WRITE (SMOOTH UPLOAD) ────────────────────────────────
        try:
            # We use chunks to avoid loading the whole file into RAM at once
            import aiofiles
            async with aiofiles.open(temp_path, "wb") as out_file:
                while content := await image.read(1024 * 1024): # 1MB chunks
                    await out_file.write(content)
        except ImportError:
            # Fallback if aiofiles isn't installed (though it should be for SaaS scale)
            with open(temp_path, "wb") as buffer:
                buffer.write(await image.read())
        
        try:
            # Upload to Telegram immediately
            from app.client_cache import get_client
            account = await TelegramAccount.get(account_id)
            if account:
                client = await get_client(
                    str(account.id),
                    account.session_string,
                    account.api_id,
                    account.api_hash,
                    device_model=getattr(account, 'device_model', 'Telegram Android')
                )
                
                # Send to 'me' to get a permanent media reference
                sent_msg = await client.send_file('me', temp_path)
                if sent_msg:
                    telegram_message_id = sent_msg.id
                    # Also store media dict as fallback
                    if sent_msg.media:
                        telegram_media = sent_msg.media.to_dict()
                    
                    # Since it's on Telegram, we don't need the local file anymore
                    # But we'll keep it for now as a safety fallback until logic.py is tested
                    media_path = temp_path 
        except Exception as e:
            print(f"Error uploading to Telegram: {e}")
            media_path = temp_path

    try:
        remind_dt = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid remind_at format: {e}")

    reminder = Reminder(
        user_id=str(user.id),
        telegram_account_id=account_id,
        chat_id=chat_id,
        chat_name=chat_name,
        message=message,
        media_path=media_path,
        telegram_media=telegram_media,
        telegram_message_id=telegram_message_id,
        remind_at=remind_dt
    )
    await reminder.insert()
    return {"status": "ok", "reminder_id": str(reminder.id)}

@router.get("/active-popups")
async def get_active_popups(user: User = Depends(get_current_user)):
    # ── FIX: Return empty immediately if services are globally stopped ───────
    if not user.services_active:
        return []

    reminders = await Reminder.find(
        Reminder.user_id == str(user.id),
        Reminder.status == "triggered",
        Reminder.popup_status == "not_closed"
    ).to_list()
    # Convert to JSON serializable list
    return [
        {
            "id": str(r.id),
            "chat_name": r.chat_name,
            "message": r.message,
            "remind_at": r.remind_at.isoformat(),
            "chat_id": r.chat_id,
            "account_id": r.telegram_account_id,
            "image_url": r.media_path.replace("\\", "/") if r.media_path else None
        }
        for r in reminders
    ]

@router.post("/close/{reminder_id}")
async def close_reminder(reminder_id: str, user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(reminder_id):
        raise HTTPException(status_code=400, detail="Invalid reminder ID")
    reminder = await Reminder.find_one(
        Reminder.id == ObjectId(reminder_id),
        Reminder.user_id == str(user.id)
    )
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")
    
    reminder.popup_status = "closed"
    reminder.status = "completed"
    await reminder.save()
    return {"status": "ok"}

@router.get("/list")
async def list_reminders(user: User = Depends(get_current_user)):
    reminders = await Reminder.find(
        Reminder.user_id == str(user.id)
    ).sort("-created_at").to_list()
    
    # FIX: Return serializable dicts — raw Beanie documents can crash with
    # ObjectId serialization errors when returned directly from FastAPI.
    result = []
    for r in reminders:
        result.append({
            "id":                   str(r.id),
            "chat_id":              r.chat_id,
            "chat_name":            r.chat_name,
            "message":              r.message,
            "remind_at":            r.remind_at.isoformat(),
            "status":               r.status,
            "popup_status":         r.popup_status,
            "created_at":           r.created_at.isoformat(),
            "triggered_at":         r.triggered_at.isoformat() if r.triggered_at else None,
            "telegram_account_id":  r.telegram_account_id,
            "media_path":           r.media_path,
            "retry_count":          getattr(r, "retry_count", 0),
        })
    return result

@router.delete("/delete/{reminder_id}")
async def delete_reminder(reminder_id: str, user: User = Depends(get_current_user)):
    if not ObjectId.is_valid(reminder_id):
        raise HTTPException(status_code=400, detail="Invalid reminder ID")
    reminder = await Reminder.find_one(
        Reminder.id == ObjectId(reminder_id),
        Reminder.user_id == str(user.id)
    )
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")
    
    # Ideally delete media file too
    if reminder.media_path and os.path.exists(reminder.media_path):
        try: os.remove(reminder.media_path)
        except: pass
        
    await reminder.delete()
    return {"status": "ok"}

@router.put("/update/{reminder_id}")
async def update_reminder(
    reminder_id: str,
    message: Optional[str] = Form(None),
    remind_at: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user)
):
    if not ObjectId.is_valid(reminder_id):
        raise HTTPException(status_code=400, detail="Invalid reminder ID")
    reminder = await Reminder.find_one(
        Reminder.id == ObjectId(reminder_id),
        Reminder.user_id == str(user.id)
    )
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")

    if message:
        reminder.message = message
    
    if remind_at:
        try:
            reminder.remind_at = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
            # FIX: Only reset status to 'pending' when the time is changed.
            # Previously, ANY field update (even just message text) would reset
            # an already-triggered reminder back to pending and re-send it.
            if reminder.status in ("triggered", "completed", "error"):
                reminder.status = "pending"
                reminder.retry_count = 0
                reminder.error_message = None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid remind_at: {e}")

    if image:
        # Delete old media if exists
        if reminder.media_path and os.path.exists(reminder.media_path):
            try:
                os.remove(reminder.media_path)
            except OSError:
                pass
            
        file_extension = os.path.splitext(image.filename)[1]
        filename = f"{user.id}_{datetime.utcnow().timestamp()}{file_extension}"
        media_path = os.path.join(UPLOAD_DIR, filename)
        try:
            import aiofiles
            async with aiofiles.open(media_path, "wb") as out_file:
                while content := await image.read(1024 * 1024):
                    await out_file.write(content)
        except ImportError:
            with open(media_path, "wb") as buffer:
                buffer.write(await image.read())
        reminder.media_path = media_path
        # Reset cloud reference since media changed
        reminder.telegram_message_id = None
        reminder.telegram_media = None

    await reminder.save()
    return {"status": "ok"}
