"""Exception hierarchy for the download engine.

The distinction that matters to the retry loop is:
  * RetryableError  - reconnecting and asking for the same byte range may work
  * FatalError      - retrying the same request cannot succeed (bad auth, 404,
                      server ignored Range, content changed, disk full, ...)
"""

from __future__ import annotations


class PdmError(Exception):
    """Base class for all engine errors."""


class FatalError(PdmError):
    """An error that will not be fixed by retrying the same request."""


class RetryableError(PdmError):
    """A transient error; the worker may reconnect and continue from its offset."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Cancelled(PdmError):
    """The user (or the engine) asked the download to stop."""


class ProbeError(FatalError):
    """The initial metadata request failed.

    ``transient`` marks failures worth another attempt later (timeout, refused
    connection, 429, 5xx) as opposed to permanent ones (bad URL, 404).
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class AuthRequired(FatalError):
    """The server requires credentials we do not have."""


class HTTPStatusError(FatalError):
    def __init__(self, status: int, reason: str, url: str) -> None:
        super().__init__(f"HTTP {status} {reason} for {url}")
        self.status = status
        self.reason = reason
        self.url = url


class RangeNotSupported(FatalError):
    """The server ignored a Range request during a multi-segment download."""


class ContentChanged(FatalError):
    """ETag / Last-Modified / total size changed between probe and download."""


class IncompleteTransfer(RetryableError):
    """The connection closed before the requested byte range was complete."""


class ProtocolError(FatalError):
    """The server returned a syntactically valid but contradictory response."""


class DiskError(FatalError):
    """Local filesystem failure (permissions, full disk, ...)."""


class JournalMismatch(FatalError):
    """An existing partial download cannot be resumed safely."""


class ChecksumMismatch(FatalError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"SHA-256 mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual


# Statuses that say "come back later" rather than "this request is wrong".
TRANSIENT_STATUSES = frozenset({408, 425, 429})


def is_retryable(exc: BaseException) -> bool:
    """Whether a *later* attempt at the queue level could succeed.

    The engine already retries ``RetryableError`` inline.  This predicate is
    for the outer layer (scheduled queue retry), which sees the error after
    the engine gave up.  Permanent failures -- bad credentials, 404, checksum
    mismatch, server ignored Range, disk errors -- must not loop forever.
    """
    if isinstance(exc, Cancelled):
        return False
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, HTTPStatusError):
        return exc.status in TRANSIENT_STATUSES or exc.status >= 500
    if isinstance(exc, ProbeError):
        return exc.transient
    return False
