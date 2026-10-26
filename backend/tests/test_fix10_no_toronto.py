"""Fix 10 (owner decision): no place is assumed anywhere in server code. The FRAME prompt is
place-neutral, no workspace is born with a planning area, the fixed replies name no city and the
workspace time zone defaults to UTC. ``planning_area`` stays a RESERVED key the model cannot write."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, canary_payload, frame_turn

import app
import app.frame as frame
from app import pipeline
from app.domain import TripWorkspace
from app.frame import FrameIntent
from app.frame import FrameProposal, ProposedFact, build_patch
from app.patching import PatchRejected, apply_patch_checked

MSG = "Something fun for the kids on Saturday morning"
FR_MSG = "Je cherche un endroit pour les enfants ce soir"


def test_the_prompt_is_place_neutral_and_states_the_language_rule() -> None:
    prompt = frame.FRAME_SYSTEM_PROMPT
    assert "Toronto" not in prompt and "GTA" not in prompt
    assert "same-day outing assistant" in prompt
    assert "ISO 639-1" in prompt and "'en'" in prompt
    for rule in ("bare place name", "omit it if the user named no place", "never invent facts"):
        assert rule in prompt
    assert len(prompt) < 1_300


def test_the_prompt_hash_is_repinned_deliberately() -> None:
    digest = hashlib.sha256(frame.FRAME_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert frame.FRAME_PROMPT_SHA256 == digest
    assert not digest.startswith("fb151a75")  # the previous (Toronto/GTA) prompt


def test_no_workspace_is_born_with_a_place() -> None:
    workspace = TripWorkspace(id="ws-1", owner_id="u-1")
    assert workspace.timezone == "UTC"
    proposal = FrameProposal(intent=FrameIntent.OTHER, search=None)
    updated, _ = apply_patch_checked(workspace, build_patch(workspace, proposal, search_ok=False))
    assert updated.facts == []


def test_planning_area_is_reserved_the_model_can_never_write_it() -> None:
    workspace = TripWorkspace(id="ws-1", owner_id="u-1")
    proposal = FrameProposal(intent=FrameIntent.OTHER, facts=[
        ProposedFact(key="planning_area", value="Paris"), ProposedFact(key="party", value="two")])
    with pytest.raises(PatchRejected):  # the whole proposal is refused, as before
        apply_patch_checked(workspace, build_patch(workspace, proposal, search_ok=False))


def test_the_state_message_shows_no_planning_area_on_a_fresh_workspace() -> None:
    message = frame._state_message(TripWorkspace(id="ws-1", owner_id="u-1"))  # noqa: SLF001
    assert "planning_area" not in message and "Toronto" not in message


def test_the_other_reply_names_no_place_and_has_a_french_variant() -> None:
    reply = pipeline.REPLY_OTHER
    assert isinstance(reply, dict) and set(reply) == {"en", "fr"}
    for text in reply.values():
        assert "Toronto" not in text and "GTA" not in text
    assert "What area are you starting from?" in reply["en"]
    assert "Union Station" in reply["en"] and "Vieux-Port" in reply["fr"]


def _env(*turns):
    config = make_config()
    gateway = ScriptedGateway([t(config) for t in turns], config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config)
    maps.inner._now = env.clock  # noqa: SLF001
    return env


@pytest.mark.parametrize("message,language", [(MSG, "en"), (FR_MSG, "fr")])
def test_other_turns_use_the_message_language_and_create_no_planning_fact(message, language) -> None:
    env = _env(lambda c: frame_turn(c, intent=FrameIntent.OTHER))
    reply = env.post({"message": message}).json()["reply"]
    assert reply == pipeline.REPLY_OTHER[language]
    workspace = env.orchestrator.workspaces[("guest-a", "default")]
    assert workspace.facts == []


def test_the_dev_scaffold_names_no_place_and_seeds_no_fact() -> None:
    env = Env(gateway=None, discovery=None)
    body = env.post({"message": "Plan a quiet restaurant this evening"}).json()
    assert "Toronto" not in body["reply"] and "GTA" not in body["reply"]
    workspace = env.orchestrator.workspaces[("guest-a", "default")]
    assert workspace.facts == []


def test_no_server_code_names_a_place() -> None:
    root = Path(app.__file__).parent
    offenders = [str(p.name) for p in root.rglob("*.py")
                 if re.search(r"Toronto|\bGTA\b", p.read_text(encoding="utf-8"))]
    assert offenders == []
