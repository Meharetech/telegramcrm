import asyncio
import logging
from typing import Dict, List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.client_cache import get_client
from app.models import TelegramAccount
from telethon import events
import json

logger = logging.getLogger(__name__)

router = APIRouter()

# Store active websocket connections per account
# { account_id: [WebSocket, WebSocket, ...] }
_ws_connections: Dict[str, List[WebSocket]] = {}
_ws_handlers_attached: Dict[str, bool] = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # New: Global notification connections indexed by user_id
        self.user_notifications: Dict[str, List[WebSocket]] = {}

    async def connect(self, ws: WebSocket, account_id: str):
        await ws.accept()
        if account_id not in self.active_connections:
            self.active_connections[account_id] = []
        self.active_connections[account_id].append(ws)

    async def connect_user(self, ws: WebSocket, user_id: str):
        await ws.accept()
        if user_id not in self.user_notifications:
            self.user_notifications[user_id] = []
        self.user_notifications[user_id].append(ws)

    def disconnect(self, ws: WebSocket, account_id: str):
        if account_id in self.active_connections:
            if ws in self.active_connections[account_id]:
                self.active_connections[account_id].remove(ws)
            if not self.active_connections[account_id]:
                del self.active_connections[account_id]

    def disconnect_user(self, ws: WebSocket, user_id: str):
        if user_id in self.user_notifications:
            if ws in self.user_notifications[user_id]:
                self.user_notifications[user_id].remove(ws)
            if not self.user_notifications[user_id]:
                del self.user_notifications[user_id]

    async def send_to_account(self, account_id: str, message: dict):
        if account_id in self.active_connections:
            for ws in list(self.active_connections[account_id]):
                try:
                    await ws.send_json(message)
                except Exception as e:
                    logger.warning(f"Error sending to WS for {account_id}: {e}")
                    self.disconnect(ws, account_id)

    async def send_to_user(self, user_id: str, message: dict):
        """Send global notification to all tabs/devices of a specific user."""
        if user_id in self.user_notifications:
            for ws in list(self.user_notifications[user_id]):
                try:
                    await ws.send_json(message)
                except Exception as e:
                    logger.warning(f"Error sending notification to user {user_id}: {e}")
                    self.disconnect_user(ws, user_id)

    async def broadcast(self, message: dict):
        """Broadcast a message to ALL connected users across the platform."""
        for user_id, connections in list(self.user_notifications.items()):
            for ws in list(connections):
                try:
                    await ws.send_json(message)
                except Exception as e:
                    logger.warning(f"Error broadcasting to user {user_id}: {e}")
                    self.disconnect_user(ws, user_id)

manager = ConnectionManager()


async def extract_message_data_sync(m):
    """Lighter version of extract_message_data from messages.py for WS sending"""
    sender_name = "Unknown"
    
    media_type = None
    file_name = None
    file_size = None
    
    # We do a quick extraction to avoid blocking the event loop or needing to fetch full info
    text = m.text or ""
    if m.media:
        media_type = "document" # generic fallback for ws real-time

    return {
        "id":          m.id,
        "text":        text,
        "sender":      "me" if m.out else "them",
        "sender_name": "Me" if m.out else sender_name,
        "date":        m.date.isoformat() if m.date else None,
        "media_type":  media_type,
        "file_name":   file_name,
        "file_size":   file_size,
        "chat_id": str(m.chat_id)
    }

async def attach_ws_handlers(client, account_id: str):
    """Attach purely WebSocket-related Telethon handlers if not already attached."""
    if _ws_handlers_attached.get(account_id):
        return

    @client.on(events.NewMessage)
    async def ws_new_message_handler(event):
        # Don't do heavy processing if nobody is connected to this account
        if account_id not in manager.active_connections:
            return

        msg_data = await extract_message_data_sync(event.message)
        
        await manager.send_to_account(account_id, {
            "type": "new_message",
            "data": msg_data
        })

    @client.on(events.MessageRead)
    async def ws_message_read_handler(event):
        if account_id not in manager.active_connections:
            return
            
        await manager.send_to_account(account_id, {
            "type": "message_read",
            "data": {
                "chat_id": str(event.chat_id),
                "max_id": event.max_id
            }
        })

    _ws_handlers_attached[account_id] = True
    logger.info(f"[ws] Handlers attached for {account_id}")

@router.websocket("/ws/{account_id}")
async def websocket_endpoint(websocket: WebSocket, account_id: str, token: str = None):
    # FIRST: Accept the connection immediately to satisfy the browser handshake
    await websocket.accept()
    
    from app.api.auth_utils import get_user_from_token
    from bson import ObjectId

    if not token:
        await websocket.send_json({"type": "error", "message": "Missing authentication token"})
        await websocket.close(code=1008)
        return

    user = await get_user_from_token(token)
    if not user:
        await websocket.send_json({"type": "error", "message": "Invalid token"})
        await websocket.close(code=1008)
        return

    # Verify account ownership before adding to pool
    account = None
    try:
        account = await TelegramAccount.find_one(
            TelegramAccount.id == ObjectId(account_id),
            TelegramAccount.user_id == str(user.id)
        )
    except Exception:
        pass

    if not account or not account.session_string:
        await websocket.send_json({"type": "error", "message": "Unauthorized account access"})
        await websocket.close(code=1008)
        return

    # ── Auth passed — now register in manager ─────────────────────────────
    # Note: connect no longer calls accept() because we did it above
    if account_id not in manager.active_connections:
        manager.active_connections[account_id] = []
    manager.active_connections[account_id].append(websocket)
    
    try:
        # Ensure client is connected and handlers are attached
        try:
            client = await get_client(account_id, account.session_string, account.api_id, account.api_hash, device_model=getattr(account, 'device_model', 'Telegram Android'))
            await attach_ws_handlers(client, account_id)
        except Exception as e:
            logger.error(f"[ws] Error initializing client for {account_id}: {e}")
            await websocket.send_json({"type": "error", "message": "Failed to connect telegram client"})
            await websocket.close(code=1011)
            manager.disconnect(websocket, account_id)
            return

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass

    except WebSocketDisconnect:
        manager.disconnect(websocket, account_id)
        logger.info(f"[ws] Disconnected {account_id}")
    except Exception as e:
        logger.error(f"[ws] Unexpected error: {e}")
        manager.disconnect(websocket, account_id)


@router.websocket("/ws/notifications/{token}")
async def global_notification_endpoint(websocket: WebSocket, token: str):
    """Real-time global notifications (reminders, system alerts, etc) for a user."""
    # FIRST: Accept connection immediately
    await websocket.accept()
    
    from app.api.auth_utils import get_user_from_token
    
    user = await get_user_from_token(token)
    if not user:
        await websocket.send_json({"type": "error", "message": "Invalid token"})
        await websocket.close(code=1008)
        return

    user_id = str(user.id)
    # Note: connect_user no longer calls accept() because we did it above
    if user_id not in manager.user_notifications:
        manager.user_notifications[user_id] = []
    manager.user_notifications[user_id].append(websocket)
    
    logger.info(f"[ws] User {user_id} connected to global notifications")
    
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect_user(websocket, user_id)
        logger.info(f"[ws] User {user_id} disconnected from global notifications")
    except Exception as e:
        logger.error(f"[ws] Global WS unexpected error: {e}")
        manager.disconnect_user(websocket, user_id)

