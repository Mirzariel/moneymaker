"""Dead-man's switch. A dead bot can't tell you it died, so an external service must notice the silence.

Create a free check at https://healthchecks.io (period 5 min, grace 10 min), connect it to Telegram/email,
and put its ping URL in HEALTHCHECK_URL. We only ping while the engine loop is actually completing cycles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)


async def heartbeat_loop(url: str, engine: "Engine", interval: int = 60) -> None:
    max_age = max(engine.cfg.loop_interval_sec * 5, 300)
    started = time.time()
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                now = time.time()
                fresh = engine.last_cycle_at is not None and now - engine.last_cycle_at < max_age
                if fresh:
                    await client.get(url)
                elif now - started > max_age:  # give the first cycle time to finish before crying wolf
                    await client.get(f"{url.rstrip('/')}/fail")
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat ping failed: %s", e)
            await asyncio.sleep(interval)
