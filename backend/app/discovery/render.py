"""Verbatim render contract (docs/legal/google-maps-llm-policy-summary.md section 6; Google's
attribution page, retrieved 2026-10-15).

ONE contract. The block carries the RAW summary text and the raw attribution fields; its
``render_mode`` is "text": a client inserts every field with ``textContent`` and never as HTML or
Markdown. Escaping exists in exactly one place, ``html_sink_escape``, for a sink that must write
HTML. The text hash covers the exact string the user sees, because that string is never altered.

The block is the text, then every place's source immediately after it, and nothing else.
Server framing is a separate enum-typed field above the block. ``branding`` ("Google Maps") sits
beside it with ``translate_no`` (Google: the name must not be translated or localized).
``verify_verbatim`` is bound to the run ledger the client wrote: a block that matches a
forged answer, however consistent, is rejected.
"""
from __future__ import annotations

import html
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from ..domain import FramingKind
from ._validated import ValidatedAnswer, is_validated
from .client import ToolRun
from .contracts import GroundedAnswer, GroundFrozen, content_hash, display_hash
from .errors import DiscoveryError

RENDER_MODE = "text"
BRANDING = "Google Maps"


class RenderOption(StrEnum):
    """Named render options. NONE is enabled by default: the summary is plain text and its Markdown
    (``**bold**``, ``- `` bullets) is shown literally.

    TODO(legal): whether a client may render Markdown emphasis in grounded text is a QUESTION FOR
    GOOGLE (open question 12, docs/legal/open-questions.md). ``MARKDOWN_EMPHASIS`` is a placeholder
    for that decision only; it is NOT implemented and requesting it raises NotImplementedError."""

    MARKDOWN_EMPHASIS = "markdown_emphasis"


class VerbatimViolation(DiscoveryError):
    default_message = "rendered block is not the verbatim grounded answer"


FRAMING_TEXT: dict[FramingKind, str] = {
    FramingKind.RESULTS_INTRO: "Here is what Google Maps found for your request.",
    FramingKind.NO_RESULTS: "I could not get results from Google Maps just now.",
}


def html_sink_escape(text: str) -> str:
    """The ONLY place text is escaped: for a sink that writes HTML. Lossless (``html.unescape``)."""
    return html.escape(text, quote=True)


class GroundedTextPart(GroundFrozen):
    text: str = Field(repr=False)  # raw, exactly as the tool returned it
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")  # hash of that exact string


class SourcePart(GroundFrozen):
    place_id: str
    attribution_title: str = Field(repr=False)  # shown exactly as provided
    attribution_url: str = Field(repr=False)  # the link target, exactly as provided
    place_url: str = Field(repr=False)
    cited: bool


class ServerTextPart(GroundFrozen):
    """Exists only so a violation can be expressed and rejected: it is never produced by
    ``render_grounded_block`` and is never valid inside the block."""

    text: Annotated[str, StringConstraints(max_length=500)] = Field(repr=False)


class RenderedBlock(GroundFrozen):
    framing: FramingKind | None = None  # above the block, never inside
    parts: tuple[GroundedTextPart | SourcePart | ServerTextPart, ...]
    branding: str = BRANDING  # required Google Maps attribution, beside the block
    translate_no: bool = True  # translate="no": "Google Maps" is never translated or localized
    render_mode: str = RENDER_MODE


def _source_parts(answer: GroundedAnswer) -> tuple[SourcePart, ...]:
    return tuple(
        SourcePart(
            place_id=p.place_id, attribution_title=p.attribution_title,
            attribution_url=p.attribution_url, place_url=p.place_url, cited=p.cited,
        )
        for p in answer.places
    )


def render_grounded_block(
    validated: ValidatedAnswer,
    framing: FramingKind | None = None,
    *,
    options: frozenset[RenderOption] = frozenset(),
) -> RenderedBlock:
    """Render a VALIDATED answer. A bare GroundedAnswer (or a forged wrapper) is refused: only
    ``GroundedAnswerValidator.validate`` can mint a ValidatedAnswer."""
    if not is_validated(validated):
        raise ValueError("only a validated answer can be rendered")
    if options:
        raise NotImplementedError("render options are placeholders; Markdown emphasis awaits Google's answer")
    if framing is not None and not isinstance(framing, FramingKind):
        raise ValueError("framing must be a FramingKind")
    answer = validated.answer
    return RenderedBlock(
        framing=framing,
        parts=(GroundedTextPart(text=answer.text, content_hash=content_hash(answer.text)),
               *_source_parts(answer)),
    )


def _block_display_hash(rendered: RenderedBlock) -> str | None:
    """The display hash of what the block actually shows, or None if it is not text-then-sources."""
    parts = rendered.parts
    if not parts or not isinstance(parts[0], GroundedTextPart):
        return None
    sources = parts[1:]
    if not sources or not all(isinstance(p, SourcePart) for p in sources):
        return None
    return display_hash(parts[0].text, [
        (p.place_id, p.attribution_title, p.attribution_url, p.place_url)  # type: ignore[union-attr]
        for p in sources
    ])


def verify_verbatim(rendered: RenderedBlock, validated: ValidatedAnswer, run: ToolRun) -> None:
    """Raise ``VerbatimViolation`` unless the block is exactly text-then-sources for ``answer``
    AND both match what the client recorded for that tool call in ``run``'s ledger."""
    if not _matches(rendered, validated, run):
        raise VerbatimViolation()


def _matches(rendered: object, validated: object, run: object) -> bool:
    if not (isinstance(rendered, RenderedBlock) and is_validated(validated) and isinstance(run, ToolRun)):
        return False
    answer = validated.answer  # type: ignore[attr-defined]
    if not isinstance(answer, GroundedAnswer):
        return False
    if rendered.branding != BRANDING or rendered.render_mode != RENDER_MODE or not rendered.translate_no:
        return False
    # 1. Exact, ordered equality with a fresh render of the answer: nothing interspersed,
    #    reordered, missing, late or edited, and text-then-sources only.
    expected = render_grounded_block(validated, rendered.framing)  # type: ignore[arg-type]
    parts = rendered.parts
    if len(parts) != len(expected.parts) or len(parts) < 2:
        return False
    if not all(type(a) is type(b) and a == b for a, b in zip(parts, expected.parts, strict=True)):
        return False
    # 2. The block's own display hash, recomputed from what it shows, must equal the ledger's.
    shown = _block_display_hash(rendered)
    record = run.record_for(answer.tool_call_id)
    if shown is None or record is None or record.display_hash != shown or record.text_hash != content_hash(
            parts[0].text):  # type: ignore[union-attr]
        return False
    return answer.run_id == run.run_id and answer.display_hash == shown
