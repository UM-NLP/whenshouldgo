"""GroundedAnswerValidator: accept or reject the WHOLE block; it can never edit. Provenance is
checked against the run ledger the client wrote from the tool's own payload."""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from maps_simulator import (
    NOW,
    PRINCIPAL,
    SYNTH_TEXT,
    SyntheticMapsClient,
    new_run,
    place_link,
    synthetic_payload,
)
from test_discovery_contracts import TTLS

from app.discovery import (
    GroundedAnswer,
    GroundedAnswerRejected,
    GroundedAnswerValidator,
    RejectionReason,
    display_hash,
    parse_grounded_answer,
)
from app.domain import ContentClass, EvidenceStorageClass, evolve
from app.telemetry import InMemorySink, Phase, ValidationRejection

validator = GroundedAnswerValidator(TTLS)


def run_and_answer(payload=None, run=None):
    run = run or new_run()
    result = SyntheticMapsClient(payload=payload).call_tool(run, "search_places", {"text_query": "q"})
    return run, parse_grounded_answer(result, TTLS)


def forged(answer: GroundedAnswer, **changes) -> GroundedAnswer:
    """Build a contract-invalid answer around the validators, as a buggy caller might."""
    values = {name: getattr(answer, name) for name in type(answer).model_fields}
    values.update(changes)
    return GroundedAnswer.model_construct(**values)


def forged_place(place, **changes):
    values = {name: getattr(place, name) for name in type(place).model_fields}
    values.update(changes)
    return type(place).model_construct(**values)


def consistent(answer: GroundedAnswer, places) -> GroundedAnswer:
    """A forgery whose own hashes are internally consistent (so only the ledger can catch it)."""
    return forged(answer, places=tuple(places), display_hash=display_hash(
        answer.text, [(p.place_id, p.attribution_title, p.attribution_url, p.place_url) for p in places]))


def reason_of(answer, run, now=NOW, principal=PRINCIPAL, **kw) -> RejectionReason:
    with pytest.raises(GroundedAnswerRejected) as caught:
        GroundedAnswerValidator(TTLS, **kw).validate(answer, run, now, principal_id=principal)
    return caught.value.reason


def accept(answer, run, now=NOW) -> None:
    validated = validator.validate(answer, run, now, principal_id=PRINCIPAL)
    assert validated.answer is answer


def test_a_sound_answer_from_this_run_is_accepted_unchanged() -> None:
    run, answer = run_and_answer()
    before = answer.model_dump()
    accept(answer, run)
    assert answer.model_dump() == before


def test_the_validator_has_no_way_to_edit_an_answer() -> None:
    public = {name for name in dir(validator) if not name.startswith("_")}
    assert public == {"validate"}


# --- forgeries a ledger that only held place ids would have accepted (H2) ---------------------------
def test_a_forged_source_link_with_a_real_place_id_and_real_text_is_rejected() -> None:
    run, answer = run_and_answer()
    # A well-formed link to ANOTHER place (consistent token), swapped in for the real one.
    other = place_link(9)
    bad = consistent(answer, [forged_place(answer.places[0], place_url=other, attribution_url=other),
                              answer.places[1]])
    assert reason_of(bad, run) == RejectionReason.PROVENANCE
    # A link that disagrees with the place's own attribution link fails the integrity check.
    lopsided = consistent(answer, [forged_place(answer.places[0], place_url=other), answer.places[1]])
    assert reason_of(lopsided, run) == RejectionReason.URI


def test_a_forged_attribution_url_or_title_is_rejected() -> None:
    run, answer = run_and_answer()
    for change in ({"attribution_url": place_link(9), "place_url": place_link(9)},
                   {"attribution_title": "Google Maps Official"}):
        bad = consistent(answer, [forged_place(answer.places[0], **change), answer.places[1]])
        assert reason_of(bad, run) == RejectionReason.PROVENANCE


def test_forged_text_with_real_places_and_consistent_hashes_is_rejected() -> None:
    run, answer = run_and_answer()
    text = "Model-written text [0] [1] [2] [3] [4]."
    bad = forged(answer, text=text, content_hash=hashlib.sha256(b"x").hexdigest())
    assert reason_of(bad, run) == RejectionReason.HASH
    from app.discovery import content_hash

    consistent_text = forged(answer, text=text, content_hash=content_hash(text), display_hash=display_hash(
        text, [(p.place_id, p.attribution_title, p.attribution_url, p.place_url) for p in answer.places]))
    assert reason_of(consistent_text, run) == RejectionReason.PROVENANCE


