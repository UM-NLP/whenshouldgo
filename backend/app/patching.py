"""WorkspacePatch validation (docs/agent-conversation-policy.md; the runtime role brief).

A patch is an untrusted model proposal. ``apply_patch`` is the only way it reaches state: it
builds the next workspace on a copy, or raises ``PatchRejected`` and changes nothing.

Rules, all server-owned:
* A model-proposed fact is only ever an INFERENCE, UNCONFIRMED and soft. Hard or confirmed facts
  come from explicit structured user input fields in the request; the request model has none
  today, so a patch can create none.
* Facts a user stated (any non-inference, confirmed or hard fact) and server-owned facts cannot
  be overwritten or removed by the model.
* The patch must name this workspace at its current version, carry no evidence ids,
  dependencies or expiry, and stay inside the count and size limits.
* ``next_action`` and ``status`` come from small allowlists: nothing that would confirm, arm or
  activate a plan, propose a side effect, or run a tool that is not wired in Phase 0.
* A proposed fact's id equals its key, and its key can never match a protected fact's key or the
  server-owned ``planning_area`` (a model fact cannot shadow a user or server fact by key);
  keys stay unique across the workspace.
* Text that re-enters the next prompt (key, scope, string value) is plain: no hidden characters,
  markup, urls, coordinates or addresses; numbers are finite.
* The goal is never overwritten: the raw user message is not workspace state.

Messages are static; a rejection reason is a telemetry enum, never model text.
"""
from __future__ import annotations

import math

from .domain import (
    Confirmation,
    Fact,
    FactSource,
    NextAction,
    TripWorkspace,
    WorkspacePatch,
    WorkspaceStatus,
    evolve,
)
from .telemetry import ValidationReason
from .textsafety import plain_text_problem

MAX_UPSERTS = 10
MAX_REMOVALS = 10
MAX_FACTS = 50
MAX_FACT_VALUE_CHARS = 200
# Created and owned by the server: the model can neither change nor remove it.
SERVER_OWNED_FACT_IDS = frozenset({"planning_area"})
ALLOWED_NEXT_ACTIONS = frozenset({
    NextAction.ASK, NextAction.SEARCH, NextAction.PRESENT_SHORTLIST,
    NextAction.EXPLAIN_CONFLICT, NextAction.STOP_UNSUPPORTED,
})
_EARLY = frozenset({WorkspaceStatus.CAPTURED, WorkspaceStatus.FRAMING, WorkspaceStatus.EXPLORING})
_MODEL_STATUSES = frozenset({WorkspaceStatus.FRAMING, WorkspaceStatus.EXPLORING})
_TERMINAL = frozenset({
    WorkspaceStatus.COMPLETED, WorkspaceStatus.CANCELLED, WorkspaceStatus.EXPIRED,
    WorkspaceStatus.UNSUPPORTED,
})


class PatchRejected(Exception):
    """The proposal was refused whole. ``reason`` is the telemetry enum; the message is static."""

    def __init__(self, reason: ValidationReason) -> None:
        super().__init__("workspace patch rejected")
        self.reason = reason


def planning_area_fact() -> Fact:
    return Fact(
        id="planning_area", key="planning_area", value="Toronto/GTA", source=FactSource.INFERENCE,
        confirmation=Confirmation.UNCONFIRMED, hard=False, scope="trip",
    )


def _protected(fact: Fact) -> bool:
    """A fact only the user (or the server) may change."""
    return (
        fact.id in SERVER_OWNED_FACT_IDS
        or fact.source != FactSource.INFERENCE
        or fact.confirmation == Confirmation.CONFIRMED
        or fact.hard
    )


