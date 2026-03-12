import asyncio
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from fastapi import WebSocket
from app.models.system_log import SystemLog

class TerminalManager:
    def __init__(self):
        # { user_id: [WebSocket] }
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, ws: WebSocket, user_id: str):
        await ws.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(ws)

    def disconnect(self, ws: WebSocket, user_id: str):
        if user_id in self.active_connections:
            if ws in self.active_connections[user_id]:
                self.active_connections[user_id].remove(ws)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def log_event(self, user_id: str, message: str, account_id: Optional[str] = None, module: str = "system", level: str = "INFO"):
        """
        Optimized: Returns immediately. DB insertion and WS broadcast happen in the background.
        This prevents database latency from "freezing" the main message loop.
        """
        asyncio.create_task(self._process_log(user_id, message, account_id, module, level))

    async def _process_log(self, user_id: str, message: str, account_id: Optional[str] = None, module: str = "system", level: str = "INFO"):
        try:
            # 1. Create log in DB
            log_entry = SystemLog(
                user_id=user_id,
                account_id=account_id,
                module=module,
                level=level,
                message=message,
                timestamp=datetime.now(timezone.utc)
            )
            await log_entry.insert()

            # 2. Broadcast to user's active terminal tabs
            if user_id in self.active_connections:
                payload = {
                    "type": "log_event",
                    "data": {
                        "id": str(log_entry.id),
                        "account_id": account_id,
                        "module": module,
                        "level": level,
                        "message": message,
                        "timestamp": log_entry.timestamp.isoformat()
                    }
                }
                for ws in list(self.active_connections[user_id]):
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        self.disconnect(ws, user_id)
        except Exception as e:
            # Basic fallback to stdout if logging fails (prevent app crash)
            print(f"[log_error] {e}")

terminal_manager = TerminalManager()
