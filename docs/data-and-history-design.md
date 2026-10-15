# Data, History, Patterns and Notification Design

Updated 2026-10-14. This document defines the stored data that every phase in [trip-assistant-roadmap.md](trip-assistant-roadmap.md) shares, so that history (Phase 1), patterns and routines (Phase 2) and routine reminder notifications (Phase 3) add features without changing earlier data. It follows decisions 16–22 in [decisions.md](decisions.md) and the owner-supplied legal summary in [legal/google-maps-llm-policy-summary.md](legal/google-maps-llm-policy-summary.md). Items marked as hypotheses are starting values to tune from the pilot.

## Storage classes

Every field carries one class, enforced in code by a `content_class` on evidence and a wrapper type for Maps content.

| Class | Examples | Rule |
|---|---|---|
| `USER` | Goal text, labels the user typed, ratings, timestamps, consent | Store under the retention rules below |
| `PLACE_ID` | Google place ID | Store indefinitely; refresh IDs that Google reports obsolete |
| `MAPS_GROUNDED` | Grounded answer text and its sources | Display cache only, 24 hours by default, 30 consecutive days at most. Never in conversation history, summaries, logs, traces, evaluation data, exports or prompts for any purpose other than display |
| `MAPS_PLACES` | Names, addresses, hours, ratings, categories, reviews | Not stored. Re-fetched by place ID at display time and held in memory for a short time only |
| `MAPS_LATLNG` | Venue coordinates | Avoid. If needed, at most 30 consecutive days, then deleted |
| `DERIVED_SAFE` | Day of week, daypart, the user's activity tag, counts | Derived only from `USER` fields and timestamps |

## Outing history record

History is an append-only log of user actions. Pattern computation reads only these fields, so it never needs Maps content.

```text
OutingRecord  (schema_version, id, owner_id, workspace_id, plan_version)
  occurred_at (UTC), tz, local_dow, local_daypart       # derived at write
  place_ref { place_id, id_checked_at }                 # PLACE_ID only
  user_label?                                           # only if the user typed it; never filled from a Maps name
  intent_tag  (FOOD_DRINK, OUTDOOR, KIDS, ERRAND, ...)  # classified from the user's request, not from Maps
  events[] { type: SELECTED | HANDOFF_LAUNCHED | REMINDER_SET | VISITED_CONFIRMED | SKIPPED, at }
  feedback { thumb?, reason_enum?, note? }              # note is excluded from logs
  strength    (HANDOFF < SELF_REPORTED_VISIT)           # a handoff is not proof of a visit
  consent_id, retention_expires_at
```

