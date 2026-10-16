"""Third discovery review pass: validated wrapper, JSON-RPC ids, best-effort citations, private
authority scan, cache eviction fairness, strict JSON, SSE and attribution details, outbound
schemas, and invisible fillers. Items are numbered as in the review."""
from __future__ import annotations

import ast
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from maps_simulator import (
    ECHO,
    NOW,
    PRINCIPAL,
    FakeMcpTransport,
    SyntheticMapsClient,
    mcp_ok,
    new_run,
    synthetic_payload,
    synthetic_place,
    wrap_unvalidated,
)
from test_discovery_cache import DtClock
from test_discovery_contracts import TTLS, make_answer, result_for
from test_discovery_validator import forged, run_and_answer, validator
from test_gateway_support import SECRET, Clock, make_config, make_policy, make_request

from app.discovery import (
    TOOL_SPECS,
    CacheError,
    DiscoveryConfigError,
    DisplayCache,
    GroundedParseError,
    GroundingLiteMcpClient,
    HttpxMcpTransport,
    MalformedToolResult,
    McpResponse,
    ToolId,
    ToolResult,
    ValidatedAnswer,
    check_attribution_url,
    parse_grounded_answer,
    parse_single_sse_event,
    render_grounded_block,
    verify_verbatim,
)
from app.discovery._mint import IssuedToken
from app.gateway import CallRole, MalformedOutputError, PrivacyClass
from app.gateway.openrouter import build_body, strict_loads
from app.gateway.schema import outbound_schema

APP_DIR = Path(__file__).resolve().parents[1] / "app"
CFG = make_config()
ARGS = {"text_query": "quiet cafe"}


def client_for(script, **kw):
    transport = FakeMcpTransport(script)
    return transport, GroundingLiteMcpClient(
        transport, lambda: SECRET, config=CFG, clock=Clock(), now=lambda: NOW, **kw)


def ok():
    return mcp_ok(synthetic_payload())


# --- item 2: only a validated answer can be cached or rendered -----------------------------------------
def test_validate_returns_a_wrapper_only_the_validator_can_mint() -> None:
    run, answer = run_and_answer()
    validated = validator.validate(answer, run, NOW, principal_id=PRINCIPAL)
    assert isinstance(validated, ValidatedAnswer) and validated.answer is answer
    assert (validated.principal_id, validated.run_id) == (PRINCIPAL, run.run_id)
    assert (validated.content_hash, validated.display_hash) == (answer.content_hash, answer.display_hash)
    with pytest.raises(DiscoveryConfigError):
        ValidatedAnswer(object(), answer, PRINCIPAL)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        validated.answer = answer  # type: ignore[misc]


def test_unvalidated_or_forged_answers_cannot_be_rendered_or_cached() -> None:
    run, answer = run_and_answer()
    cache = DisplayCache(DtClock())
    with pytest.raises((TypeError, ValueError)):
        render_grounded_block(answer)  # type: ignore[arg-type]
    with pytest.raises(CacheError):
        cache.put("u", "k", answer)  # type: ignore[arg-type]
    bare = ValidatedAnswer.__new__(ValidatedAnswer)  # built around the constructor: no authority
    with pytest.raises((TypeError, ValueError)):
        render_grounded_block(bare)
    with pytest.raises(CacheError):
        cache.put("u", "k", bare)
    assert len(cache) == 0


def test_a_validated_answer_still_works_end_to_end() -> None:
    run, answer = run_and_answer()
    validated = validator.validate(answer, run, NOW, principal_id=PRINCIPAL)
    block = render_grounded_block(validated)
    verify_verbatim(block, validated, run)
    cache = DisplayCache(DtClock())
    cache.put("u", "k", validated)
    assert cache.get("u", "k") is validated


def test_the_cache_reverifies_the_wrapper_hashes_on_get() -> None:
    run, answer = run_and_answer()
    validated = validator.validate(answer, run, NOW, principal_id=PRINCIPAL)
    cache = DisplayCache(DtClock())
    cache.put("u", "k", validated)
    corrupt = wrap_unvalidated(forged(answer, text=answer.text + " (corrupted)"))
    cache._entries["u"]["k"] = corrupt  # noqa: SLF001
    assert cache.get("u", "k") is None
    assert wrap_unvalidated(answer).display_hash == answer.display_hash


