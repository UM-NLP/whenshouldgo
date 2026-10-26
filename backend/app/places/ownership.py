"""Which place IDs a caller may ask the paid Places routes about.

``POST /v1/places/details`` and ``/v1/routes/check`` are paid Google calls. Without this check they
would be a generic proxy: any authenticated principal could look up any place ID it can type.
A place ID is accepted only when the caller already has a legitimate reference to it: the
caller's own workspace (selected place, candidate evidence, plan destination), the caller's saved
places and history records (only while history is active), or the caller's CURRENT unexpired
display-cache discovery answers (the pipeline binds those place IDs to the ledger). Anything else
is refused before any Google call and costs nothing. With no check configured nothing is allowed.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

# A source yields the place IDs the principal ``uid`` legitimately references. It must read only
# that principal's own data. A source that raises contributes nothing (fail closed).
PlaceSource = Callable[[str], Iterable[str]]


class PlaceReferences(Protocol):
    def permits(self, uid: str, place_ids: Iterable[str]) -> bool: ...


class NoPlaceReferences:
    """The default: nothing is referenced, so every lookup is refused."""

    def permits(self, uid: str, place_ids: Iterable[str]) -> bool:
        return False


class SourcedPlaceReferences:
    def __init__(self, sources: Iterable[PlaceSource]) -> None:
        self._sources = tuple(sources)

    def permits(self, uid: str, place_ids: Iterable[str]) -> bool:
        wanted = set(place_ids)
        if not wanted:
            return False
        for source in self._sources:
            try:
                wanted.difference_update(source(uid))
            except Exception:  # noqa: BLE001 - an unreadable source grants nothing
                continue
            if not wanted:
                return True
        return not wanted


class AllowAllPlaceReferences:
    """Test double only: permits everything. Never constructed by the composition root."""

    def permits(self, uid: str, place_ids: Iterable[str]) -> bool:
        return True
