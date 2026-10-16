"""Status bar and the activity stream behind it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from akmarkets.activity import ActivityLog

router = APIRouter(prefix="/api", tags=["activity"])

# How often the stream looks for new events. A poll rather than a push
# because jobs run in worker threads and write to a plain buffer; pushing
# would mean marshalling every emit back onto the event loop for a status
# bar that a human reads at roughly this rate anyway.
POLL_SECONDS = 1.0


def _log(request: Request) -> ActivityLog:
    return cast(ActivityLog, request.app.state.activity)


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    return _log(request).status()


@router.get("/activity")
def activity(request: Request, since: int = 0, limit: int = 200) -> dict[str, Any]:
    log = _log(request)
    events = log.since(since, limit=limit)
    return {
        "events": [event.as_dict() for event in events],
        "last_id": events[-1].id if events else log.last_id(),
        "jobs": [job.as_dict() for job in log.jobs()[:10]],
    }


@router.get("/activity/stream")
async def stream(request: Request, since: int = 0) -> StreamingResponse:
    """Server-sent events, resumable from the last id the client saw.

    Correctness on reconnect is free here: the client sends the highest id
    it has and gets everything above it. No server-side subscription state,
    so a dropped connection costs nothing.
    """
    log = _log(request)
    cursor = since or log.last_id()

    async def generate() -> AsyncIterator[str]:
        nonlocal cursor
        while True:
            if await request.is_disconnected():
                return
            for event in log.since(cursor):
                cursor = event.id
                yield f"data: {json.dumps(event.as_dict())}\n\n"
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Without this, nginx and Cloudflare buffer the stream and the
            # status bar updates in bursts minutes apart, or not at all.
            "X-Accel-Buffering": "no",
        },
    )
