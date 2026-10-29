"""Live push channel for the web and mobile apps.

Clients open a WebSocket at /ws?token=<jwt>. When bookings or notifications change, the
server pushes a tiny "something changed" event and the client re-fetches through the normal
(authorised) REST endpoints. No row data travels over the socket.

This hub is in-process, so run a single uvicorn worker (or put Redis / Postgres LISTEN-NOTIFY
behind `publish` if you scale out).
"""
import asyncio
import uuid
from typing import Any

from fastapi import WebSocket


class RealtimeHub:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, uuid.UUID] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket, user_id: uuid.UUID) -> None:
        await ws.accept()
        self._clients[ws] = user_id

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    async def _broadcast(self, event: dict[str, Any], only_user: str | None) -> None:
        for ws, uid in list(self._clients.items()):
            if only_user and str(uid) != only_user:
                continue
            try:
                await ws.send_json(event)
            except Exception:
                self.disconnect(ws)

    def publish(self, table: str, user_id: uuid.UUID | str | None = None) -> None:
        """Thread-safe: callable from sync endpoints running in the threadpool.

        table="bookings"      -> sent to every connected client (they just refetch)
        table="notifications" -> sent only to `user_id`
        """
        if self._loop is None or not self._loop.is_running():
            return
        event = {"table": table}
        only = str(user_id) if (table == "notifications" and user_id) else None
        asyncio.run_coroutine_threadsafe(self._broadcast(event, only), self._loop)


hub = RealtimeHub()
