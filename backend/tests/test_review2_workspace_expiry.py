"""Review finding 7 (decision 28): a registered user's server workspace and plan versions expire
after 90 inactive days; every write refreshes the expiry."""
from __future__ import annotations

import pytest
from history_support import PLACE, PLACE2, HEnv

from app.entitlements import REGISTERED_WORKSPACE_INACTIVITY_DAYS


@pytest.fixture(params=["memory", "fake-firestore"])
def env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> HEnv:
    return HEnv(request.param, monkeypatch)


def select(env: HEnv, place: str, base: int, key: str):
    edit = {"base_version": base, "upsert_facts": [{"key": "selected_place_id", "value": place}]}
    return env.call("PATCH", "/v1/workspace", "a", edit, key)


def stored(env: HEnv):
    rt = env.runtime
    return (rt.stores.workspaces.get("reg-a", "default", env.clock()),
            rt.plan_stores.plans.latest("reg-a", "x", env.clock()))


def test_the_constant_is_ninety_days() -> None:
    assert REGISTERED_WORKSPACE_INACTIVITY_DAYS == 90


def test_a_registered_workspace_and_its_plans_expire_after_90_inactive_days(env: HEnv) -> None:
    done = select(env, PLACE, 0, "edit-key-0001")
    assert done.status_code == 200 and done.json()["plan_versions"] == [1]
    ws_id = done.json()["id"]
    env.clock.advance(days=89)
    assert env.runtime.stores.workspaces.get("reg-a", "default", env.clock()) is not None
    assert env.runtime.plan_stores.plans.latest("reg-a", ws_id, env.clock()) is not None
    env.clock.advance(days=2)
    assert env.runtime.stores.workspaces.get("reg-a", "default", env.clock()) is None
    assert env.runtime.plan_stores.plans.latest("reg-a", ws_id, env.clock()) is None


def test_a_write_refreshes_the_expiry(env: HEnv) -> None:
    first = select(env, PLACE, 0, "edit-key-0001")
    version = first.json()["version"]
    env.clock.advance(days=60)
    assert select(env, PLACE2, version, "edit-key-0002").status_code == 200
    env.clock.advance(days=60)  # 120 days after the first write, 60 after the refresh
    assert env.runtime.stores.workspaces.get("reg-a", "default", env.clock()) is not None
    env.clock.advance(days=31)
    assert env.runtime.stores.workspaces.get("reg-a", "default", env.clock()) is None
