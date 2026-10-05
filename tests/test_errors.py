import pytest

from voice_agent.providers.base import ErrorKind, ProviderError

TRANSIENT = [
    ErrorKind.TIMEOUT,
    ErrorKind.CONNECTION,
    ErrorKind.RATE_LIMITED,
    ErrorKind.SERVER_ERROR,
    ErrorKind.MALFORMED,
]


@pytest.mark.parametrize("kind", TRANSIENT)
def test_transient_errors_are_retried_and_count_against_the_breaker(kind: ErrorKind) -> None:
    err = ProviderError("stt", kind, "boom")
    assert err.retryable
    assert err.counts_as_failure


@pytest.mark.parametrize("kind", [ErrorKind.CLIENT_ERROR, ErrorKind.AUTH])
def test_request_errors_are_not_retried_and_do_not_trip_the_breaker(kind: ErrorKind) -> None:
    err = ProviderError("llm", kind, "bad request", status_code=400)
    assert not err.retryable
    assert not err.counts_as_failure


@pytest.mark.parametrize("kind", [ErrorKind.CIRCUIT_OPEN, ErrorKind.DEADLINE])
def test_fail_fast_errors_are_not_retried(kind: ErrorKind) -> None:
    err = ProviderError("tts", kind, "nope")
    assert not err.retryable
    assert not err.counts_as_failure


def test_error_message_names_stage_and_kind() -> None:
    err = ProviderError("tts", ErrorKind.RATE_LIMITED, "slow down", status_code=429, retry_after=1.5)
    assert "tts" in str(err)
    assert "rate_limited" in str(err)
    assert err.retry_after == 1.5
