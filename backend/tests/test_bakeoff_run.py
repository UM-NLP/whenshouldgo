"""Bake-off harness: spend cap, resume, dry run through the real gateway stack, key handling,
TLS wiring and result-file hygiene. Offline: only httpx.MockTransport, never a socket."""
from __future__ import annotations

import json
import ssl
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.frame import FRAME_SYSTEM_PROMPT
from app.gateway import HttpxTransport
from evals.frame_bakeoff import fixtures as fixture_lib
from evals.frame_bakeoff import run, score
from evals.frame_bakeoff.dryrun import DryRunModel

SECRET = "sk-or-test-NOT-A-REAL-KEY-9f8e7d6c5b4a"
REPO = Path(__file__).resolve().parents[2]


def read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def fake_call_factory(cost: str = "0.4", interrupt_on: int | None = None):
    calls: list[tuple[str, int]] = []

    def fake_call(stack: Any, fixture: dict[str, Any], repeat: int) -> dict[str, Any]:
        calls.append((fixture["id"], repeat))
        if interrupt_on is not None and len(calls) == interrupt_on:
            raise KeyboardInterrupt
        return {"fixture_id": fixture["id"], "category": fixture["category"], "repeat": repeat,
                "outcome": "OK", "model_calls": 1, "repairs": 0, "latency_ms": 1.0,
                "input_tokens": 300, "output_tokens": 100, "cost_usd": cost}

    fake_call.calls = calls  # type: ignore[attr-defined]
    return fake_call


def go(argv: list[str], **kwargs: Any) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = run.main(argv, emit=lines.append, **kwargs)
    return code, lines


# --- dry run end to end through the REAL gateway stack ----------------------------------
def test_dry_run_goes_through_the_real_gateway_and_scores(tmp_path: Path) -> None:
    out = tmp_path / "dry.jsonl"
    code, lines = go(["--dry-run", "--repeats", "1", "--out", str(out), "--report-every", "50"])
    assert code == 0
    records = read(out)
    assert len(records) == 150 and len({r["fixture_id"] for r in records}) == 150
    assert {r["outcome"] for r in records} <= {"OK", "TRUNCATED", "VALIDATION_FAILED"}
    ok = [r for r in records if r["outcome"] == "OK"]
    assert ok and all(r["served_provider"] == "dryrun" and r["cost_basis"] == "reported" for r in ok)
    assert all(r["input_tokens"] > 0 and r["output_tokens"] > 0 for r in ok)
    assert all(r["repairs"] == 0 and r["model_calls"] == 1 for r in records)  # no FRAME repair
    assert {r["turn_outcome"] for r in records} <= set(run.TURN_OUTCOMES)
    assert all(r["turn_outcome"] == "MALFORMED" for r in records if r["outcome"] != "OK")
    assert all(r["plan"] and r["patch"] and r["turn_outcome"] for r in ok)
    # cost comes from the gateway turn and the non-placeholder price table (0.10 / 0.40 per M)
    first = ok[0]
    expected = (Decimal("0.10") * first["input_tokens"]
                + Decimal("0.40") * first["output_tokens"]) / 1_000_000
    assert Decimal(first["cost_usd"]) == expected.quantize(Decimal("0.00000001"))
    # progress lines and a final cost line in USD and CAD
    assert any(line.startswith("[50/150]") for line in lines)
    assert lines[-2].startswith("COMPLETE: ") and "CA$" in lines[-2] and "US$" in lines[-2]
    # the planted slips each reach a different production branch
    by_id = {r["fixture_id"]: r for r in records}
    assert by_id["other-010"]["outcome"] == "VALIDATION_FAILED"            # state fields: MALFORMED
    assert by_id["other-009"]["turn_outcome"] == "CLARIFY_OTHER"          # OTHER + search: ignored
    assert by_id["other-009"]["plan"]["outcome"] == "ignored" and by_id["other-009"]["search"]
    assert by_id["inj-005"]["turn_outcome"] == "CLARIFY_QUERY_REJECTED"   # injected query degrades
    assert by_id["inj-001"]["turn_outcome"] == "DISCOVERY_SEARCH"         # user-written word passes
    assert by_id["inj-001"]["requirements"]["forbidden_words"] is False
    assert by_id["inj-002"]["turn_outcome"] == "DISCOVERY_SEARCH"         # forbidden word leaks
    assert by_id["inj-002"]["requirements"]["forbidden_words"] is False
    assert by_id["disc-020"]["plan"]["area_dropped"] is True              # invented area dropped
    assert by_id["edge-006"]["plan"]["area_dropped"] is True              # duration-as-area dropped
    assert by_id["inj-007"]["patch"]["facts_dropped"] == 1                # link fact dropped
    cases = fixture_lib.load_fixtures()
    evals, unknown = score.evaluate_all(records, cases)
    assert unknown == 0 and len(evals) == 150
    text = score.render_report("dry", records, cases)
    assert "Confusion: gold action" in text and "Injection failures" in text
    assert any("forbidden_word:unlimited" in e.violations for e in evals if e.fixture_id == "inj-002")


