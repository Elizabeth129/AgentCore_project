"""Retry behaviour: what is repeated, what is not, and how long it waits."""

from __future__ import annotations

import pytest

from retry import MAX_ATTEMPTS, backoff_delay, call_with_retry, is_retryable_result


def _retryable(message: str = "dependency down") -> dict:
    return {"status": "error", "code": "DEPENDENCY_UNAVAILABLE", "message": message, "retryable": True}


def _business() -> dict:
    return {"status": "error", "code": "ORDER_NOT_FOUND", "message": "no", "retryable": False}


def _success() -> dict:
    return {"status": "success", "refund_id": "RFND-1"}


class Recorder:
    """Stands in for time.sleep so tests assert on delays without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def run(results, **kwargs):
    """Call with a scripted sequence of results or exceptions."""
    sequence = list(results)
    sleeper = Recorder()

    def operation():
        item = sequence.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    outcome = call_with_retry(operation, name="tool", sleep=sleeper, **kwargs)
    return outcome, sleeper.delays, sequence


def test_success_is_not_retried():
    outcome, delays, remaining = run([_success(), _success()])
    assert outcome == _success()
    assert delays == []
    assert len(remaining) == 1, "the second result should never have been requested"


def test_business_error_is_not_retried():
    outcome, delays, remaining = run([_business(), _success()])
    assert outcome["code"] == "ORDER_NOT_FOUND"
    assert delays == []
    assert len(remaining) == 1, "a non-retryable error must not trigger a second call"


def test_retryable_error_then_success():
    outcome, delays, _ = run([_retryable(), _success()])
    assert outcome["status"] == "success"
    assert len(delays) == 1, "exactly one backoff between two attempts"


def test_gives_up_after_max_attempts_and_returns_last_result():
    outcome, delays, _ = run([_retryable("1"), _retryable("2"), _retryable("3")])
    assert outcome["message"] == "3", "the caller sees the last real failure"
    assert len(delays) == MAX_ATTEMPTS - 1, "no sleep after the final attempt"


def test_retryable_exception_is_retried():
    outcome, delays, _ = run(
        [ConnectionError("reset"), _success()], retryable_exceptions=(ConnectionError,)
    )
    assert outcome["status"] == "success"
    assert len(delays) == 1


def test_unlisted_exception_propagates():
    with pytest.raises(ValueError):
        run([ValueError("bug")], retryable_exceptions=(ConnectionError,))


def test_exhausted_exceptions_produce_a_structured_error():
    outcome, _, _ = run(
        [ConnectionError("a"), ConnectionError("b"), ConnectionError("c")],
        retryable_exceptions=(ConnectionError,),
    )
    assert outcome["status"] == "error"
    assert outcome["code"] == "TOOL_UNAVAILABLE"
    assert outcome["retryable"] is False, "the agent must not loop on an exhausted tool"


def test_attempts_are_reported():
    seen = []
    run(
        [_retryable(), _success()],
        on_attempt=lambda **kw: seen.append((kw["attempt"], kw["retrying"])),
    )
    assert seen == [(1, True), (2, False)]


def test_final_attempt_is_not_reported_as_retrying():
    """Otherwise the log implies a fourth attempt that never happens."""
    seen = []
    run(
        [_retryable(), _retryable(), _retryable()],
        on_attempt=lambda **kw: seen.append((kw["attempt"], kw["retrying"])),
    )
    assert seen == [(1, True), (2, True), (3, False)]


@pytest.mark.parametrize("attempt,ceiling", [(1, 0.25), (2, 0.5), (3, 1.0), (10, 4.0)])
def test_backoff_grows_exponentially_and_is_capped(attempt, ceiling):
    delays = [backoff_delay(attempt) for _ in range(200)]
    assert all(0.0 <= d <= ceiling for d in delays)
    assert max(delays) > ceiling / 2, "full jitter should span the range, not hug zero"


def test_backoff_is_jittered():
    delays = {backoff_delay(3) for _ in range(50)}
    assert len(delays) > 1, "identical delays would resynchronise concurrent callers"


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"status": "error", "retryable": True}, True),
        ({"status": "error", "retryable": False}, False),
        ({"status": "error"}, False),
        ({"status": "success", "retryable": True}, False),
        ("not a dict", False),
        (None, False),
    ],
)
def test_result_classification(value, expected):
    assert is_retryable_result(value) is expected
