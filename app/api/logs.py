from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from typing import List
from app.models import User, SystemLog
from app.api.auth_utils import get_current_user, get_user_from_token
from app.services.terminal_service import terminal_manager

router = APIRouter()

@router.get("/recent")
async def get_recent_logs(limit: int = 100, current_user: User = Depends(get_current_user)):
    logs = await SystemLog.find(
        SystemLog.user_id == str(current_user.id)
    ).sort(-SystemLog.timestamp).limit(limit).to_list()
    # Return in chronological order for terminal
    return logs[::-1]

@router.delete("/clear")
async def clear_logs(current_user: User = Depends(get_current_user)):
    await SystemLog.find(SystemLog.user_id == str(current_user.id)).delete()
    return {"status": "success", "message": "Logs cleared"}

@router.websocket("/ws")
async def terminal_ws_endpoint(websocket: WebSocket, token: str = None):
    if not token:
        await websocket.accept()
        await websocket.close(code=1008)
        return

    user = await get_user_from_token(token)
    if not user:
        await websocket.accept()
        await websocket.close(code=1008)
        return

    user_id = str(user.id)
    await terminal_manager.connect(websocket, user_id)
    
    try:
        while True:
            # We don't expect messages from client yet, but keep socket open
            await websocket.receive_text()
    except WebSocketDisconnect:
        terminal_manager.disconnect(websocket, user_id)
    except Exception:
        terminal_manager.disconnect(websocket, user_id)