# --- item 3: the JSON-RPC response id ------------------------------------------------------------------
def test_request_ids_are_per_call_sequence_numbers() -> None:
    transport, client = client_for([ok(), ok(), ok()])
    run = new_run()
    for _ in range(3):
        client.call_tool(run, "search_places", ARGS)
    assert [r.body["id"] for r in transport.requests] == [1, 2, 3]


@pytest.mark.parametrize("response_id", [999, 0, None, True, "1", 1.0, [1], {"id": 1}])
def test_a_response_id_that_is_not_the_request_id_is_rejected(response_id) -> None:
    _, client = client_for([mcp_ok(synthetic_payload(), id=response_id)])
    with pytest.raises(MalformedToolResult):
        client.call_tool(new_run(), "search_places", ARGS)


def test_a_missing_response_id_is_rejected() -> None:
    body = {"jsonrpc": "2.0", "result": {"content": [], "structuredContent": synthetic_payload()}}
    _, client = client_for([McpResponse(status_code=200, body=body)])
    with pytest.raises(MalformedToolResult):
        client.call_tool(new_run(), "search_places", ARGS)


def test_concurrent_calls_in_one_run_get_distinct_ids_and_each_gets_its_own_reply() -> None:
    lock = threading.Lock()

    class Locked(FakeMcpTransport):
        def send(self, request, credential):
            with lock:
                return super().send(request, credential)

    transport = Locked([ok() for _ in range(12)])
    client = GroundingLiteMcpClient(transport, lambda: SECRET, config=CFG, now=lambda: NOW)
    run = new_run()
    results: list[str] = []

    def worker() -> None:
        results.append(client.call_tool(run, "search_places", ARGS).tool_call_id)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = [r.body["id"] for r in transport.requests]
    assert len(set(ids)) == 12 and sorted(ids) == list(range(1, 13)) and len(set(results)) == 12


# --- item 5: citations are best-effort, never a reason to reject -------------------------------------
@pytest.mark.parametrize("text", ["Opened in [2019].", "Years [10, 2000] here", "[2019] [2020] [99999] [0, 2019]",
                                  "Brackets [] [a] [1.5] [ 1 ] [-1] stay plain"])
def test_year_like_or_out_of_range_brackets_never_reject_the_answer(text: str) -> None:
    answer = make_answer(synthetic_payload(text=text))
    assert answer.text == text
    assert [p.cited for p in answer.places] == [False, False] or text.startswith("[2019]")
    run, built = run_and_answer(synthetic_payload(text=text))
    validator.validate(built, run, NOW, principal_id=PRINCIPAL)  # and it still validates


def test_cited_is_derived_only_from_in_range_indexes() -> None:
    assert [p.cited for p in make_answer(synthetic_payload(text="[2019] [0, 2019]")).places] == [True, False]
    assert [p.cited for p in make_answer(synthetic_payload(text="[10, 2000] and [1]")).places] == [False, True]


def test_one_based_citations_are_not_assumed_away_or_rejected() -> None:
    # TODO(verify-live): the docs say citations are zero-based. A 1-based answer would flag the
    # wrong place (best effort) but must never fail validation.
    answer = make_answer(synthetic_payload(text="First [1] and second [2]."))
    assert [p.cited for p in answer.places] == [False, True]


def test_many_brackets_are_fine() -> None:
    text = " ".join(f"[{i}]" for i in range(2_000))
    assert [p.cited for p in make_answer(synthetic_payload(text=text[:7_900])).places] == [True, True]


# --- item 6: the private-authority scan -------------------------------------------------------------------
PRIVATE = {"_mint": ({"IssuedToken"}, {"client.py", "_mint.py"}),
           "_validated": ({"ValidatedAnswer", "is_validated"}, {"validator.py", "_validated.py"})}
SECRET_NAMES = {"AUTHORITY", "VALIDATED_AUTHORITY", "mint", "mint_validated"}


