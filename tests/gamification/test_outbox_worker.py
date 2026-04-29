"""Tests for the long-running outbox worker.

The worker is fundamentally a `while not exit: dispatch_pending` loop. We
test the loop control flow with monkeypatched dispatch + a stub session
maker, not the underlying dispatcher (which has its own coverage in
test_outbox.py).
"""

from __future__ import annotations

import asyncio

import pytest

from app.gamification import outbox_worker


@pytest.fixture(autouse=True)
def _reset_exit_flag():
    outbox_worker._should_exit.clear()
    yield
    outbox_worker._should_exit.clear()


class _StubSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    async def __aenter__begin(self):
        return None


def _stub_session_maker_factory():
    class _Maker:
        def __call__(self):
            return _StubSession()

    return _Maker()


async def test_worker_calls_dispatch_and_exits_on_shutdown_signal(monkeypatch):
    calls: list[int] = []

    async def fake_dispatch(session, *, limit):
        calls.append(limit)
        outbox_worker._should_exit.set()  # exit after first call
        return {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}

    monkeypatch.setattr(outbox_worker.outbox, "dispatch_pending", fake_dispatch)
    monkeypatch.setattr(outbox_worker, "SessionLocal", lambda: _StubSession())

    await outbox_worker._run_loop()

    assert len(calls) == 1
    assert calls[0] == outbox_worker.POLL_BATCH_SIZE


async def test_worker_loops_burst_when_work_present(monkeypatch):
    """When work was sent, loop immediately re-polls (no idle sleep)."""
    call_count = 0

    async def fake_dispatch(session, *, limit):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            outbox_worker._should_exit.set()
            return {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}
        return {"sent": 5, "retried": 0, "dead_letter": 0, "skipped": 0}

    sleeps: list[float] = []

    async def fake_sleep_or_exit(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(outbox_worker.outbox, "dispatch_pending", fake_dispatch)
    monkeypatch.setattr(outbox_worker, "SessionLocal", lambda: _StubSession())
    monkeypatch.setattr(outbox_worker, "_sleep_or_exit", fake_sleep_or_exit)

    await outbox_worker._run_loop()

    # 3 dispatch calls; first 2 had work (continue, no sleep), 3rd was empty (1 sleep)
    assert call_count == 3
    assert len(sleeps) == 1
    assert sleeps[0] == outbox_worker.IDLE_SECONDS


async def test_worker_idle_sleeps_when_no_work(monkeypatch):
    iterations = 0

    async def fake_dispatch(session, *, limit):
        nonlocal iterations
        iterations += 1
        return {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}

    sleeps: list[float] = []

    async def fake_sleep_or_exit(seconds: float):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            outbox_worker._should_exit.set()

    monkeypatch.setattr(outbox_worker.outbox, "dispatch_pending", fake_dispatch)
    monkeypatch.setattr(outbox_worker, "SessionLocal", lambda: _StubSession())
    monkeypatch.setattr(outbox_worker, "_sleep_or_exit", fake_sleep_or_exit)

    await outbox_worker._run_loop()

    assert iterations >= 3
    assert all(s == outbox_worker.IDLE_SECONDS for s in sleeps)


async def test_worker_backs_off_on_exception(monkeypatch):
    """Exception in dispatch logs + sleeps ERROR_BACKOFF_SECONDS, then continues."""
    call_count = 0

    async def fake_dispatch(session, *, limit):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated DB hiccup")
        outbox_worker._should_exit.set()
        return {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}

    sleeps: list[float] = []

    async def fake_sleep_or_exit(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(outbox_worker.outbox, "dispatch_pending", fake_dispatch)
    monkeypatch.setattr(outbox_worker, "SessionLocal", lambda: _StubSession())
    monkeypatch.setattr(outbox_worker, "_sleep_or_exit", fake_sleep_or_exit)

    await outbox_worker._run_loop()

    # First iter exception → ERROR_BACKOFF sleep; second iter empty → IDLE sleep + exit set
    assert call_count == 2
    assert sleeps[0] == outbox_worker.ERROR_BACKOFF_SECONDS


async def test_sleep_or_exit_returns_early_on_shutdown():
    """The shutdown sentinel cuts long sleeps short so SIGTERM is responsive."""
    outbox_worker._should_exit.set()

    started = asyncio.get_running_loop().time()
    await outbox_worker._sleep_or_exit(60.0)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5  # returned immediately, didn't actually sleep 60s


async def test_sleep_or_exit_full_duration_when_not_shutdown():
    started = asyncio.get_running_loop().time()
    await outbox_worker._sleep_or_exit(0.05)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed >= 0.04  # slept ~the full duration
