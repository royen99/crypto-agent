from __future__ import annotations
from typing import Dict, Set
from starlette.websockets import WebSocket

class WSManager:
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}

    async def connect(self, run_id: str, ws: WebSocket):
        await ws.accept()
        self.rooms.setdefault(run_id, set()).add(ws)

    def remove(self, run_id: str, ws: WebSocket):
        room = self.rooms.get(run_id)
        if room and ws in room:
            room.remove(ws)

    async def broadcast(self, run_id: str, message: dict):
        room = self.rooms.get(run_id, set())
        to_drop = []
        for ws in room:
            try:
                await ws.send_json(message)
            except Exception:
                to_drop.append(ws)
        for ws in to_drop:
            room.discard(ws)

ws_manager = WSManager()