def test_dry_run_needs_no_key_and_ignores_the_environment(tmp_path: Path) -> None:
    code, _ = go(["--dry-run", "--limit", "2", "--repeats", "1", "--out", str(tmp_path / "o.jsonl")],
                 env={})
    assert code == 0


def test_dry_run_is_deterministic_for_a_seed(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for out in (a, b):
        go(["--dry-run", "--limit", "20", "--repeats", "2", "--seed", "7", "--out", str(out)])
    strip = lambda rs: [{k: v for k, v in r.items() if k != "latency_ms"} for r in rs]
    assert strip(read(a)) == strip(read(b))


# --- selection ----------------------------------------------------------------------------
def test_plan_tasks_is_seeded_filtered_and_repeat_major() -> None:
    cases = fixture_lib.load_fixtures()
    one = run.plan_tasks(cases, 2, limit=5, seed=3)
    again = run.plan_tasks(cases, 2, limit=5, seed=3)
    other = run.plan_tasks(cases, 2, limit=5, seed=4)
    assert [(c["id"], r) for c, r in one] == [(c["id"], r) for c, r in again]
    assert [c["id"] for c, _ in one] != [c["id"] for c, _ in other]
    assert [r for _, r in one] == [1] * 5 + [2] * 5
    inj = run.plan_tasks(cases, 1, categories={"injection"})
    assert len(inj) == 20 and {c["category"] for c, _ in inj} == {"injection"}


def test_unknown_category_is_refused(tmp_path: Path) -> None:
    code, lines = go(["--dry-run", "--categories", "poetry", "--out", str(tmp_path / "o.jsonl")])
    assert code == run.EXIT_USAGE and "category" in lines[-1]


# --- spend cap and call cap -----------------------------------------------------------------
def test_spend_cap_aborts_before_the_next_call(tmp_path: Path) -> None:
    out, fake = tmp_path / "o.jsonl", fake_call_factory("0.4")
    code, lines = go(["--dry-run", "--repeats", "1", "--max-spend-usd", "1.0", "--out", str(out)],
                     call=fake)
    assert code == run.EXIT_SPEND_CAP
    assert len(fake.calls) == 3  # 0.4, 0.8, then 1.2 >= 1.0: stop; never a fourth call
    assert len(read(out)) == 3
    assert lines[-2].startswith("SPEND_CAP: ") and "US$1.200000" in lines[-2]


def test_spend_cap_is_checked_before_the_first_call(tmp_path: Path) -> None:
    fake = fake_call_factory("0.4")
    code, _ = go(["--dry-run", "--max-spend-usd", "0", "--out", str(tmp_path / "o.jsonl")], call=fake)
    assert code == run.EXIT_SPEND_CAP and fake.calls == []


def test_spend_cap_counts_what_a_resumed_file_already_cost(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    go(["--dry-run", "--repeats", "1", "--max-spend-usd", "0.9", "--out", str(out)],
       call=fake_call_factory("0.4"))
    fake = fake_call_factory("0.4")
    code, _ = go(["--dry-run", "--repeats", "1", "--max-spend-usd", "0.9", "--resume",
                  "--out", str(out)], call=fake)
    assert code == run.EXIT_SPEND_CAP and fake.calls == []  # 3 prior calls already cost 1.2


def test_max_calls_aborts(tmp_path: Path) -> None:
    fake = fake_call_factory("0.0")
    code, lines = go(["--dry-run", "--repeats", "1", "--max-calls", "4", "--out",
                      str(tmp_path / "o.jsonl")], call=fake)
    assert code == run.EXIT_MAX_CALLS and len(fake.calls) == 4
    assert lines[-2].startswith("MAX_CALLS: ")


def test_concurrent_run_completes_and_respects_the_cap(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    code, _ = go(["--dry-run", "--limit", "12", "--repeats", "1", "--concurrency", "3", "--out",
                  str(out)])
    assert code == 0 and len(read(out)) == 12
    out2, fake = tmp_path / "o2.jsonl", fake_call_factory("0.5")
    code, _ = go(["--dry-run", "--repeats", "1", "--concurrency", "2", "--max-spend-usd", "1.0",
                  "--out", str(out2)], call=fake)
    assert code == run.EXIT_SPEND_CAP and 2 <= len(read(out2)) <= 3  # overshoot <= in-flight


# --- resume and Ctrl-C ------------------------------------------------------------------------
def test_ctrl_c_flushes_prints_totals_and_resume_finishes(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    args = ["--dry-run", "--limit", "6", "--repeats", "1", "--seed", "1", "--out", str(out)]
    code, lines = go(args, call=fake_call_factory(interrupt_on=3))
    assert code == run.EXIT_INTERRUPTED
    assert len(read(out)) == 2 and lines[-2].startswith("INTERRUPTED: ") and "model_calls=2" in lines[-2]
    fake = fake_call_factory()
    code, lines = go([*args, "--resume"], call=fake)
    assert code == 0 and len(fake.calls) == 4  # only the missing pairs
    ids = [(r["fixture_id"], r["repeat"]) for r in read(out)]
    assert len(ids) == 6 and len(set(ids)) == 6
    assert "resumed=2" in lines[0]


def test_resume_skips_done_pairs_and_repairs_a_torn_last_line(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    args = ["--dry-run", "--limit", "3", "--repeats", "2", "--seed", "2", "--out", str(out)]
    go(args, call=fake_call_factory())
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    out.write_text("\n".join(lines[:4]) + '\n{"fixture_id": "torn", "rep', encoding="utf-8")
    fake = fake_call_factory()
    code, _ = go([*args, "--resume"], call=fake)
    assert code == 0 and len(fake.calls) == 2
    good = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.startswith('{"fixture_id": "') and x.endswith("}")]
    assert len({(r["fixture_id"], r["repeat"]) for r in good}) == 6


def test_existing_output_without_resume_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text('{"fixture_id": "x", "repeat": 1}\n', encoding="utf-8")
    code, lines = go(["--dry-run", "--out", str(out)], call=fake_call_factory())
    assert code == run.EXIT_USAGE and "--resume" in lines[-1]
    assert out.read_text(encoding="utf-8") == '{"fixture_id": "x", "repeat": 1}\n'


# --- failed calls keep their cost -----------------------------------------------------------------
def stack_for(handler: Any, price_in: str = "1", price_out: str = "2") -> run.Stack:
    transport = HttpxTransport.for_tests(httpx.MockTransport(handler))
    return run.build_stack("vendor/model-x", "prov1", Decimal(price_in), Decimal(price_out),
                           SECRET, transport)


def one_fixture() -> dict[str, Any]:
    return fixture_lib.load_fixtures()[0]


def test_a_truncated_call_is_charged_from_the_error_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"finish_reason": "length",
                             "message": {"role": "assistant", "content": '{"patch": '}}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 1200, "total_tokens": 1600},
                "provider": "prov1"}
        return httpx.Response(200, json=body)

    record = run.run_one(stack_for(handler), one_fixture(), 1)
    assert record["outcome"] == "TRUNCATED" and record["finish_reason"] == "LENGTH"
    assert record["error_kind"] == "TRUNCATION" and record["cost_basis"] == "reported"
    # FRAME has no repair now: one call, charged from the error usage
    assert record["model_calls"] == 1 and record["intent"] is None
    assert record["turn_outcome"] == "MALFORMED"
    assert (record["input_tokens"], record["output_tokens"]) == (400, 1200)
    assert Decimal(record["cost_usd"]) == Decimal("0.0028")  # (1*400 + 2*1200) / 1e6


def test_a_provider_error_is_charged_never_free() -> None:
    record = run.run_one(stack_for(lambda r: httpx.Response(500, json={"error": {}})),
                         one_fixture(), 1)
    assert record["outcome"] == "GATEWAY_ERROR" and record["error_kind"]
    assert Decimal(record["cost_usd"]) > 0 and record["input_tokens"] > 0
    assert record["output_tokens"] > 0 and record["model_calls"] >= 1


class _RaisingGateway:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def generate(self, request: Any) -> Any:
        raise self._error


def _stack_raising(error: Exception) -> run.Stack:
    base = stack_for(lambda r: httpx.Response(200, json={}))
    return run.Stack(base.config, base.prices, _RaisingGateway(error))  # type: ignore[arg-type]


def test_an_error_without_usage_falls_back_to_the_worst_case_reservation() -> None:
    from app.gateway.errors import ProviderUnavailableError

    record = run.run_one(_stack_raising(ProviderUnavailableError(calls=1)), one_fixture(), 1)
    assert record["cost_basis"] == "worst_case" and Decimal(record["cost_usd"]) > 0
    assert record["input_tokens"] > 0 and record["model_calls"] == 1


def test_an_error_before_anything_was_sent_costs_nothing() -> None:
    from app.gateway.errors import GatewayConfigError, ProviderRateLimitError

    nothing = run.run_one(_stack_raising(GatewayConfigError()), one_fixture(), 1)
    assert nothing["cost_basis"] == "none" and Decimal(nothing["cost_usd"]) == 0
    err = ProviderRateLimitError(calls=1)
    err.rejected_before_processing = True  # a 429/503 that the provider refused to process
    free = run.run_one(_stack_raising(err), one_fixture(), 1)
    assert free["cost_basis"] == "none" and Decimal(free["cost_usd"]) == 0 and free["model_calls"] == 1


def test_an_unexpected_exception_is_recorded_by_class_name_only() -> None:
    record = run.run_one(_stack_raising(RuntimeError("secret detail sk-xyz")), one_fixture(), 1)
    assert record["outcome"] == "INTERNAL_ERROR" and record["error_kind"] == "RuntimeError"
    assert "secret" not in json.dumps(record)


def test_a_malformed_reply_is_validation_failed_with_its_cost() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "nope"}}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 5, "total_tokens": 305},
                "provider": "prov1"}
        return httpx.Response(200, json=body)

    record = run.run_one(stack_for(handler), one_fixture(), 1)
    assert record["outcome"] == "VALIDATION_FAILED" and record["error_kind"] == "MALFORMED_OUTPUT"
    # no retry (max_attempts=1) and no repair (allow_repair=False): exactly one paid call
    assert record["model_calls"] == 1 and Decimal(record["cost_usd"]) > 0
    assert record["turn_outcome"] == "MALFORMED"


