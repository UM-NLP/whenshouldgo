"""GroundedAnswerValidator: accept or reject the WHOLE block, never edit it.

There is deliberately one public method and it returns nothing: there is no way to redact,
trim, reorder or repair an answer through this class. Provenance is checked against the run
ledger the client wrote from the tool's own payload (ids plus two hashes), so text, links,
titles or places that a model or another layer made up cannot pass, and an answer cannot be
replayed in another run or for another principal.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .. import telemetry
from ..domain import MAX_MAPS_CACHE, ContentClass, EvidenceStorageClass, RuntimeTTLs
from ._validated import ValidatedAnswer, mint_validated
from .client import ToolRun
from .contracts import (
    GROUNDED_TEXT_MAX_CHARS,
    MAX_PLACES,
    MAX_TITLE_CHARS,
    GroundedAnswer,
    cited_flags,
    content_hash,
    display_hash,
)
from .errors import GroundedAnswerRejected, RejectionReason
from .text import short_field_problem, summary_problem
from .uris import check_attribution_url, check_maps_link

_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")
_TELEMETRY_REASON = {
    RejectionReason.FORMAT: telemetry.ValidationReason.SCHEMA,
    RejectionReason.CLASS: telemetry.ValidationReason.STORAGE_CLASS,
    RejectionReason.STORAGE: telemetry.ValidationReason.STORAGE_CLASS,
    RejectionReason.SIZE: telemetry.ValidationReason.OVERSIZE,
    RejectionReason.HASH: telemetry.ValidationReason.SCHEMA,
    RejectionReason.URI: telemetry.ValidationReason.OTHER,
    RejectionReason.CACHE_EXPIRY: telemetry.ValidationReason.STALE_EVIDENCE,
    RejectionReason.PROVENANCE: telemetry.ValidationReason.TOOL_NOT_ALLOWED,
}


class GroundedAnswerValidator:
    def __init__(
        self,
        ttls: RuntimeTTLs,
        *,
        max_text_chars: int = GROUNDED_TEXT_MAX_CHARS,
        sink: telemetry.TraceSink | None = None,
    ) -> None:
        self._ttls = ttls
        self._max_text = max_text_chars
        self._sink = sink

    def validate(
        self, answer: GroundedAnswer, run: ToolRun, now: datetime, *, principal_id: str
    ) -> ValidatedAnswer:
        """Accept (returning the only object the renderer and cache take) or reject the whole
        answer. The wrapper holds the SAME answer, unchanged; nothing here can edit it."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        reason = self._reason(answer, run, now, principal_id)
        if reason is not None:
            telemetry.trace(
                telemetry.ValidationRejection, self._sink,
                trace_id=telemetry.current_trace_id(),
                reason=_TELEMETRY_REASON[reason], phase=telemetry.Phase.TOOL,
            )
            raise GroundedAnswerRejected(reason)
        return mint_validated(answer, principal_id)

    # -- checks, in a fixed order; each returns a reason or None ----------------------------------
    def _reason(self, answer: Any, run: ToolRun, now: datetime, principal_id: str) -> RejectionReason | None:
        values, ok = _read(answer)
        if not ok:
            return RejectionReason.FORMAT
        text, places = values["text"], values["places"]
        if values["content_class"] != ContentClass.MAPS_GROUNDED:
            return RejectionReason.CLASS
        if values["storage_class"] != EvidenceStorageClass.DISPLAY_CACHE:
            return RejectionReason.STORAGE
        if not 1 <= len(text) <= min(self._max_text, GROUNDED_TEXT_MAX_CHARS):
            return RejectionReason.SIZE
        if not 1 <= len(places) <= MAX_PLACES:
            return RejectionReason.SIZE
        fields = _place_fields(places)
        if fields is None:
            return RejectionReason.FORMAT
        if any(not 1 <= len(title) <= MAX_TITLE_CHARS for _, title, _, _ in fields):
            return RejectionReason.SIZE
        if summary_problem(text) or any(
            short_field_problem(title) or not _PLACE_ID.fullmatch(pid) for pid, title, _, _ in fields
        ):
            return RejectionReason.FORMAT
        for _, _, attribution_url, place_url in fields:
            try:
                check_attribution_url(attribution_url)
                check_maps_link(place_url)
            except ValueError:
                return RejectionReason.URI
        if content_hash(text) != values["content_hash"] or display_hash(text, fields) != values["display_hash"]:
            return RejectionReason.HASH
        # Best effort (TODO(verify-live)): flags must match the in-range brackets; an out-of-range
        # bracket such as a year is ignored and never a reason to reject.
        if [p.cited for p in places] != cited_flags(text, len(places)):
            return RejectionReason.FORMAT
        cache = self._cache_reason(values, now)
        if cache is not None:
            return cache
        return _provenance_reason(values, places, run, principal_id)

    def _cache_reason(self, values: dict[str, Any], now: datetime) -> RejectionReason | None:
        retrieved, expires = values["retrieved_at"], values["cache_expires_at"]
        if not (isinstance(retrieved, datetime) and isinstance(expires, datetime)):
            return RejectionReason.FORMAT
        if retrieved.tzinfo is None or expires.tzinfo is None:
            return RejectionReason.FORMAT
        limit = min(timedelta(hours=self._ttls.grounded_cache_hours), MAX_MAPS_CACHE)
        if not retrieved < expires <= retrieved + limit:
            return RejectionReason.CACHE_EXPIRY
        if retrieved > now or expires <= now:
            return RejectionReason.CACHE_EXPIRY  # from the future, or already expired
        return None


def _read(answer: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(answer, GroundedAnswer):
        return {}, False
    names = ("text", "content_hash", "display_hash", "places", "tool_call_id", "run_id", "retrieved_at",
             "cache_expires_at", "content_class", "storage_class")
    try:
        values = {name: getattr(answer, name) for name in names}
        ok = isinstance(values["text"], str) and isinstance(values["places"], tuple)
    except AttributeError:
        return {}, False
    return values, ok


def _place_fields(places: tuple[Any, ...]) -> list[tuple[str, str, str, str]] | None:
    fields = []
    try:
        for p in places:
            row = (p.place_id, p.attribution_title, p.attribution_url, p.place_url)
            if not all(isinstance(v, str) for v in row) or not isinstance(p.cited, bool):
                return None
            fields.append(row)
    except AttributeError:
        return None
    return fields


def _provenance_reason(
    values: dict[str, Any], places: tuple[Any, ...], run: ToolRun, principal_id: str
) -> RejectionReason | None:
    if not isinstance(run, ToolRun) or run.principal_id != principal_id or values["run_id"] != run.run_id:
        return RejectionReason.PROVENANCE  # another tenant's or another run's answer
    record = run.record_for(values["tool_call_id"])
    if (
        record is None or record.display_hash is None
        or record.text_hash != values["content_hash"] or record.display_hash != values["display_hash"]
    ):
        return RejectionReason.PROVENANCE
    for place in places:
        # Every place must come from the SAME call as the summary and be one the tool returned.
        if place.tool_call_id != values["tool_call_id"] or place.place_id not in record.place_ids:
            return RejectionReason.PROVENANCE
    return None
