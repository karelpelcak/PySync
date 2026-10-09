"""Bound JSON request bodies before FastAPI deserializes them."""

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BodyLimit:
    def __init__(self, app: ASGIApp, max_size: int = 16384):
        self.app, self.max_size = app, max_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        if scope["path"].endswith("/files/upload"):
            await self.app(scope, receive, send)  # Streamed and bounded by Store.receive.
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.max_size:
                await JSONResponse({"detail": "Metadata request exceeds 16 KiB"}, status_code=413)(
                    scope, receive, send
                )
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
