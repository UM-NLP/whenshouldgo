"""Verbatim render contract. ONE contract: the block carries the RAW summary text and raw
attribution fields, ``render_mode`` is "text" (insert via textContent, never as HTML), the
hash covers the exact string the user sees, and escaping exists only in the named HTML-sink
helper ``html_sink_escape``. Verification is bound to the run ledger, not just to a fresh
render of the same object."""
from __future__ import annotations

import pytest
from maps_simulator import (
    INJECTION,
    SYNTH_TEXT,
    SyntheticMapsClient,
    new_run,
    synth_title,
    synthetic_payload,
    wrap_unvalidated as V,
)
from test_discovery_contracts import TTLS

from app.discovery import (
    FRAMING_TEXT,
    FramingKind,
    GroundedTextPart,
    ServerTextPart,
    SourcePart,
    VerbatimViolation,
    content_hash,
    html_sink_escape,
    parse_grounded_answer,
    render_grounded_block,
    verify_verbatim,
)
from app.domain import evolve


def fixture(payload=None):
    payload = synthetic_payload(places=2) if payload is None else payload
    run = new_run()
    result = SyntheticMapsClient(payload=payload).call_tool(run, "search_places", {"text_query": "q"})
    answer = parse_grounded_answer(result, TTLS)
    return run, answer, render_grounded_block(V(answer))


def test_the_block_is_the_raw_text_then_every_source_in_order_bound_to_the_ledger() -> None:
    run, answer, block = fixture()
    assert [type(p) for p in block.parts] == [GroundedTextPart, SourcePart, SourcePart]
    assert block.parts[0].text == answer.text  # raw, never escaped in the block
    assert block.parts[0].content_hash == content_hash(answer.text)  # the exact string shown
    assert [p.place_id for p in block.parts[1:]] == [p.place_id for p in answer.places]
    assert block.render_mode == "text" and block.branding == "Google Maps"
    assert block.translate_no is True  # "Google Maps" must never be translated or localized
    verify_verbatim(block, V(answer), run)


def test_sources_show_the_attribution_title_exactly_and_link_with_the_returned_urls() -> None:
    run, answer, block = fixture()
    part, place = block.parts[1], answer.places[0]
    assert part.attribution_title == place.attribution_title == synth_title(1)
    assert part.attribution_url == place.attribution_url
    assert part.place_url == place.place_url and part.cited == place.cited


def test_text_is_never_html_escaped_inside_the_block_only_at_the_named_sink() -> None:
    run, answer, block = fixture(synthetic_payload(text=INJECTION))
    assert block.parts[0].text == INJECTION and "<script>" in block.parts[0].text
    assert block.render_mode == "text"  # the client must use textContent
    assert html_sink_escape(INJECTION).count("<") == 0
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_sink_escape(INJECTION)
    assert html_sink_escape('"q" & \'s\'') == "&quot;q&quot; &amp; &#x27;s&#x27;"
    import html

    assert html.unescape(html_sink_escape(INJECTION)) == INJECTION  # lossless at the sink
    verify_verbatim(block, V(answer), run)


def test_server_framing_is_a_separate_typed_field_and_never_inside_the_block() -> None:
    run, answer, _ = fixture()
    block = render_grounded_block(V(answer), framing=FramingKind.RESULTS_INTRO)
    assert block.framing == FramingKind.RESULTS_INTRO and FRAMING_TEXT[FramingKind.RESULTS_INTRO]
    assert all(not isinstance(p, ServerTextPart) for p in block.parts)
    verify_verbatim(block, V(answer), run)
    with pytest.raises(ValueError):
        render_grounded_block(V(answer), framing="free text from the server")  # type: ignore[arg-type]
    assert set(FRAMING_TEXT) == set(FramingKind)


def _swap(block, parts):
    return evolve(block, parts=tuple(parts))


CASES = [
    "edited_text", "truncated_text", "extra_text_appended", "hash_swapped", "text_dropped",
    "sources_reordered", "source_missing", "source_duplicated", "source_modified_url",
    "source_modified_title", "source_modified_place_url", "source_modified_place",
    "cited_flag_flipped", "interspersed_between_text_and_sources", "interspersed_between_sources",
    "server_part_before_text", "server_part_after_sources", "sources_before_text", "no_sources",
    "escaped_text", "wrong_branding", "wrong_render_mode", "translate_allowed",
]


