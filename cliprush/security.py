"""Bound raw inbound webhook payloads before buffering into memory."""
from fastapi import HTTPException, Request


async def read_limited_body(request: Request, byte_limit: int) -> bytes:
    value = request.headers.get("content-length", "").strip()
    if value:
        try:
            if int(value) > byte_limit:
                raise HTTPException(413, "Webhook payload too large")
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length")
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > byte_limit:
            raise HTTPException(413, "Webhook payload too large")
        chunks.append(chunk)
    return b"".join(chunks)
