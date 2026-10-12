from app.domain import Intent, Principal, PrincipalKind
from app.entitlements import PILOT_POLICY
from app.orchestrator import TurnOrchestrator
from app.policy import OUT_OF_SCOPE_REPLY, classify

GUEST = Principal("guest-1", PrincipalKind.GUEST, PILOT_POLICY.version)


def test_unrelated_coding_request_uses_fixed_redirect_without_tools() -> None:
    result = TurnOrchestrator().run_turn(GUEST, "Write Python code", "one")
    assert result.intent == Intent.OUT_OF_SCOPE
    assert result.reply == OUT_OF_SCOPE_REPLY
    assert result.tools == ()


def test_travel_turn_creates_a_workspace_with_editable_assumption() -> None:
    result = TurnOrchestrator().run_turn(GUEST, "I want something to eat", "one")
    assert result.intent == Intent.TRAVEL
    assert result.workspace is not None
    assert result.workspace.assumptions[0].confirmed is False
    assert "maps_grounded_discovery" in result.tools


def test_mixed_request_does_not_receive_tools() -> None:
    result = TurnOrchestrator().run_turn(GUEST, "Write code then plan a restaurant trip", "one")
    assert result.intent == Intent.MIXED
    assert result.tools == ()


def test_orchestrator_rejects_bare_principal_id() -> None:
    import pytest

    with pytest.raises(TypeError):
        TurnOrchestrator().run_turn("guest-1", "I want something to eat", "one")  # type: ignore[arg-type]


def test_classifies_app_help() -> None:
    assert classify("How does this app handle privacy?") == Intent.APP_HELP