def test_replaying_another_calls_result_under_this_calls_id_is_rejected() -> None:
    run = new_run()
    _, first = run_and_answer(synthetic_payload(text="First answer [0]."), run=run)
    _, second = run_and_answer(synthetic_payload(text="Second answer [1]."), run=run)
    replay = forged(second, tool_call_id=first.tool_call_id,
                    places=tuple(forged_place(p, tool_call_id=first.tool_call_id) for p in second.places))
    assert reason_of(replay, run) == RejectionReason.PROVENANCE
    accept(first, run)
    accept(second, run)


def test_provenance_every_place_must_come_from_this_call_of_this_run() -> None:
    run, answer = run_and_answer()
    assert reason_of(answer, new_run()) == RejectionReason.PROVENANCE  # another run's ledger
    ghost = forged_place(answer.places[0], tool_call_id="tc-ghost")
    assert reason_of(consistent(answer, [ghost, answer.places[1]]), run) == RejectionReason.PROVENANCE
    # a place from a different call of the same run is not allowed to ride in this answer
    other_run_call = SyntheticMapsClient().call_tool(run, "search_places", {"text_query": "other"})
    other = forged_place(answer.places[0], tool_call_id=other_run_call.tool_call_id)
    assert reason_of(consistent(answer, [other, answer.places[1]]), run) == RejectionReason.PROVENANCE
    smuggled = forged_place(answer.places[0], place_id="SYNTH_PLACE_000000000000007", cited=False)
    assert reason_of(consistent(answer, [*answer.places, smuggled]), run) == RejectionReason.PROVENANCE


def test_an_answer_cannot_be_replayed_across_runs_or_tenants() -> None:
    run, answer = run_and_answer()
    assert reason_of(answer, run, principal="user-2") == RejectionReason.PROVENANCE  # another tenant
    other_tenant_run = new_run(principal_id="user-2", run_id="run-1")
    assert reason_of(answer, other_tenant_run) == RejectionReason.PROVENANCE
    same_ids_other_run = new_run(run_id="run-2")
    assert reason_of(forged(answer), same_ids_other_run) == RejectionReason.PROVENANCE
    assert reason_of(forged(answer, run_id="run-2"), run) == RejectionReason.PROVENANCE


def test_the_ledger_cannot_be_written_from_validator_or_model_code() -> None:
    run, _ = run_and_answer()
    with pytest.raises(Exception):  # noqa: B017 - TypeError (no authority) or a typed refusal
        run.record("tc-x", None)  # type: ignore[call-arg]
    with pytest.raises(Exception):  # noqa: B017
        run.begin("search_places", authority=object())  # type: ignore[arg-type]


# --- format, size, class, storage, expiry (as before, on the documented shape) -----------------------
def test_a_hash_that_does_not_match_the_text_is_rejected() -> None:
    run, answer = run_and_answer()
    assert reason_of(forged(answer, content_hash="0" * 64), run) == RejectionReason.HASH
    assert reason_of(forged(answer, display_hash="0" * 64), run) == RejectionReason.HASH


@pytest.mark.parametrize("uri", ["http://maps.app.goo.gl/SYNTH0001", "https://evil.example/x",
                                 "https://www.google.com/maps/../url?q=x", "https://www.google.com/url?q=x",
                                 "https://user@maps.app.goo.gl/SYNTH0001"])
def test_link_urls_must_pass_the_strict_allowlist(uri: str) -> None:
    run, answer = run_and_answer()
    for field in ("place_url", "attribution_url"):
        bad = consistent(answer, [forged_place(answer.places[0], **{field: uri}), answer.places[1]])
        assert reason_of(bad, run) == RejectionReason.URI


def test_place_ids_must_match_the_place_id_pattern() -> None:
    run, answer = run_and_answer()
    for bad_id in ("has space", "x" * 301, "", "id/with/slash", "id​"):
        bad = consistent(answer, [forged_place(answer.places[0], place_id=bad_id), answer.places[1]])
        assert reason_of(bad, run) in (RejectionReason.FORMAT, RejectionReason.SIZE)


def test_sizes_must_be_within_bounds() -> None:
    run, answer = run_and_answer()
    assert reason_of(forged(answer, text="x" * 9_000), run) in (RejectionReason.SIZE, RejectionReason.HASH)
    assert reason_of(forged(answer, places=tuple(answer.places[0] for _ in range(25))), run) == RejectionReason.SIZE
    assert reason_of(forged(answer, places=()), run) == RejectionReason.SIZE
    assert reason_of(answer, run, max_text_chars=10) == RejectionReason.SIZE
    long_title = forged_place(answer.places[0], attribution_title="a" * 500)
    assert reason_of(consistent(answer, [long_title, answer.places[1]]), run) == RejectionReason.SIZE


