"""
ws.py — WebSocket connection manager + broadcast helper.

All run lifecycle events are broadcast here. The orchestrator calls
ws_manager.broadcast() with typed payloads; the frontend's useLiveFeed
hook reconnects automatically on drop.

Payload shapes (§10):
  {"type": "run.update", "run": <RunOut dict>}
  {"type": "run.event",  "run_id": int, "event": <RunEventOut dict>}
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.debug("WS client connected; total=%d", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections = [c for c in self._connections if c is not websocket]
        logger.debug("WS client disconnected; total=%d", len(self._connections))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Fan out a JSON payload to all connected clients; drop stale ones."""
        message = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        async with self._lock:
            connections = list(self._connections)
        for ws in connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                self._connections = [c for c in self._connections if c not in dead]


ws_manager = ConnectionManager()
