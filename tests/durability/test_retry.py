"""Tests for retry module"""

import pytest
from apflow.durability.retry import BackoffStrategy, RetryManager, RetryPolicy


class TestRetryPolicyDefaults:
    def test_defaults(self):
        p = RetryPolicy()
        assert p.max_attempts == 3
        assert p.backoff_strategy == BackoffStrategy.EXPONENTIAL
        assert p.backoff_base_seconds == 1.0
        assert p.backoff_max_seconds == 300.0
        assert p.jitter is True


class TestRetryPolicyValidation:
    def test_max_attempts_zero_raises(self):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)

    def test_max_attempts_101_raises(self):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=101)

    def test_max_attempts_1_valid(self):
        p = RetryPolicy(max_attempts=1)
        assert p.max_attempts == 1

    def test_max_attempts_100_valid(self):
        p = RetryPolicy(max_attempts=100)
        assert p.max_attempts == 100

    def test_backoff_base_too_low(self):
        with pytest.raises(ValueError):
            RetryPolicy(backoff_base_seconds=0.09)

    def test_backoff_base_too_high(self):
        with pytest.raises(ValueError):
            RetryPolicy(backoff_base_seconds=3600.1)

    def test_backoff_max_less_than_base_raises(self):
        with pytest.raises(ValueError):
            RetryPolicy(backoff_base_seconds=10.0, backoff_max_seconds=5.0)


class TestCalculateDelay:
    def test_fixed(self):
        p = RetryPolicy(
            backoff_strategy=BackoffStrategy.FIXED, backoff_base_seconds=2.0, jitter=False
        )
        assert p.calculate_delay(0) == 2.0
        assert p.calculate_delay(5) == 2.0

    def test_exponential(self):
        p = RetryPolicy(
            backoff_strategy=BackoffStrategy.EXPONENTIAL, backoff_base_seconds=1.0, jitter=False
        )
        assert p.calculate_delay(0) == 1.0
        assert p.calculate_delay(1) == 2.0
        assert p.calculate_delay(2) == 4.0
        assert p.calculate_delay(3) == 8.0

    def test_linear(self):
        p = RetryPolicy(
            backoff_strategy=BackoffStrategy.LINEAR, backoff_base_seconds=1.0, jitter=False
        )
        assert p.calculate_delay(0) == 1.0
        assert p.calculate_delay(1) == 2.0
        assert p.calculate_delay(4) == 5.0

    def test_capped_at_max(self):
        p = RetryPolicy(backoff_base_seconds=1.0, backoff_max_seconds=10.0, jitter=False)
        assert p.calculate_delay(20) == 10.0

    def test_negative_attempt_raises(self):
        p = RetryPolicy(jitter=False)
        with pytest.raises(ValueError):
            p.calculate_delay(-1)

    def test_jitter_bounds(self):
        p = RetryPolicy(backoff_base_seconds=4.0, jitter=True)
        delays = [p.calculate_delay(0) for _ in range(100)]
        assert all(0.0 <= d <= 5.0 for d in delays)


class TestRetryManager:
    @pytest.mark.asyncio
    async def test_succeeds_first_attempt(self):
        rm = RetryManager()
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            return {"ok": True}

        result = await rm.execute_with_retry("t1", RetryPolicy(), fn)
        assert call_count == 1
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_succeeds_on_third_attempt(self):
        rm = RetryManager()
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("fail")
            return {"ok": True}

        result = await rm.execute_with_retry(
            "t1", RetryPolicy(max_attempts=3, backoff_base_seconds=0.1, jitter=False), fn
        )
        assert call_count == 3
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_exhausted_raises(self):
        rm = RetryManager()

        async def fn():
            raise RuntimeError("always fail")

        with pytest.raises(RuntimeError, match="always fail"):
            await rm.execute_with_retry(
                "t1", RetryPolicy(max_attempts=2, backoff_base_seconds=0.1, jitter=False), fn
            )

    @pytest.mark.asyncio
    async def test_on_retry_callback(self):
        rm = RetryManager()
        retry_calls: list = []

        async def on_retry(tid, attempt, exc):
            retry_calls.append((tid, attempt))

        async def fn():
            if len(retry_calls) < 2:
                raise RuntimeError("fail")
            return {"ok": True}

        await rm.execute_with_retry(
            "t1",
            RetryPolicy(max_attempts=3, backoff_base_seconds=0.1, jitter=False),
            fn,
            on_retry,
        )
        assert len(retry_calls) == 2
        assert retry_calls[0] == ("t1", 0)
        assert retry_calls[1] == ("t1", 1)