# --- key handling ----------------------------------------------------------------------------------
def test_missing_key_refuses_before_any_transport_is_built(tmp_path: Path) -> None:
    def boom(mode: str) -> HttpxTransport:
        raise AssertionError("no transport may be built without a key")

    code, lines = go(["--model", "vendor/m", "--provider", "p", "--price-in", "1", "--price-out", "2",
                      "--out", str(tmp_path / "o.jsonl")], env={}, transport_factory=boom)
    assert code == run.EXIT_USAGE and "OPENROUTER_API_KEY" in lines[-1]
    assert not (tmp_path / "o.jsonl").exists()


def test_live_mode_requires_model_provider_and_prices(tmp_path: Path) -> None:
    code, lines = go(["--out", str(tmp_path / "o.jsonl")], env={"OPENROUTER_API_KEY": SECRET})
    assert code == run.EXIT_USAGE and "required" in lines[-1]


def test_a_malformed_key_is_refused_without_echoing_it() -> None:
    with pytest.raises(run.RunRefused) as exc:
        run.load_key({"OPENROUTER_API_KEY": "has space\x00"})
    assert "has space" not in str(exc.value)


def test_env_file_reads_only_the_one_variable(tmp_path: Path) -> None:
    env_file = tmp_path / "x.env"
    env_file.write_text(
        "# comment\nOTHER_SECRET=do-not-read\nexport OPENROUTER_API_KEY=\"" + SECRET + "\"\n"
        "ANOTHER=zzz\n", encoding="utf-8")
    assert run.parse_env_file(env_file) == SECRET
    assert run.parse_env_file(env_file, "OTHER_SECRET") == "do-not-read"  # only on explicit ask
    assert run.load_key({}, str(env_file)) == SECRET
    only_other = tmp_path / "y.env"
    only_other.write_text("OTHER_SECRET=abc\n", encoding="utf-8")
    with pytest.raises(run.RunRefused):
        run.load_key({}, str(only_other))
    with pytest.raises(run.RunRefused) as exc:
        run.load_key({}, str(tmp_path / "missing.env"))
    assert "missing.env" not in str(exc.value)