def test_class_and_storage_must_be_correct() -> None:
    run, answer = run_and_answer()
    assert reason_of(forged(answer, content_class=ContentClass.MAPS_PLACES), run) == RejectionReason.CLASS
    assert reason_of(forged(answer, content_class=ContentClass.USER), run) == RejectionReason.CLASS
    assert reason_of(forged(answer, storage_class=EvidenceStorageClass.EPHEMERAL), run) == RejectionReason.STORAGE
    assert reason_of(forged(answer, storage_class=EvidenceStorageClass.PLACE_ID_ONLY), run) == RejectionReason.STORAGE


def test_citations_must_be_in_range_and_flags_consistent() -> None:
    run, answer = run_and_answer()
    flipped = consistent(answer, [forged_place(answer.places[0], cited=not answer.places[0].cited), answer.places[1]])
    assert reason_of(flipped, run) in (RejectionReason.FORMAT, RejectionReason.PROVENANCE)
    # an out-of-range bracket (a year, say) is ignored, never a reason to reject
    run2, year = run_and_answer(synthetic_payload(text="Opened in [2019]; see [0]."))
    accept(year, run2)


def test_display_cache_expiry_must_be_within_the_configured_and_hard_limits() -> None:
    run, answer = run_and_answer()
    at = answer.retrieved_at
    for delta in (timedelta(hours=25), timedelta(days=31), timedelta(0), timedelta(hours=-1)):
        assert reason_of(forged(answer, cache_expires_at=at + delta), run) == RejectionReason.CACHE_EXPIRY
    hard = GroundedAnswerValidator(evolve(TTLS, grounded_cache_hours=24 * 30))
    with pytest.raises(GroundedAnswerRejected) as caught:
        hard.validate(forged(answer, cache_expires_at=at + timedelta(days=30, seconds=1)), run, NOW,
                      principal_id=PRINCIPAL)
    assert caught.value.reason == RejectionReason.CACHE_EXPIRY


def test_an_answer_past_its_cache_expiry_or_from_the_future_is_rejected() -> None:
    run, answer = run_and_answer()
    assert reason_of(answer, run, now=answer.cache_expires_at) == RejectionReason.CACHE_EXPIRY
    assert reason_of(answer, run, now=answer.retrieved_at - timedelta(hours=1)) == RejectionReason.CACHE_EXPIRY
    with pytest.raises(ValueError):
        validator.validate(answer, run, NOW.replace(tzinfo=None), principal_id=PRINCIPAL)


def test_hidden_characters_reject_the_whole_block() -> None:
    from app.discovery import content_hash

    run, answer = run_and_answer()
    for text in ("bad\x00text", "bidi‮text", "zero​width", "lone\ud800"):
        bad = forged(answer, text=text, content_hash=content_hash(text))
        assert reason_of(bad, run) == RejectionReason.FORMAT
    title = forged_place(answer.places[0], attribution_title="Caf‮")
    assert reason_of(consistent(answer, [title, answer.places[1]]), run) == RejectionReason.FORMAT


def test_a_non_answer_object_is_rejected() -> None:
    run, _ = run_and_answer()
    with pytest.raises(GroundedAnswerRejected) as caught:
        validator.validate({"text": SYNTH_TEXT}, run, NOW, principal_id=PRINCIPAL)  # type: ignore[arg-type]
    assert caught.value.reason == RejectionReason.FORMAT


def test_rejections_are_typed_static_and_never_echo_content() -> None:
    run, answer = run_and_answer(synthetic_payload(text="SECRET-MAPS-CANARY [0]"))
    with pytest.raises(GroundedAnswerRejected) as caught:
        validator.validate(forged(answer, content_hash="0" * 64), run, NOW, principal_id=PRINCIPAL)
    assert "CANARY" not in str(caught.value) + repr(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_a_rejection_emits_a_content_free_telemetry_event() -> None:
    run, answer = run_and_answer()
    sink = InMemorySink()
    with pytest.raises(GroundedAnswerRejected):
        GroundedAnswerValidator(TTLS, sink=sink).validate(
            forged(answer, content_hash="0" * 64), run, NOW, principal_id=PRINCIPAL)
    [event] = sink.events
    assert isinstance(event, ValidationRejection) and event.phase == Phase.TOOL
    assert "SYNTH" not in event.model_dump_json()
