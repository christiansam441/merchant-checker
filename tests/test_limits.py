import asyncio

from app.limits import ConcurrencyGate, SlidingWindowRateLimiter


def test_rate_limiter_rejects_requests_over_limit_and_recovers() -> None:
    async def exercise_limiter():
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
        return [
            await limiter.allow("203.0.113.10", now=0),
            await limiter.allow("203.0.113.10", now=1),
            await limiter.allow("203.0.113.10", now=2),
            await limiter.allow("203.0.113.10", now=61),
        ]

    assert asyncio.run(exercise_limiter()) == [True, True, False, True]


def test_concurrency_gate_rejects_and_releases_capacity() -> None:
    async def exercise_gate():
        gate = ConcurrencyGate(limit=1)
        first = await gate.try_acquire()
        second = await gate.try_acquire()
        await gate.release()
        third = await gate.try_acquire()
        return first, second, third

    assert asyncio.run(exercise_gate()) == (True, False, True)
