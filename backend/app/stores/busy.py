"""A typed, retryable store contention failure.

``StoreBusy`` means a durable store could not complete an operation because the backing service was
contended or briefly unavailable (a Firestore transaction exhausted its retries, a commit timed
out, the service answered unavailable). Nothing was applied. The text is static: it never carries a
document id, a key or any content. The HTTP layer maps it to a typed 503 STORE_BUSY with a jittered
Retry-After; the paid turn path releases its reservation first.
"""
from __future__ import annotations

# Exception class NAMES (matched along the MRO) that mean "contended or briefly unavailable". Names,
# not imports: the optional google packages are never needed to classify, and the in-process fake's
# own AbortedError is covered too.
_BUSY_NAMES = frozenset({"Aborted", "AbortedError", "DeadlineExceeded", "ServiceUnavailable"})
_RETRIES_PREFIX = "Failed to commit transaction in"


class StoreBusy(Exception):
    def __init__(self) -> None:
        super().__init__("the store is busy")


def is_busy_failure(exc: BaseException) -> bool:
    if isinstance(exc, StoreBusy):
        return True
    if any(cls.__name__ in _BUSY_NAMES for cls in type(exc).__mro__):
        return True
    return isinstance(exc, ValueError) and str(exc).startswith(_RETRIES_PREFIX)
