from app.domain import Confirmation, DomainClass, Principal, PrincipalKind
from app.entitlements import PILOT_POLICY
from app.orchestrator import TurnOrchestrator
from app.guard import OUT_OF_SCOPE_REPLY, classify

GUEST = Principal("guest-1", PrincipalKind.GUEST, PILOT_POLICY.version)


def test_unrelated_coding_request_uses_fixed_redirect_without_tools() -> None:
    result = TurnOrchestrator(dev_scaffold=True).run_turn(GUEST, "Write Python code", "one")
    assert result.domain == DomainClass.OUT_OF_SCOPE
    assert result.reply == OUT_OF_SCOPE_REPLY
    assert result.tools == ()


def test_travel_turn_creates_a_workspace_with_editable_assumption() -> None:
    result = TurnOrchestrator(dev_scaffold=True).run_turn(GUEST, "I want something to eat", "one")
    assert result.domain == DomainClass.TRAVEL
    assert result.workspace is not None
    assert result.workspace.assumptions[0].confirmation == Confirmation.UNCONFIRMED
    assert "maps_grounded_discovery" in result.tools


def test_mixed_request_continues_as_travel_and_keeps_only_the_outing() -> None:
    # The model never writes reply text, so the off-topic part cannot be answered: the message
    # is not refused and FRAME extracts only the travel search.
    result = TurnOrchestrator(dev_scaffold=True).run_turn(GUEST, "Write code then plan a restaurant trip", "one")
    assert result.domain == DomainClass.TRAVEL
    assert "maps_grounded_discovery" in result.tools


def test_orchestrator_rejects_bare_principal_id() -> None:
    import pytest

    with pytest.raises(TypeError):
        TurnOrchestrator(dev_scaffold=True).run_turn("guest-1", "I want something to eat", "one")  # type: ignore[arg-type]


def test_classifies_app_help() -> None:
    assert classify("How does this app handle privacy?") == DomainClass.APP_HELP
