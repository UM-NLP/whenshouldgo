"""WorkspacePatch validation (docs/agent-conversation-policy.md; the runtime role brief).

A patch is an untrusted model proposal. ``apply_patch`` / ``apply_patch_checked`` are the only
way it reaches state: they build the next workspace on a copy, or raise ``PatchRejected`` and
change nothing.

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
  keys stay unique across the workspace. Ids and keys are compared NFKC-normalised and
  case-folded, so "Budget" and "budget" are one key.
* STRUCTURAL violations (protected facts, key shadowing, hard or confirmed facts, counts and
  sizes, a stale base) reject the whole patch. A fact whose TEXT is not plain (hidden characters,
  markup, links, coordinates, addresses; see ``textsafety``) or whose number is not finite is
  DROPPED on its own and counted, so one odd fact does not cost the user the rest of the
  patch; the caller surfaces a typed warning.
* The goal is never overwritten: the raw user message is not workspace state.

Messages are static; a rejection reason is a telemetry enum, never model text.
"""
from __future__ import annotations

import math
import unicodedata

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


def _n(text: str) -> str:
    """The comparison form of an id or key: NFKC and case-folded."""
    return unicodedata.normalize("NFKC", text).casefold()


def planning_area_fact() -> Fact:
    return Fact(
        id="planning_area", key="planning_area", value="Toronto/GTA", source=FactSource.INFERENCE,
        confirmation=Confirmation.UNCONFIRMED, hard=False, scope="trip",
    )


def _protected(fact: Fact) -> bool:
    """A fact only the user (or the server) may change."""
    return (
        _n(fact.id) in SERVER_OWNED_FACT_IDS
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


def _check_proposed_fact(fact: Fact, existing: dict[str, Fact]) -> bool:
    """Structural checks raise ``PatchRejected``; returns False when only the fact's TEXT is
    unacceptable (the caller drops that one fact)."""
    if (
        fact.source != FactSource.INFERENCE
        or fact.confirmation != Confirmation.UNCONFIRMED
        or fact.hard
    ):
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    if fact.evidence_ids or fact.depends_on or fact.expires_at is not None:
        raise PatchRejected(ValidationReason.SCHEMA)
    key = _n(fact.key)
    protected_keys = {_n(f.key) for f in existing.values() if _protected(f)} | SERVER_OWNED_FACT_IDS
    if key in protected_keys:  # shadows (or overwrites) a user or server fact
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    if _n(fact.id) != key:  # the id is the key: no second name to shadow another fact with
        raise PatchRejected(ValidationReason.SCHEMA)
    if isinstance(fact.value, str) and len(fact.value) > MAX_FACT_VALUE_CHARS:
        raise PatchRejected(ValidationReason.OVERSIZE)
    current = existing.get(_n(fact.id))
    if _n(fact.id) in SERVER_OWNED_FACT_IDS or (current is not None and _protected(current)):
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    for other in existing.values():  # a key already used by a different fact is a shadow too
        if _n(other.key) == key and _n(other.id) != _n(fact.id):
            raise PatchRejected(
                ValidationReason.HARD_CONSTRAINT if _protected(other) else ValidationReason.SCHEMA)
    # Only text problems are left: they cost this fact, not the patch.
    if isinstance(fact.value, float) and not math.isfinite(fact.value):
        return False
    if isinstance(fact.value, str) and plain_text_problem(fact.value, allow_joiners=True):
        return False
    return not (plain_text_problem(fact.scope) or plain_text_problem(fact.key))


def apply_patch_checked(workspace: TripWorkspace, patch: WorkspacePatch) -> tuple[TripWorkspace, int]:
    """The next workspace and the number of proposed facts dropped for unacceptable text, or
    ``PatchRejected``. ``workspace`` is never modified."""
    _check_header(workspace, patch)
    if len(patch.upsert_facts) > MAX_UPSERTS or len(patch.remove_fact_ids) > MAX_REMOVALS:
        raise PatchRejected(ValidationReason.OVERSIZE)
    upsert_ids = [_n(fact.id) for fact in patch.upsert_facts]
    remove_ids = [_n(fact_id) for fact_id in patch.remove_fact_ids]
    if (
        len(set(upsert_ids)) != len(upsert_ids)
        or len(set(remove_ids)) != len(remove_ids)
        or set(upsert_ids) & set(remove_ids)
    ):
        raise PatchRejected(ValidationReason.SCHEMA)

    existing = {_n(fact.id): fact for fact in workspace.facts}
    keep = [(_n(p.id), p) for p in patch.upsert_facts if _check_proposed_fact(p, existing)]
    dropped = len(patch.upsert_facts) - len(keep)
    for fact_id in remove_ids:
        current = existing.get(fact_id)
        if current is None:
            raise PatchRejected(ValidationReason.UNKNOWN_FIELD)
        if _protected(current):
            raise PatchRejected(ValidationReason.HARD_CONSTRAINT)

    facts = {fact_id: fact for fact_id, fact in existing.items() if fact_id not in remove_ids}
    if "planning_area" not in facts:
        facts["planning_area"] = planning_area_fact()  # the editable Toronto/GTA assumption
    for fact_id, proposed in keep:
        facts[fact_id] = proposed
    if len(facts) > MAX_FACTS:
        raise PatchRejected(ValidationReason.OVERSIZE)
    status = WorkspaceStatus.EXPLORING if workspace.status in _EARLY else workspace.status
    updated = evolve(workspace, facts=list(facts.values()), status=status,
                     version=workspace.version + 1)
    return updated, dropped


def apply_patch(workspace: TripWorkspace, patch: WorkspacePatch) -> TripWorkspace:
    """The next workspace (facts with unacceptable text silently dropped), or ``PatchRejected``."""
    return apply_patch_checked(workspace, patch)[0]