def _check_header(workspace: TripWorkspace, patch: WorkspacePatch) -> None:
    if patch.workspace_id != workspace.id or patch.base_version != workspace.version:
        raise PatchRejected(ValidationReason.OTHER)  # another workspace, or a stale base
    if workspace.status in _TERMINAL:
        raise PatchRejected(ValidationReason.UNKNOWN_FIELD)
    if patch.next_action not in ALLOWED_NEXT_ACTIONS:
        raise PatchRejected(ValidationReason.UNKNOWN_FIELD)
    if patch.status is not None and (
        patch.status not in _MODEL_STATUSES or workspace.status not in _EARLY
    ):
        raise PatchRejected(ValidationReason.UNKNOWN_FIELD)


def _check_proposed_fact(fact: Fact, existing: dict[str, Fact]) -> None:
    if (
        fact.source != FactSource.INFERENCE
        or fact.confirmation != Confirmation.UNCONFIRMED
        or fact.hard
    ):
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    if fact.evidence_ids or fact.depends_on or fact.expires_at is not None:
        raise PatchRejected(ValidationReason.SCHEMA)
    protected_keys = {f.key for f in existing.values() if _protected(f)} | SERVER_OWNED_FACT_IDS
    if fact.key in protected_keys:  # shadows (or overwrites) a user or server fact
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    if fact.id != fact.key:  # the id is the key: no second name to shadow another fact with
        raise PatchRejected(ValidationReason.SCHEMA)
    if isinstance(fact.value, str):
        if len(fact.value) > MAX_FACT_VALUE_CHARS:
            raise PatchRejected(ValidationReason.OVERSIZE)
        if plain_text_problem(fact.value):
            raise PatchRejected(ValidationReason.SCHEMA)
    elif isinstance(fact.value, float) and not math.isfinite(fact.value):
        raise PatchRejected(ValidationReason.SCHEMA)
    if plain_text_problem(fact.scope) or plain_text_problem(fact.key):
        raise PatchRejected(ValidationReason.SCHEMA)
    current = existing.get(fact.id)
    if fact.id in SERVER_OWNED_FACT_IDS or (current is not None and _protected(current)):
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    for other in existing.values():  # a key already used by a different fact is a shadow too
        if other.key == fact.key and other.id != fact.id:
            raise PatchRejected(
                ValidationReason.HARD_CONSTRAINT if _protected(other) else ValidationReason.SCHEMA)


def apply_patch(workspace: TripWorkspace, patch: WorkspacePatch) -> TripWorkspace:
    """The next workspace, or ``PatchRejected``. ``workspace`` is never modified."""
    _check_header(workspace, patch)
    if len(patch.upsert_facts) > MAX_UPSERTS or len(patch.remove_fact_ids) > MAX_REMOVALS:
        raise PatchRejected(ValidationReason.OVERSIZE)
    upsert_ids = [fact.id for fact in patch.upsert_facts]
    if (
        len(set(upsert_ids)) != len(upsert_ids)
        or len(set(patch.remove_fact_ids)) != len(patch.remove_fact_ids)
        or set(upsert_ids) & set(patch.remove_fact_ids)
    ):
        raise PatchRejected(ValidationReason.SCHEMA)

    existing = {fact.id: fact for fact in workspace.facts}
    for proposed in patch.upsert_facts:
        _check_proposed_fact(proposed, existing)
    for fact_id in patch.remove_fact_ids:
        current = existing.get(fact_id)
        if current is None:
            raise PatchRejected(ValidationReason.UNKNOWN_FIELD)
        if _protected(current):
            raise PatchRejected(ValidationReason.HARD_CONSTRAINT)

    facts = {fact_id: fact for fact_id, fact in existing.items() if fact_id not in patch.remove_fact_ids}
    if "planning_area" not in facts:
        facts["planning_area"] = planning_area_fact()  # the editable Toronto/GTA assumption
    for proposed in patch.upsert_facts:
        facts[proposed.id] = proposed
    if len(facts) > MAX_FACTS:
        raise PatchRejected(ValidationReason.OVERSIZE)
    status = WorkspaceStatus.EXPLORING if workspace.status in _EARLY else workspace.status
    return evolve(workspace, facts=list(facts.values()), status=status, version=workspace.version + 1)
