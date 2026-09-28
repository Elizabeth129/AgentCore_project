"""Retry with exponential backoff and jitter, for tool calls only.

The rule this module enforces is that **only transient failures are retried**.
A tool that says "this order does not exist" or "that amount is over the limit"
will say the same thing however many times it is asked, so repeating the call
wastes time and, for anything that writes, risks doing the work twice. The tools
make that distinction explicit with a `retryable` flag, and the
classification here is the only place that reads it.

Backoff uses **full jitter** — a delay drawn uniformly from `[0, cap]` rather
than a fixed `base * 2^n`. When several concurrent sessions hit the same
throttled dependency, equal delays would send them back in lockstep and throttle
them again; jitter spreads them out.

Retrying a refund is only safe because the idempotency key is stable across
attempts (`agent/idempotency.py`): every attempt carries the same key, so at
most one refund is ever recorded no matter how many attempts are made.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 0.25
MAX_DELAY_SECONDS = 4.0


def backoff_delay(attempt: int, *, base: float = BASE_DELAY_SECONDS, cap: float = MAX_DELAY_SECONDS) -> float:
    """Full-jitter delay before `attempt` (1-based: the wait after attempt 1)."""
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


def is_retryable_result(result: Any) -> bool:
    """Whether a structured tool result asks to be retried.

    Anything that is not a recognisable tool result is treated as *not*
    retryable: an unreadable response is far more likely to be a bug than a
    blip, and retrying it just triples the damage.
    """
    if not isinstance(result, dict):
        return False
    if result.get("status") != "error":
        return False
    return bool(result.get("retryable"))


def call_with_retry(
    operation: Callable[[], Any],
    *,
    name: str,
    max_attempts: int = MAX_ATTEMPTS,
    is_retryable: Callable[[Any], bool] = is_retryable_result,
    retryable_exceptions: tuple[type[BaseException], ...] = (),
    on_attempt: Callable[..., None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Run `operation`, retrying transient failures with backoff.

    Returns the last result. A retryable failure that survives every attempt is
    returned as-is rather than raised, so the agent can explain it to the
    customer instead of the turn collapsing.

    `retryable_exceptions` covers transport-level faults, where no structured
    result comes back at all. Any other exception propagates immediately —
    a bug should surface, not be retried.
    """
    last_result: Any = None

    for attempt in range(1, max_attempts + 1):
        try:
            last_result = operation()
            retry_wanted = is_retryable(last_result)
            failure: BaseException | None = None
        except retryable_exceptions as exc:  # type: ignore[misc]
            last_result = None
            retry_wanted = True
            failure = exc

        will_retry = retry_wanted and attempt < max_attempts

        if on_attempt is not None:
            on_attempt(name=name, attempt=attempt, retrying=will_retry, error=failure)

        if not retry_wanted:
            return last_result

        if not will_retry:
            break

        sleep(backoff_delay(attempt))

    if last_result is not None:
        return last_result

    return {
        "status": "error",
        "code": "TOOL_UNAVAILABLE",
        "message": f"{name} did not respond after {max_attempts} attempts.",
        "retryable": False,
        "attempts": max_attempts,
    }