@pytest.mark.parametrize("case", CASES)
def test_any_modification_truncation_reorder_or_interspersing_is_rejected(case: str) -> None:
    run, answer, block = fixture()
    text, s1, s2 = block.parts
    note = ServerTextPart(text="Here are some great picks!")
    mutated = {
        "edited_text": lambda: _swap(block, [evolve(text, text=text.text.replace("invented", "great")), s1, s2]),
        "truncated_text": lambda: _swap(block, [evolve(text, text=text.text[:-10]), s1, s2]),
        "extra_text_appended": lambda: _swap(block, [evolve(text, text=text.text + " Enjoy!"), s1, s2]),
        "hash_swapped": lambda: _swap(block, [evolve(text, content_hash="0" * 64), s1, s2]),
        "text_dropped": lambda: _swap(block, [s1, s2]),
        "sources_reordered": lambda: _swap(block, [text, s2, s1]),
        "source_missing": lambda: _swap(block, [text, s1]),
        "source_duplicated": lambda: _swap(block, [text, s1, s1, s2]),
        "source_modified_url": lambda: _swap(block, [text, evolve(s1, attribution_url="https://maps.app.goo.gl/EVIL1"), s2]),
        "source_modified_title": lambda: _swap(block, [text, evolve(s1, attribution_title="Better title"), s2]),
        "source_modified_place_url": lambda: _swap(block, [text, evolve(s1, place_url="https://www.google.com/maps/place/EVIL"), s2]),
        "source_modified_place": lambda: _swap(block, [text, evolve(s1, place_id="SYNTH_PLACE_000000000009999"), s2]),
        "cited_flag_flipped": lambda: _swap(block, [text, evolve(s1, cited=not s1.cited), s2]),
        "interspersed_between_text_and_sources": lambda: _swap(block, [text, note, s1, s2]),
        "interspersed_between_sources": lambda: _swap(block, [text, s1, note, s2]),
        "server_part_before_text": lambda: _swap(block, [note, text, s1, s2]),
        "server_part_after_sources": lambda: _swap(block, [text, s1, s2, note]),
        "sources_before_text": lambda: _swap(block, [s1, s2, text]),
        "no_sources": lambda: _swap(block, [text]),
        "escaped_text": lambda: _swap(block, [evolve(text, text=html_sink_escape(text.text) + "&amp;"), s1, s2]),
        "wrong_branding": lambda: evolve(block, branding="Maps-ish"),
        "wrong_render_mode": lambda: evolve(block, render_mode="html"),
        "translate_allowed": lambda: evolve(block, translate_no=False),
    }[case]()
    with pytest.raises((VerbatimViolation, ValueError)):
        verify_verbatim(mutated, V(answer), run)


def test_a_block_and_answer_that_agree_with_each_other_but_not_with_the_ledger_are_rejected() -> None:
    from test_discovery_validator import forged, forged_place

    run, answer, _ = fixture()
    # A consistent forgery: different place link, hashes recomputed, block rendered from it.
    fake_place = forged_place(answer.places[0], place_url="https://www.google.com/maps/place/EVIL")
    from app.discovery import display_hash

    fake = forged(answer, places=(fake_place, answer.places[1]),
                  display_hash=display_hash(answer.text, [
                      (p.place_id, p.attribution_title, p.attribution_url, p.place_url)
                      for p in (fake_place, answer.places[1])]))
    block = render_grounded_block(V(fake))
    with pytest.raises(VerbatimViolation):
        verify_verbatim(block, V(fake), run)  # a fresh render of the forgery matches; the ledger does not


def test_a_block_for_a_different_answer_or_another_run_is_rejected() -> None:
    run, answer, block = fixture()
    _, other_answer, _ = fixture(synthetic_payload(text="A different invented answer [0]."))
    with pytest.raises(VerbatimViolation):
        verify_verbatim(block, V(other_answer), run)
    with pytest.raises(VerbatimViolation):
        verify_verbatim(block, V(answer), new_run(run_id="run-2"))  # that run never made this call


def test_verification_error_never_echoes_content() -> None:
    run, answer, block = fixture(synthetic_payload(text=INJECTION))
    text = block.parts[0]
    bad = _swap(block, [evolve(text, text=text.text + "x"), *block.parts[1:]])
    with pytest.raises(VerbatimViolation) as caught:
        verify_verbatim(bad, V(answer), run)
    assert "alert(1)" not in str(caught.value) + repr(caught.value)
    assert caught.value.__context__ is None


def test_rendering_is_pure_and_content_stays_out_of_reprs() -> None:
    _, answer, _ = fixture()
    assert render_grounded_block(V(answer)) == render_grounded_block(V(answer))
    assert SYNTH_TEXT not in repr(render_grounded_block(V(answer)))
