import io
import asyncio
import logging
import os
import mimetypes
import tempfile
import uuid
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Query, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.sessions import StringSession
from telethon import TelegramClient, functions, types
from app.models import TelegramAccount, User
from app.api.auth_utils import get_current_user
from app.client_cache import get_client, invalidate

logger = logging.getLogger(__name__)
router = APIRouter()

async def extract_message_data(m):
    """Parses a Telethon Message object into a serializable dictionary."""
    sender_name = "Unknown"
    try:
        # Note: get_sender is usually cached by Telethon if the message was part of a get_messages call
        sender = await m.get_sender()
        if sender:
            # Handle both User and Channel/Chat senders
            sender_name = getattr(sender, 'first_name', '') or getattr(sender, 'title', 'Unknown')
            if getattr(sender, 'last_name', None):
                sender_name += f" {sender.last_name}"
    except Exception:
        pass

    media_type = None
    file_name = None
    file_size = None
    
    if m.media:
        if isinstance(m.media, MessageMediaPhoto):
            media_type = "photo"
        elif isinstance(m.media, MessageMediaDocument) and m.media.document:
            doc = m.media.document
            file_size = doc.size
            attrs = {type(a).__name__: a for a in doc.attributes}
            
            if 'DocumentAttributeFilename' in attrs:
                file_name = attrs['DocumentAttributeFilename'].file_name
                
            if 'DocumentAttributeSticker' in attrs:
                media_type = "sticker"
            elif 'DocumentAttributeAnimated' in attrs or (
                    'DocumentAttributeVideo' in attrs and
                    getattr(attrs.get('DocumentAttributeVideo'), 'round_message', False)):
                media_type = "gif"
            elif 'DocumentAttributeVideo' in attrs:
                media_type = "video"
            elif 'DocumentAttributeAudio' in attrs:
                audio = attrs['DocumentAttributeAudio']
                media_type = "voice" if getattr(audio, 'voice', False) else "audio"
            else:
                media_type = "document"

    text = m.text or ""
    # If no text but has media, provide a fallback type
    if not text and media_type is None and m.media:
        media_type = "document"

    reactions_data = []
    if hasattr(m, 'reactions') and m.reactions:
        for r in m.reactions.results:
            emoji = getattr(r.reaction, 'emoticon', None)
            if emoji:
                is_chosen = getattr(r, 'chosen', False) or (getattr(r, 'chosen_order', None) is not None)
                reactions_data.append({
                    "emoji": emoji, 
                    "count": r.count,
                    "chosen": bool(is_chosen)
                })

    return {
        "id":          m.id,
        "text":        text,
        "sender":      "me" if m.out else "them",
        "sender_name": "Me" if m.out else sender_name,
        "date":        m.date.isoformat() if m.date else None,
        "media_type":  media_type,
        "file_name":   file_name,
        "file_size":   file_size,
        "reactions":   reactions_data,
        "is_edited":   bool(getattr(m, 'edit_date', None))
    }