def offenders(source: str, filename: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        for private, (public, owners) in PRIVATE.items():
            if filename in owners:
                continue
            if isinstance(node, ast.Import):
                found += [f"import {a.name}" for a in node.names if private in a.name]
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = {a.name for a in node.names}
                if private in module and names - public:
                    found.append(f"from {module} import {sorted(names - public)}")
                if private in names:  # from . import _mint / from app.discovery import _mint as x
                    found.append(f"from {module or '.'} import {private}")
            if isinstance(node, ast.Call) and any(
                    isinstance(c, ast.Constant) and isinstance(c.value, str) and private in c.value
                    for arg in [*node.args, *[k.value for k in node.keywords]] for c in ast.walk(arg)):
                found.append("dynamic reference")
        if filename not in {"client.py", "_mint.py", "validator.py", "_validated.py"}:
            if isinstance(node, ast.Name) and node.id in SECRET_NAMES:
                found.append(node.id)
            if isinstance(node, ast.Attribute) and node.attr in SECRET_NAMES:
                found.append(node.attr)
    return found


@pytest.mark.parametrize("source", [
    "from . import _mint", "from .. import _validated", "from app.discovery import _mint",
    "from app.discovery import _validated as v", "import app.discovery._mint as m",
    "import app.discovery._validated", "from ._mint import AUTHORITY", "from ._validated import mint_validated",
    "from app.discovery._mint import mint", "x = something.AUTHORITY", "mint('tc-1')",
    "import importlib\nimportlib.import_module('app.discovery._mint')", "__import__('app.discovery._validated')",
    "getattr(pkg, '_mint')", "importlib.import_module(name='app.discovery._mint')",
])
def test_the_scan_catches_every_way_to_reach_the_private_authorities(source: str) -> None:
    assert offenders(source, "evil.py"), source


def test_the_scan_allows_the_public_classes_and_the_owner_modules() -> None:
    assert offenders("from ._mint import IssuedToken\nfrom ._validated import ValidatedAnswer", "contracts.py") == []
    assert offenders("from . import _mint\nx = _mint.AUTHORITY", "client.py") == []
    assert offenders("from . import _validated\nx = _validated.mint_validated", "validator.py") == []


def test_no_module_under_app_reaches_a_private_authority_it_does_not_own() -> None:
    bad = {path.name: offenders(path.read_text(encoding="utf-8"), path.name)
           for path in APP_DIR.rglob("*.py")}
    assert {name: found for name, found in bad.items() if found} == {}


def test_a_tool_result_must_carry_an_issued_token() -> None:
    result = result_for()
    base = {name: getattr(result, name) for name in type(result).model_fields if name != "payload_json"}
    base["payload"] = result.payload
    for missing in (None, "token", object()):
        with pytest.raises(ValueError):
            ToolResult(**{**base, "issued": missing})
    without = {k: v for k, v in base.items() if k != "issued"}
    with pytest.raises(ValueError):
        ToolResult(**without)
    assert isinstance(result.issued, IssuedToken)


# --- item 7: cache eviction fairness -------------------------------------------------------------------------
def test_many_owners_cannot_evict_a_well_behaved_owner_below_its_share() -> None:
    cache = DisplayCache(DtClock(), max_entries=20, max_per_owner=5)
    validated = wrap_unvalidated(make_answer())
    for i in range(3):
        cache.put("honest", f"k{i}", validated)
    for owner in range(60):
        cache.put(f"flood-{owner}", "k", validated)
    kept = sum(1 for i in range(3) if cache.get("honest", f"k{i}") is not None)
    assert kept >= 1 and len(cache) <= 20
    assert cache.get("honest", "k2") is validated  # its newest entry survives


def test_when_the_global_cap_is_hit_the_biggest_owner_pays_first() -> None:
    cache = DisplayCache(DtClock(), max_entries=10, max_per_owner=10)
    validated = wrap_unvalidated(make_answer())
    for i in range(8):
        cache.put("hog", f"k{i}", validated)
    cache.put("small-a", "k", validated)
    cache.put("small-b", "k", validated)
    cache.put("small-c", "k", validated)  # over the cap: the hog loses its OLDEST entry
    assert cache.get("hog", "k0") is None and cache.get("hog", "k7") is validated
    assert all(cache.get(o, "k") is validated for o in ("small-a", "small-b", "small-c"))
    assert len(cache) == 10


def test_the_number_of_owners_is_bounded_by_the_entry_cap() -> None:
    cache = DisplayCache(DtClock(), max_entries=10)
    validated = wrap_unvalidated(make_answer())
    for owner in range(200):
        cache.put(f"owner-{owner}", "k", validated)
    assert cache.owner_count <= 10 and len(cache) <= 10


def test_purge_and_delete_use_the_per_owner_index() -> None:
    clock = DtClock()
    cache = DisplayCache(clock)
    validated = wrap_unvalidated(make_answer())
    cache.put("a", "1", validated)
    cache.put("a", "2", validated)
    cache.put("b", "1", validated)
    assert cache.delete_owner("a") == 2 and cache.owner_count == 1 and len(cache) == 1
    clock.advance(timedelta(days=2))
    assert cache.purge() == 1 and cache.owner_count == 0


# --- item 8: strict JSON ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    '{"a": 1, "a": 2}', '{"a": {"b": 1, "b": 2}}', '[{"x": 1, "x": 1}]',
    '{"a": 1}'.encode("utf-16"), '{"a": 1}'.encode("utf-16-le"), '{"a": 1}'.encode("utf-16-be"),
    '{"a": 1}'.encode("utf-32"), b"\xef\xbb\xbf" + b'{"a": 1}', "﻿" + '{"a": 1}',
    b'{"a": "\xff"}', b'{"a": "\xc3("}',
])
def test_strict_loads_rejects_duplicate_keys_other_encodings_and_boms(raw) -> None:
    with pytest.raises(ValueError):
        strict_loads(raw)


