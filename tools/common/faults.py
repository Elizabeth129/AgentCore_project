"""Deterministic fault injection for reliability testing.

Retry and backoff are only worth anything if they have been watched working, and
waiting for a real throttle is not a test. Setting `FAULT_MODE` on a tool Lambda
makes it fail on demand, the same way every time:

    FAULT_MODE=error     -> a retryable DEPENDENCY_UNAVAILABLE result
    FAULT_MODE=throttle  -> as above, but shaped like a throttle
    FAULT_MODE=timeout   -> sleeps until the Lambda's own timeout kills it
    FAULT_MODE=business  -> a NON-retryable error, to prove retry does not fire
    FAULT_MODE=crash     -> raises, escaping the handler's error boundary, so the
                            invocation fails like an unhandled bug (the 5xx path)

`scripts/inject_fault.py` sets and clears it, and always clears it afterwards.

This is inert unless `FAULT_MODE` is set *and* the stage is dev or test, so the
prod path cannot be switched into failing by an environment variable alone.
"""

from __future__ import annotations

import os
import time

from . import errors
from .config import STAGE

TEST_STAGES = {"dev", "test"}


def active_mode() -> str | None:
    mode = (os.environ.get("FAULT_MODE") or "").strip().lower()
    if not mode or STAGE not in TEST_STAGES:
        return None
    return mode


class InjectedCrash(RuntimeError):
    """Raised by FAULT_MODE=crash. Escapes the handler's error boundary."""


def maybe_crash(tool_name: str | None) -> None:
    """Raise if crash injection is armed. Called outside the error boundary."""
    if active_mode() == "crash":
        raise InjectedCrash(f"Injected unhandled failure in {tool_name or 'tool'}.")


def maybe_fail(tool_name: str | None) -> dict[str, object] | None:
    """Return a fault result, or None to let the real tool run."""
    mode = active_mode()
    if mode is None:
        return None

    if mode == "timeout":
        # Longer than any of our Lambda timeouts: the platform kills the
        # invocation, which is what a real hung dependency looks like.
        time.sleep(900)
        return None

    if mode == "throttle":
        return errors.err(
            errors.DEPENDENCY_UNAVAILABLE,
            "Injected fault: dependency is throttling.",
            retryable=True,
            fault_injected=True,
        )

    if mode == "business":
        return errors.err(
            errors.INVALID_INPUT,
            "Injected fault: a business error, which must not be retried.",
            retryable=False,
            fault_injected=True,
        )

    return errors.err(
        errors.DEPENDENCY_UNAVAILABLE,
        f"Injected fault in {tool_name or 'tool'}.",
        retryable=True,
        fault_injected=True,
    )