@router.get("/messages/{account_id}/{chat_id}")
async def get_messages(account_id: str, chat_id: str, limit: int = 50, offset_id: int = 0):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)

    try:
        search_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        history = await client.get_messages(search_id, limit=limit, offset_id=offset_id)

        # Batch resolution happens automatically in extraction if cache is warm
        messages = []
        for m in history:
            messages.append(await extract_message_data(m))

        return messages
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/media/{account_id}/{chat_id}/{message_id}")
async def get_media(account_id: str, chat_id: str, message_id: int):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)
        
        parsed_chat_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        messages = await client.get_messages(parsed_chat_id, ids=message_id)
        if not messages or not messages.media:
            raise HTTPException(status_code=404, detail="No media in message")

        msg = messages
        content_type = "application/octet-stream"
        file_name = "download"
        
        if isinstance(msg.media, MessageMediaPhoto):
            content_type = "image/jpeg"
            file_name = "photo.jpg"
        elif isinstance(msg.media, MessageMediaDocument) and msg.media.document:
            mime = getattr(msg.media.document, 'mime_type', '') or ''
            content_type = mime if mime else "application/octet-stream"
            for attr in msg.media.document.attributes:
                if type(attr).__name__ == 'DocumentAttributeFilename':
                    file_name = getattr(attr, 'file_name', 'download')

        async def stream_media():
            try:
                async for chunk in client.iter_download(msg.media):
                    yield chunk
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Media stream interrupted: {str(e)}")

        headers = {"Cache-Control": "public, max-age=86400"}
        if "image/jpeg" not in content_type and "image/webp" not in content_type:
            import urllib.parse
            safe_name = urllib.parse.quote(file_name)
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{safe_name}"

        return StreamingResponse(stream_media(), media_type=content_type, headers=headers)

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Error downloading media: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/send-message/{account_id}/{chat_id}")
async def send_telegram_message(
    account_id: str, 
    chat_id: str, 
    message: str = Form(None), 
    file: UploadFile = File(None),
    is_document: bool = Form(False),
    temp_id: str = Form(None),
    current_user: User = Depends(get_current_user)
):
    """Sends a message, optionally with a file. Tracks progress for UI."""
    from app.api.auth_utils import check_plan_limit
    
    account = await TelegramAccount.get(account_id)
    if not account or account.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Account not found or access denied")

    await check_plan_limit(current_user, "access_chat_message")

    try:
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)
        parsed_chat_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        
        msg = None
        if file:
            from app.api.accounts.auth import upload_progresses

            temp_dir = tempfile.gettempdir()
            safe_filename = file.filename or "upload"
            temp_path = os.path.join(temp_dir, f"{uuid.uuid4()}_{safe_filename}")
            
            # Write to temp file
            with open(temp_path, "wb") as f:
                f.write(await file.read())
                
            try:
                async def progress_callback(current, total):
                    if temp_id and total:
                        upload_progresses[temp_id] = 50 + int((current / total) * 50)
                
                msg = await client.send_file(
                    parsed_chat_id, 
                    file=temp_path, 
                    caption=message or "",
                    force_document=is_document,
                    progress_callback=progress_callback if temp_id else None
                )
            finally:
                if temp_id in upload_progresses: del upload_progresses[temp_id]
                if os.path.exists(temp_path): os.remove(temp_path)
        else:
            if not message: raise HTTPException(status_code=400, detail="Either message or file is required")
            msg = await client.send_message(parsed_chat_id, message)
            
        return {
            "status": "success",
            "id": msg.id,
            "date": msg.date.isoformat(),
            "text": msg.message
        }

    except Exception as e:
        logger.error(f"Error sending message: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/edit-message/{account_id}/{chat_id}/{message_id}")
async def edit_telegram_message(
    account_id: str, 
    chat_id: str, 
    message_id: int, 
    message: str = Form(...)
):
    """Edits an existing text message."""
    account = await TelegramAccount.get(account_id)
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)
    try:
        peer = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        msg = await client.edit_message(peer, message_id, message)
        return {"status": "success", "id": msg.id, "text": msg.message}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/mark-read/{account_id}/{chat_id}")
async def mark_chat_as_read(account_id: str, chat_id: str):
    account = await TelegramAccount.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)
        parsed_chat_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        await client.send_read_acknowledge(parsed_chat_id, max_id=0, clear_mentions=True)
        return {"status": "ok", "chat_id": chat_id}
    except Exception as e:
        return {"status": "warning", "detail": str(e)}

@router.get("/new-messages/{account_id}/{chat_id}")
async def get_new_messages(account_id: str, chat_id: str, since_id: int = 0):
    account = await TelegramAccount.get(account_id)
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)

    try:
        parsed_chat_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        history = await client.get_messages(parsed_chat_id, limit=20, min_id=since_id)

        messages = []
        for m in history:
            messages.append(await extract_message_data(m))

        messages.reverse()
        return messages
    except Exception as e:
        logger.error(f"Error polling new messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/react-message/{account_id}/{chat_id}/{message_id}")
async def react_to_message(account_id: str, chat_id: str, message_id: int, reaction: str = Form(None)):
    account = await TelegramAccount.get(account_id)
    client = await get_client(account_id, account.session_string, account.api_id, account.api_hash)

    try:
        parsed_chat_id = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
        reaction_obj = [types.ReactionEmoji(emoticon=reaction)] if reaction else []
        
        await client(functions.messages.SendReactionRequest(
            peer=parsed_chat_id,
            msg_id=message_id,
            reaction=reaction_obj
        ))
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error reacting to message: {e}")
        raise HTTPException(status_code=500, detail=str(e))