def test_strict_loads_accepts_plain_utf8_bytes_and_text() -> None:
    assert strict_loads('{"a": "é"}'.encode()) == {"a": "é"}
    assert strict_loads('{"a": [1, 2]}') == {"a": [1, 2]}


def test_discovery_bodies_with_duplicate_keys_or_odd_encodings_yield_no_body() -> None:
    import httpx

    from app.discovery import McpRequest

    transport = FakeMcpTransport([ok()])
    GroundingLiteMcpClient(transport, lambda: SECRET, config=CFG, now=lambda: NOW).call_tool(
        new_run(), "search_places", ARGS)
    sent: McpRequest = transport.requests[0]
    for content, kind in ((b'{"a": 1, "a": 2}', "application/json"), ('{"a": 1}'.encode("utf-16"), "application/json"),
                          (b"\xef\xbb\xbf" + b'{"a": 1}', "application/json"),
                          (b'data: {"a": 1, "a": 2}\n\n', "text/event-stream")):
        http = HttpxMcpTransport.for_tests(httpx.MockTransport(
            lambda r, c=content, k=kind: httpx.Response(200, content=c, headers={"content-type": k})))
        assert http.send(sent, __import__("app.gateway.openrouter", fromlist=["Credential"]).Credential(SECRET)).body is None


def test_gateway_tool_arguments_and_outputs_reject_duplicate_keys() -> None:
    from gateway_fakes import FakeTransport, completion_response, tool_call_entry
    from test_gateway_support import SEARCH_TOOL, make_gateway

    entry = tool_call_entry("search_places", {})
    entry["function"]["arguments"] = '{"q": "a", "q": "b"}'
    reply = completion_response(None, finish_reason="tool_calls", tool_calls=[entry])
    with pytest.raises(MalformedOutputError):
        make_gateway(FakeTransport([reply])).generate(
            make_request(allowed_tools=(SEARCH_TOOL,), allow_repair=False))
    dup = completion_response('{"answer": "ok", "answer": "x", "count": 1}')
    with pytest.raises(MalformedOutputError):
        make_gateway(FakeTransport([dup])).generate(make_request(allow_repair=False))


# --- item 10: SSE event types, unterminated events, search attribution urls -------------------------------------
PAYLOAD = json.dumps({"a": 1})


def test_sse_honors_the_event_type() -> None:
    assert parse_single_sse_event(f"event: message\ndata: {PAYLOAD}\n\n".encode()) == {"a": 1}
    assert parse_single_sse_event(f"data: {PAYLOAD}\n\n".encode()) == {"a": 1}
    for kind in ("error", "ping", "endpoint", "message2", "Message", ""):
        assert parse_single_sse_event(f"event: {kind}\ndata: {PAYLOAD}\n\n".encode()) is None, kind