def test_the_key_reaches_the_provider_but_never_output_or_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = fixture_lib.load_fixtures()
    model = DryRunModel(cases, "prov1")
    auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth.append(request.headers.get("authorization", ""))
        return model(request)

    factory = lambda mode: HttpxTransport.for_tests(httpx.MockTransport(handler))  # noqa: E731
    out = tmp_path / "o.jsonl"
    code = run.main(["--model", "vendor/model-x", "--provider", "prov1", "--price-in", "1",
                     "--price-out", "2", "--limit", "5", "--repeats", "1", "--out", str(out)],
                    env={"OPENROUTER_API_KEY": SECRET}, transport_factory=factory)
    assert code == 0 and auth and set(auth) == {f"Bearer {SECRET}"}
    printed = capsys.readouterr()
    assert SECRET not in printed.out and SECRET not in printed.err
    assert SECRET not in out.read_text(encoding="utf-8")
    assert "LIVE ssl=strict" in printed.out


# --- TLS wiring --------------------------------------------------------------------------------------
def test_ssl_mode_defaults_to_strict() -> None:
    assert run.build_parser().parse_args(["--out", "x"]).ssl_mode == "strict"


def test_strict_mode_keeps_the_default_context_and_the_gateway_client(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(run.HttpxTransport, "for_tests", lambda *a, **k: calls.append(a))
    ctx = run.build_ssl_context("strict")
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.verify_flags == ssl.create_default_context().verify_flags
    assert isinstance(run.build_transport("strict"), HttpxTransport)
    assert calls == []  # the strict path never injects a custom network layer


def test_nonstrict_mode_relaxes_only_the_strict_flag_and_injects_through_for_tests(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_http_transport(*, verify: Any) -> httpx.MockTransport:
        seen["verify"] = verify
        return httpx.MockTransport(lambda r: httpx.Response(200, json={}))

    monkeypatch.setattr(run.httpx, "HTTPTransport", fake_http_transport)
    transport = run.build_transport("os-store-nonstrict")
    assert isinstance(transport, HttpxTransport)
    ctx = seen["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT
    default = ssl.create_default_context().verify_flags
    assert ctx.verify_flags == default & ~ssl.VERIFY_X509_STRICT  # nothing else was relaxed
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_unknown_ssl_mode_is_refused() -> None:
    with pytest.raises(run.RunRefused):
        run.build_ssl_context("insecure")


# --- hygiene of the results file ---------------------------------------------------------------------
def test_results_hold_no_raw_prompts_and_no_key(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    go(["--dry-run", "--repeats", "1", "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    cases = fixture_lib.load_fixtures()
    long_messages = [c["message"] for c in cases if len(c["message"]) >= 25]
    assert len(long_messages) > 100
    for message in long_messages:
        assert message not in text and json.dumps(message, ensure_ascii=False)[1:-1] not in text
    assert FRAME_SYSTEM_PROMPT[:60] not in text and "WORKSPACE_STATE_JSON" not in text
    assert run.DRY_RUN_KEY not in text and "Bearer" not in text and "sk-or-" not in text
    for record in read(out):
        assert "message" not in record and "prompt" not in record and "context" not in record
        patch = record["patch"] or {}  # fact keys only, never values
        assert set(patch) <= {"outcome", "reason", "facts_dropped", "fact_keys"}


def test_results_directory_is_git_ignored() -> None:
    assert "backend/evals/frame_bakeoff/results/" in (REPO / ".gitignore").read_text(encoding="utf-8")


def test_the_harness_never_opens_a_real_socket(monkeypatch, tmp_path: Path) -> None:
    import socket

    def no_socket(*a: Any, **k: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    code, _ = go(["--dry-run", "--limit", "5", "--repeats", "1", "--out", str(tmp_path / "o.jsonl")])
    assert code == 0
