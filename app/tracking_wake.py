import asyncio

from app import operations as ops
from app import tracking_queue as tq

_WAKE_TASK = None


async def _wake_site_once():
    """Advance one small site-side tracking step without carrying file data.

    The request is intentionally best-effort. If the Iranian host/relay is slow,
    the durable queue remains intact and WordPress cron is still the fallback.
    """
    client = ops.WooClient()
    return await asyncio.wait_for(
        client._async_signed_get('vpt_wake', {'source': 'telegram-queue'}),
        timeout=8.0,
    )


async def wake_worker(application):
    """Keep nudging WordPress while a tracking file is waiting.

    This removes the old 1-minute-or-worse WP-Cron startup delay. Every wake is
    tiny; WordPress still pulls the spreadsheet from the bot and advances only a
    bounded batch per request.
    """
    delay = 1.0
    while True:
        try:
            pending, _, _ = tq._queue_stats()
            if pending <= 0:
                delay = 1.0
                await asyncio.sleep(1.0)
                continue

            try:
                await _wake_site_once()
                delay = 0.75
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not hammer the WAF/relay if a wake fails. The queue is
                # durable and the next attempt/WordPress cron can safely resume.
                delay = min(8.0, max(2.0, delay * 1.8))

            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(3.0)


async def startup(application):
    global _WAKE_TASK
    await tq.startup(application)
    if _WAKE_TASK is None or _WAKE_TASK.done():
        _WAKE_TASK = asyncio.create_task(wake_worker(application))