def test_sse_discards_a_final_unterminated_event() -> None:
    assert parse_single_sse_event(f"data: {PAYLOAD}".encode()) is None
    assert parse_single_sse_event(f"data: {PAYLOAD}\n".encode()) is None
    assert parse_single_sse_event(f"data: {PAYLOAD}\n\ndata: {PAYLOAD}".encode()) == {"a": 1}  # tail dropped


@pytest.mark.parametrize("uri", [
    "https://www.google.com/search?q=x&btnI=1", "https://www.google.com/search?btnI=1&q=x",
    "https://www.google.com/search?q=a&q=b", "https://www.google.com/search?btnI=1",
    "https://www.google.com/search?q=", "https://www.google.com/search", "https://www.google.com/search?Q=x",
    "https://www.google.com/search?q=x&", "https://www.google.com/search?q=x&source=1",
])
def test_search_attribution_urls_must_have_exactly_one_q_parameter(uri: str) -> None:
    with pytest.raises(ValueError):
        check_attribution_url(uri)


def test_a_single_q_parameter_is_accepted() -> None:
    check_attribution_url("https://www.google.com/search?q=place_id:SYNTH_PLACE_0001")
    check_attribution_url("https://www.google.com/search?q=Invented+Spot")


# --- item 13: internal-only schema keywords never leave the process -------------------------------------------
def test_internal_keywords_are_stripped_from_outbound_tool_schemas() -> None:
    spec = TOOL_SPECS[ToolId.LOOKUP_WEATHER]
    assert "x_exactly_one_of" in json.dumps(spec.parameters)  # still enforced server-side
    cleaned = outbound_schema(spec.parameters)
    assert "x_" not in json.dumps(cleaned) and cleaned["properties"]["location"]["properties"]
    request = make_request(call_role=CallRole.DISCOVER, privacy_class=PrivacyClass.MAPS_CONTENT,
                           allowed_tools=(TOOL_SPECS[ToolId.LOOKUP_WEATHER], TOOL_SPECS[ToolId.COMPUTE_ROUTES]))
    body = build_body(request, make_policy())
    assert "x_" not in json.dumps(body["tools"]) and len(body["tools"]) == 2
    assert "x_exactly_one_of" in json.dumps(request.allowed_tools[0].parameters)  # the spec is untouched


# --- item 11 (cheap part): invisible fillers ---------------------------------------------------------------------
@pytest.mark.parametrize("char", ["ㅤ", "ᅟ", "⠀", "͏", " ", " "])
def test_invisible_fillers_are_rejected_in_titles_and_the_summary(char: str) -> None:
    title_payload = {"summary": "t", "places": [synthetic_place(1) | {"attribution": {
        "title": f"Ca{char}fe", "url": "https://maps.app.goo.gl/SYNTH0001"}}]}
    with pytest.raises(GroundedParseError):
        make_answer(title_payload)
    with pytest.raises(GroundedParseError):
        make_answer(synthetic_payload(text=f"quiet{char}spot"))


def test_persian_titles_with_zwnj_are_rejected_but_the_same_text_in_the_summary_is_allowed() -> None:
    # Decision kept: ZWNJ/ZWJ are allowed in the summary only. A short-field title such as
    # "می‌خواهم" (with ZWNJ U+200C) is therefore rejected; the same title without it is fine.
    with_zwnj = "می‌خواهم"
    without = with_zwnj.replace("‌", "")
    payload = lambda title: {"summary": "t", "places": [synthetic_place(1) | {"attribution": {  # noqa: E731
        "title": title, "url": "https://maps.app.goo.gl/SYNTH0001"}}]}
    with pytest.raises(GroundedParseError):
        make_answer(payload(with_zwnj))
    assert make_answer(payload(without)).places[0].attribution_title == without
    assert make_answer(synthetic_payload(text=f"Persian {with_zwnj} [0]")).text.endswith("[0]")
    assert ECHO and TTLS and parse_grounded_answer  # fixtures referenced for linters
