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
import re
import unicodedata
from dataclasses import dataclass

from .domain import (
    Confirmation,
    Fact,
    FactSource,
    NextAction,
    TripWorkspace,
    WorkspaceEditRequest,
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
# RESERVED key: the model can neither write nor remove it. No workspace is born with it (the
# product assumes no place); a later feature may set it from a user-confirmed area.
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


# --- the user-edit path ------------------------------------------------------------------------
# A USER edit (PATCH /v1/workspace) is a different trust level from a model proposal: the verified
# user typed it, so the server stamps it USER_EXPLICIT and CONFIRMED (a user may also mark it
# hard). The model path above never produces such a fact and can never touch one. The checks here
# are strict anyway: the request is client input, sizes are capped and text is held to the same
# plain-text rule, and the server-owned keys stay protected from the user too.
MAX_USER_UPSERTS = 20
MAX_USER_REMOVALS = 20
# A user edit that selects a candidate place: the value is a Maps place ID (an identifier, never
# content) and the optional label is what the user typed. The service turns a change of the
# selection into a new immutable plan version.
SELECTED_PLACE_KEY = "selected_place_id"
SELECTED_LABEL_KEY = "selected_place_label"
SELECTION_KEYS = frozenset({SELECTED_PLACE_KEY, SELECTED_LABEL_KEY})
MAX_SELECTED_LABEL_CHARS = 80
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")


class StaleEdit(Exception):
    """The edit was built on an older workspace version. ``current`` is the stored version."""

    def __init__(self, current: int) -> None:
        super().__init__("workspace edit is stale")
        self.current = current


@dataclass(frozen=True)
class UserEditResult:
    workspace: TripWorkspace
    upserts: int
    removals: int
    changed_ids: frozenset[str]  # normalised ids of every fact the edit wrote or removed


def _user_may_not_touch(fact: Fact) -> bool:
    """Server-owned facts (reserved key, tool output) are changed by the server only."""
    return (
        _n(fact.id) in SERVER_OWNED_FACT_IDS
        or _n(fact.key) in SERVER_OWNED_FACT_IDS
        or fact.source == FactSource.TOOL
    )


def _check_user_value(key: str, value: object, hard: bool, scope: str) -> None:
    if _n(key) in SERVER_OWNED_FACT_IDS:
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    if isinstance(value, float) and not math.isfinite(value):
        raise PatchRejected(ValidationReason.SCHEMA)
    if isinstance(value, str):
        if len(value) > MAX_FACT_VALUE_CHARS:
            raise PatchRejected(ValidationReason.OVERSIZE)
        if plain_text_problem(value, allow_joiners=True):
            raise PatchRejected(ValidationReason.SCHEMA)
    if plain_text_problem(scope) or plain_text_problem(key):
        raise PatchRejected(ValidationReason.SCHEMA)
    if hard and value is None:
        raise PatchRejected(ValidationReason.SCHEMA)  # a hard constraint needs a value
    if _n(key) == SELECTED_PLACE_KEY and not (
        isinstance(value, str) and _PLACE_ID.fullmatch(value) and not hard
    ):
        raise PatchRejected(ValidationReason.SCHEMA)
    if _n(key) == SELECTED_LABEL_KEY and not (
        isinstance(value, str) and 0 < len(value) <= MAX_SELECTED_LABEL_CHARS and not hard
    ):
        raise PatchRejected(ValidationReason.SCHEMA)


def apply_user_edit(workspace: TripWorkspace, request: WorkspaceEditRequest) -> UserEditResult:
    """The workspace after a validated user edit, or ``PatchRejected`` / ``StaleEdit``.

    ``workspace`` is never modified. Every written fact is USER_EXPLICIT, CONFIRMED, never from a
    model; an existing fact with the same key (a model assumption included) is replaced in place."""
    if workspace.status in _TERMINAL:
        raise PatchRejected(ValidationReason.UNKNOWN_FIELD)
    if request.base_version != workspace.version:
        raise StaleEdit(workspace.version)
    if not request.upsert_facts and not request.remove_fact_ids:
        raise PatchRejected(ValidationReason.SCHEMA)
    if (len(request.upsert_facts) > MAX_USER_UPSERTS
            or len(request.remove_fact_ids) > MAX_USER_REMOVALS):
        raise PatchRejected(ValidationReason.OVERSIZE)
    upsert_keys = [_n(edit.key) for edit in request.upsert_facts]
    remove_ids = [_n(fact_id) for fact_id in request.remove_fact_ids]
    if (
        len(set(upsert_keys)) != len(upsert_keys)
        or len(set(remove_ids)) != len(remove_ids)
        or set(upsert_keys) & set(remove_ids)
    ):
        raise PatchRejected(ValidationReason.SCHEMA)

    facts: dict[str, Fact] = {_n(f.id): f for f in workspace.facts}
    for edit in request.upsert_facts:
        _check_user_value(edit.key, edit.value, edit.hard, edit.scope)
    for fact_id in remove_ids:
        current = facts.get(fact_id)
        if current is None:
            raise PatchRejected(ValidationReason.UNKNOWN_FIELD)
        if _user_may_not_touch(current):
            raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
    changed = set(remove_ids)
    for fact_id in remove_ids:
        del facts[fact_id]
    for edit in request.upsert_facts:
        key = _n(edit.key)
        matches = [fid for fid, f in facts.items() if fid == key or _n(f.key) == key]
        if len(matches) > 1:
            raise PatchRejected(ValidationReason.SCHEMA)
        if matches:
            old = facts[matches[0]]
            if _user_may_not_touch(old):
                raise PatchRejected(ValidationReason.HARD_CONSTRAINT)
            fact_id, fact_key = old.id, old.key  # one name per fact: keep the stored one
            del facts[matches[0]]
        else:
            fact_id = fact_key = edit.key
        facts[_n(fact_id)] = Fact(
            id=fact_id, key=fact_key, value=edit.value, source=FactSource.USER_EXPLICIT,
            confirmation=Confirmation.CONFIRMED, hard=edit.hard, scope=edit.scope)
        changed.add(_n(fact_id))
    if len(facts) > MAX_FACTS:
        raise PatchRejected(ValidationReason.OVERSIZE)
    updated = evolve(workspace, facts=list(facts.values()), version=workspace.version + 1)
    return UserEditResult(updated, len(request.upsert_facts), len(request.remove_fact_ids),
                          frozenset(changed))
