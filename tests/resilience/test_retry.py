import random

import pytest

from voice_agent.providers.base import ErrorKind, ProviderError
from voice_agent.resilience.retry import RetryPolicy

TRANSIENT = ProviderError("llm", ErrorKind.SERVER_ERROR, "503")


def policy(**overrides: object) -> RetryPolicy:
    params: dict[str, object] = {
        "max_attempts": 3,
        "base_delay": 0.1,
        "max_delay": 2.0,
        "rng": random.Random(7),
    }
    params.update(overrides)
    return RetryPolicy(**params)  # type: ignore[arg-type]


@pytest.mark.parametrize("attempt_index", range(8))
def test_backoff_is_full_jitter_within_the_exponential_ceiling(attempt_index: int) -> None:
    p = policy()
    ceiling = min(2.0, 0.1 * 2**attempt_index)
    delays = [p.backoff(attempt_index, TRANSIENT) for _ in range(200)]
    assert all(0.0 <= d <= ceiling for d in delays)
    # Full jitter spreads retries out: not every client waits the same amount.
    assert len({round(d, 6) for d in delays}) > 50


def test_backoff_is_deterministic_for_a_seeded_rng() -> None:
    a = [policy(rng=random.Random(1)).backoff(i, TRANSIENT) for i in range(5)]
    b = [policy(rng=random.Random(1)).backoff(i, TRANSIENT) for i in range(5)]
    assert a == b


def test_retry_after_from_the_provider_is_honoured() -> None:
    err = ProviderError("llm", ErrorKind.RATE_LIMITED, "429", status_code=429, retry_after=0.75)
    assert policy().backoff(0, err) == 0.75


def test_retry_after_is_capped_at_max_delay() -> None:
    err = ProviderError("llm", ErrorKind.RATE_LIMITED, "429", status_code=429, retry_after=60)
    assert policy().backoff(0, err) == 2.0


def test_should_retry_respects_max_attempts_and_error_kind() -> None:
    p = policy(max_attempts=3)
    assert p.should_retry(attempt_number=1, error=TRANSIENT)
    assert p.should_retry(attempt_number=2, error=TRANSIENT)
    assert not p.should_retry(attempt_number=3, error=TRANSIENT)
    bad_request = ProviderError("llm", ErrorKind.CLIENT_ERROR, "400", status_code=400)
    assert not p.should_retry(attempt_number=1, error=bad_request)


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"base_delay": -1}, {"max_delay": 0.01, "base_delay": 0.1}],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        policy(**kwargs)
