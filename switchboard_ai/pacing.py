"""
switchboard_ai.pacing
─────────────────
Re-chunk coarse text deltas into smooth, evenly paced ones.

Both binaries batch their output (claude.exe measured ~121 chars every
~320 ms), which renders as a stutter. This drains a buffer at an adaptive
character rate so the SSE stream itself is smooth.

Trade-off: steady-state lag of ~target_drain seconds; once upstream ends
the remainder drains within ~final_drain. Set SMOOTH_STREAM=0 to disable.
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from switchboard_ai import config


async def smooth_text_stream(
    source: AsyncGenerator[dict, None],
    tick_seconds: float = config.STREAM_TICK_MS / 1000.0,
    target_drain: float = config.STREAM_TARGET_DRAIN,
    final_drain: float = config.STREAM_FINAL_DRAIN,
    min_cps: float = config.STREAM_MIN_CPS,
    max_cps: float = config.STREAM_MAX_CPS,
) -> AsyncGenerator[dict, None]:
    """
    Yield the same events as `source`, with "text" events re-chunked into
    small slices every `tick_seconds`. Non-text events are held until the
    text ahead of them has drained, so ordering is preserved.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for ev in source:
                await queue.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # surface upstream failures in-band
            await queue.put({"type": "error", "content": str(e)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())

    pending = ""
    held: list[dict] = []
    upstream_done = False

    def absorb(ev) -> None:
        nonlocal pending, upstream_done
        if ev is None:
            upstream_done = True
        elif ev.get("type") == "text":
            pending += ev.get("content", "")
        else:
            held.append(ev)

    try:
        while True:
            while True:
                try:
                    absorb(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            if pending:
                drain = final_drain if upstream_done else target_drain
                cps = min(max_cps, max(min_cps, len(pending) / drain))
                n = max(1, int(cps * tick_seconds))
                yield {"type": "text", "content": pending[:n]}
                pending = pending[n:]

            if not pending and held:
                for ev in held:
                    yield ev
                held.clear()

            if upstream_done and not pending:
                break

            if pending:
                await asyncio.sleep(tick_seconds)
            else:
                # Nothing buffered: block for the next event instead of spinning.
                absorb(await queue.get())

    finally:
        # On client disconnect, propagate cancellation into the provider so
        # it can retire its process.
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