- A place's category is Places content and is not stored. The activity tag comes from the user's own request or a label the user chooses at check-in.
- “Visited” exists only when the user confirms it. Nothing is inferred from location.
- The history screen shows IDs and user data from storage, then fetches names and details by place ID with a minimal field mask, renders them directly with Google attribution, and keeps them in memory only. If an ID is not found, the screen shows the user's label and “details unavailable”. A refresh job rechecks IDs older than about 12 months against Google's current guidance.
- Users can hide any entry. Entries that could reveal a sensitive category (for example health, religion, children's activities) are never surfaced as a pattern unless the user has chosen to label them.
- Retention is a rolling 180 days by default, adjustable by the user. The consent notice states the period. Phase 0 records outings locally on the device only.

## Consent ledger

`ConsentRecord` is append-only: `schema_version, id, owner_id, purpose, notice_version, processors[], scope{retention_days, channels, ...}, granted_at, withdrawn_at?, context`.

- Purposes: `LOCATION_FOREGROUND`, `HISTORY_STORE`, `PATTERN_INFERENCE`, `PROACTIVE_NOTIFY`, `CLOUD_SYNC` and later `CALENDAR_READ`.
- Each use checks the active consent for its purpose when it reads data.
- Withdrawal starts a cascade job that stops scheduled work, drops the push subscription and deletes or stops using the data for that purpose.
- The notice names the processors (Google, OpenRouter and the upstream model host), states that processing may happen outside Canada, and states the retention period.

## Patterns and routines

A **pattern candidate** is computed deterministically, without a model, from `OutingRecord`s when a session opens and, in Phase 3, in a bounded sweep over consenting users. It is persisted only when it is proposed to the user, so that declined patterns are never proposed again.

```text
PatternCandidate  (schema_version, id, owner_id, kind, params, support_count, window,
                   supporting_record_ids, status PROPOSED|ACCEPTED|DECLINED|SNOOZED,
                   proposed_at, expires_at)
```

Unconfirmed candidates expire. Starting hypothesis for a weekly pattern: at least four of the last six weeks.

A **routine** is created only by a confirmed `PendingAction` of type `CREATE_ROUTINE`.

```text
Routine  (schema_version, id, owner_id, label,
          place_ref? or intent_tag, schedule{dow[], window, tz},
          origin USER_CREATED | FROM_PATTERN, source_candidate_id?,
          confirmation_ref (PendingAction id), status, version)
```

A routine is a fact with `source=MEMORY`, `confirmation=CONFIRMED` and `hard=false`. It becomes a hard constraint only through a separate explicit user statement. A model may phrase a pattern from the activity tag, day and daypart only. Place names in the interface come from a component that fetches them by place ID.

Patterns use counts by weekday and time bucket. A trained model over place IDs is not used until Google answers the open question in [legal/open-questions.md](legal/open-questions.md).

## Routine reminder notifications (Phase 3)

| Store | Contents |
|---|---|
| `NotificationPreference` | Channel flags, quiet hours (hypothesis: 21:00–08:00 local), caps (hypothesis: 1 per day and 3 per week), per-routine mute, snooze, time zone |
| `PushSubscription` | Channel `WEBPUSH` (later `FCM_ANDROID`), encrypted endpoint, `installed_pwa`, status, last success |
| `NotificationOutbox` | `id, owner, routine_id, expected_routine_version, dedupe_key = routine_id + local_date, scheduled_for, status PENDING|SENT|SUPPRESSED(reason)|FAILED|EXPIRED, channel, payload_ref (opaque), attempts` |

Web constraints: iOS web push works only from an installed Home Screen web app with permission requested from a user gesture. A closed tab cannot fire a local alarm, so only server push can reach the user. Guests cannot receive notifications. The permission prompt appears only after the user confirms a routine and opts in. An in-app inbox is the fallback.

Trigger path:

1. A daily planner materializes the next 24–48 hours of occurrences.
2. It schedules one Cloud Task per occurrence carrying only the routine ID, routine version and occurrence date, authenticated with OIDC.
3. The handler checks, in order, the current routine version, active consent, push subscription, quiet hours, caps, the dedupe key and opt-out. Any failure records the outbox row as `SUPPRESSED` with a reason.
4. If all pass, it sends a generic message, for example “Time for your usual Saturday outing? Tap to plan.” with a one-tap stop action. It names no place and carries no Maps content.
5. Tapping opens a new workspace through an opaque deep link. A normal metered turn then runs discovery.
6. A side effect remains a normal `PendingAction` confirmed in the app. A notification never creates or executes an action.
7. Repeated ignored notifications snooze the routine.

## Discovery and evidence contracts

```text
Orchestrator -> ModelGateway (OpenRouter, pinned provider, zero retention, no fallbacks)
  call A  FRAME:    intent_tag, assumptions, WorkspacePatch from user facts only (no Maps content)
  call B  DISCOVER: server-side MCP client for Grounding Lite with a tool allowlist
                    -> GroundedAnswer { text, sources[{place_id, maps_uri, attribution, preview}] }
Validator: accepts or rejects, never edits
Direct path (no LLM): Places Details by place ID, Routes check -> server-rendered components
```

- The output of call B is `MAPS_GROUNDED`. It is never logged, never summarized into history, and never used to produce a patch. The model's `WorkspacePatch` comes only from call A and may reference place IDs but never names, hours or ratings.
- The validator checks that each source came from a tool call in this run, that source URIs are on the allowlist, finish reason, truncation, budgets and schema. It may reject the whole block and fall back. It cannot redact, trim or reorder it.
- The server renders the grounded text escaped and unmodified with sources immediately after it, Google Maps attribution and link previews, and no interstitial content. Server-authored framing above it is visually distinct. Route and place components are separate and labelled.
- Selecting a source sets `selected_place_id`. The server then calls Places Details and Routes directly for the feasibility check.
- Follow-up turns do not replay stored Maps content. The server stores user text, facts and place IDs.
- Hard needs such as accessibility or diet cannot be verified inside grounded text without extracting from it. In Phase 0 they are passed as query terms and flagged, and the details come from direct Places data shown separately.

`Evidence` keeps its existing fields and adds:

```text
schema_version
kind            GROUNDED_ANSWER | PLACES_DIRECT | ROUTE_DIRECT | USER | WEATHER
content_class   MAPS_GROUNDED | MAPS_PLACES | MAPS_ROUTE | USER | NONE
llm_visible     bool        # false for PLACES_DIRECT and ROUTE_DIRECT until decision 17 changes
storage_class   EPHEMERAL | DISPLAY_CACHE | PLACE_ID_ONLY
cache_expires_at            # at most retrieved_at + 30 days, enforced by the validator
place_id?
attribution_required  bool
tool_call_id                # provenance within the run
content_hash                # for the verbatim-display invariant; hash only, never the content
```

`expires_at` (freshness) and `cache_expires_at` (storage) are separate. `Evidence` holds no content.

`PlanVersion` is immutable: `schema_version, id, workspace_id, version, parent_version?, replaced_reason?, destination{place_id, user_label?}, origin_ref, user_constraint fact IDs, grounded_evidence_id (display cache pointer that may expire), route_evidence_id? (ephemeral), assumption_fact_ids[], warnings[], created_at`. After the cache expires, rendering uses place-ID re-fetch. Activation (save, reminder or handoff) writes the `OutingRecord`.

## Versioning, retention, deletion and export

Every persisted document and API payload carries `schema_version` and `kind`. Changes are additive within a version; upcasters run on read with a backfill job when needed; input models stay `extra="forbid"`.

| Store | Retention default | Deletion | Export |
|---|---|---|---|
| Grounded display cache | 24 hours, 30 days at most | TTL purge and account deletion | Excluded |
| Guest server workspace summary | 7 inactive days | TTL | Place IDs, user text, plan decisions |
| `OutingRecord` | Opt-in rolling 180 days, user-adjustable | Per item, clear all, on consent withdrawal, on account deletion | Yes (IDs, labels, timestamps, feedback) |
| `ConsentRecord` | Life of account plus the audit window counsel approves | Account deletion, keeping only approved audit residue | Yes |
| `PatternCandidate` | Expires at `expires_at` | With history | Yes |
| `Routine` | Until the user deletes it | Per item, account deletion | Yes |
| `PushSubscription` | Until revoked or unreachable | On revoke, withdrawal, account deletion | No |
| `NotificationOutbox` | 30 days | TTL | No |
| Idempotency records, cost ledger | Existing policy | Existing policy | Existing policy |

Late jobs must not recreate a deleted user.

## Open points

- Whether a place ID returned by Grounding Lite falls under the indefinite-storage rule, whether analysis over stored place IDs is permitted, and whether Google accepts zero-retention hosting as “not cached by the model provider” are tracked in [legal/open-questions.md](legal/open-questions.md).
- Pattern thresholds, quiet hours and notification caps are hypotheses to tune from the pilot.
