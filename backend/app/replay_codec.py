"""Serialisation of an idempotent replay record for a durable store (Firestore).

A ``ReplayRecord`` holds the turn's server-authored reply, its workspace, enums, and a
``DiscoveryRef`` (cache key, framing kind, two hashes, expiry). It NEVER holds Maps content: the
orchestrator stores the result with ``discovery=None`` and this codec refuses to encode one that
carries a block. Only JSON-like values and timezone-aware datetimes are produced, because that is
what the document store accepts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .domain import DomainClass, FramingKind, TripWorkspace, TurnWarning
from .orchestrator import ReplayRecord, TurnResult
from .pipeline import DiscoveryRef

__all__ = ["CODEC", "ReplayCodec"]


class ReplayCodec:
    def encode(self, record: ReplayRecord) -> dict[str, Any]:
        result = record.result
        if result.discovery is not None:
            raise ValueError("a replay record never carries Maps content")
        ref = record.ref
        return {
            "result": {
                "reply": result.reply,
                "domain": result.domain.value if result.domain is not None else None,
                "tools": list(result.tools),
                "workspace": result.workspace.model_dump(mode="json") if result.workspace else None,
                "allowance_exhausted": result.allowance_exhausted,
                "warnings": [w.value for w in result.warnings],
            },
            "ref": None if ref is None else {
                "cache_key": ref.cache_key, "framing": ref.framing.value,
                "content_hash": ref.content_hash, "display_hash": ref.display_hash,
                "cache_expires_at": ref.cache_expires_at,
            },
        }

    def decode(self, doc: dict[str, Any]) -> ReplayRecord:
        raw = doc["result"]
        result = TurnResult(
            reply=str(raw["reply"]),
            domain=DomainClass(raw["domain"]) if raw["domain"] is not None else None,
            tools=tuple(raw["tools"]),
            workspace=(TripWorkspace.model_validate(raw["workspace"])
                       if raw["workspace"] is not None else None),
            allowance_exhausted=bool(raw["allowance_exhausted"]),
            discovery=None,
            warnings=tuple(TurnWarning(w) for w in raw["warnings"]),
        )
        ref_doc = doc["ref"]
        ref = None
        if ref_doc is not None:
            expires = ref_doc["cache_expires_at"]
            if not isinstance(expires, datetime):
                raise ValueError("cache_expires_at must be a datetime")
            ref = DiscoveryRef(
                cache_key=str(ref_doc["cache_key"]), framing=FramingKind(ref_doc["framing"]),
                content_hash=str(ref_doc["content_hash"]),
                display_hash=str(ref_doc["display_hash"]), cache_expires_at=expires)
        return ReplayRecord(result, ref)


CODEC = ReplayCodec()
