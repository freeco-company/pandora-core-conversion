"""Long-running outbox dispatcher worker.

Replaces the cron-driven `POST /outbox/dispatch` polling with a tight loop
inside a single process. Latency drops from ~30s average (cron resolution
60s + dispatch within window) to <5s (poll interval) for the level-up →
mirror webhook path.

Run as systemd unit:
  ExecStart=/var/www/.../.venv/bin/python -m app.gamification.outbox_worker

Idle behaviour:
  - When `dispatch_pending` returns 0 sent + 0 retried (queue empty + no due
    rows), sleep `IDLE_SECONDS` (default 5).
  - When work was done, immediately re-poll (no sleep) so a burst clears
    quickly without 1-row-per-tick latency.
  - On unhandled exception, log + sleep `ERROR_BACKOFF_SECONDS` (default 30)
    so a transient DB hiccup doesn't tight-loop the CPU.

SIGTERM / SIGINT handler exits cleanly between polls.

Out of scope:
  - Multi-worker fan-out — outbox UPDATE has no row-level lock, so two
    workers would double-deliver. Single-worker is fine for v1 throughput.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from app.db import SessionLocal, engine
from app.gamification import outbox

LOG = logging.getLogger("outbox_worker")

POLL_BATCH_SIZE = int(os.environ.get("OUTBOX_WORKER_BATCH_SIZE", "100"))
IDLE_SECONDS = float(os.environ.get("OUTBOX_WORKER_IDLE_SECONDS", "5"))
ERROR_BACKOFF_SECONDS = float(os.environ.get("OUTBOX_WORKER_ERROR_BACKOFF_SECONDS", "30"))


_should_exit = asyncio.Event()


def _install_signal_handlers() -> None:
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        LOG.info("worker: received shutdown signal, draining...")
        _should_exit.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # windows
            loop.add_signal_handler(sig, _on_signal)


async def _run_loop() -> None:
    LOG.info(
        "outbox_worker started (batch=%d idle=%.1fs error_backoff=%.1fs)",
        POLL_BATCH_SIZE,
        IDLE_SECONDS,
        ERROR_BACKOFF_SECONDS,
    )

    while not _should_exit.is_set():
        try:
            async with SessionLocal() as session, session.begin():
                summary = await outbox.dispatch_pending(session, limit=POLL_BATCH_SIZE)
            had_work = (
                summary["sent"] + summary["retried"] + summary["dead_letter"] > 0
            )
            if had_work:
                LOG.info(
                    "dispatched: sent=%d retried=%d dead_letter=%d",
                    summary["sent"],
                    summary["retried"],
                    summary["dead_letter"],
                )
                # Burst mode — keep going without sleep
                continue
        except Exception as exc:  # noqa: BLE001 — top-level worker resilience
            LOG.exception("worker: dispatch error: %s", exc)
            await _sleep_or_exit(ERROR_BACKOFF_SECONDS)
            continue

        await _sleep_or_exit(IDLE_SECONDS)

    await engine.dispose()
    LOG.info("outbox_worker stopped cleanly")


async def _sleep_or_exit(seconds: float) -> None:
    """Sleep up to `seconds` but break early if shutdown was requested."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(_should_exit.wait(), timeout=seconds)


async def _main_async() -> None:
    _install_signal_handlers()
    await _run_loop()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OUTBOX_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
