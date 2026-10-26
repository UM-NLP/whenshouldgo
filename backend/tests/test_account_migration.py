"""Item 10: the explicit, previewed, resumable merge of a guest into an existing account.

Over the in-memory stores and the Firestore adapters on the in-process fake.
"""
from __future__ import annotations

import threading

import pytest
from account_support import H, Env, user_fact, workspace

from app.migration import ManifestEntry, WorkspaceSource


@pytest.fixture(params=["memory", "fake-firestore"])
def env(request: pytest.FixtureRequest) -> Env:
    e = Env(request.param)
    e.seed("g1", turns=40, goal="sunset walk then ramen")
    e.seed("r1", turns=7, goal="my usual saturday outing")
    return e


def ws(env: Env, uid: str, name: str = "default"):
    return env.stores.workspaces.get(uid, name, env.clock())


# --- authenticate both identities, then preview -------------------------------------------------------
def test_preview_shows_the_manifest_and_changes_nothing(env: Env) -> None:
    intent = env.collide()
    response = env.start_migration(intent)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["state"] == "PREVIEW" and view["completed"] is False
    assert [(i["id"], i["kind"], i["title"], i["selected"], i["copied"]) for i in view["items"]] == [
        ("workspace-default", "workspace", "sunset walk then ramen", False, False)]
    assert ws(env, "g1") is not None and ws(env, "r1").goal == "my usual saturday outing"
    assert env.quotas.usage_for("g1").successful_turns == 40
    assert env.quotas.usage_for("r1").successful_turns == 7


def test_no_silent_merge_without_the_parked_collision_and_both_proofs(env: Env) -> None:
    intent = env.start_intent("g1")
    # the intent was never parked: a registered account cannot just claim a guest's data
    not_parked = env.start_migration(intent)
    assert not_parked.status_code == 409 and not_parked.json()["message"].startswith("NOT_COLLISION")
    # a guest token is not a registered account
    assert env.start_migration(intent, who="g1").status_code in (403, 409)
    # wrong nonce looks like an unknown intent
    bad = env.http.post("/v1/account-migrations", json={"intent_id": intent["id"], "nonce": "n" * 24},
                        headers=H["r1"])
    assert bad.status_code == 404
    assert ws(env, "g1") is not None and ws(env, "r1").goal == "my usual saturday outing"


def test_a_different_registered_account_cannot_use_a_parked_intent(env: Env) -> None:
    intent = env.collide(who="r1")  # parked for r1
    stolen = env.start_migration(intent, who="r2")  # r2 has the id and nonce but is not the parked uid
    assert stolen.status_code == 409 and stolen.json()["message"].startswith("NOT_COLLISION")
    assert env.start_migration(intent, who="r1").status_code == 200


def test_the_expired_intent_cannot_start_a_merge(env: Env) -> None:
    intent = env.collide()
    env.clock.advance(minutes=16)
    response = env.start_migration(intent)
    assert response.status_code == 409 and response.json()["message"].startswith("EXPIRED")


def test_start_is_idempotent_by_key_and_one_unfinished_merge_per_guest(env: Env) -> None:
    intent = env.collide()
    a = env.start_migration(intent, **{"Idempotency-Key": "mig-key-1"}).json()
    b = env.start_migration(intent, **{"Idempotency-Key": "mig-key-1"}).json()
    c = env.start_migration(intent, **{"Idempotency-Key": "mig-key-2"}).json()
    assert a["id"] == b["id"] == c["id"]


# --- copy, preferences, allowance ---------------------------------------------------------------------
def test_run_keeps_the_registered_workspace_and_copies_the_guest_one_beside_it(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    response = env.run_migration(migration["id"], item_ids=["workspace-default"])
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["state"] == "COMPLETED" and view["completed"] is True
    assert view["preference"] == "KEEP_REGISTERED"
    assert all(i["title"] == "" and i["copied"] for i in view["items"])  # no user text kept
    assert ws(env, "r1").goal == "my usual saturday outing"  # registered preferences preserved
    copy = ws(env, "r1", f"migrated-{migration['id'][:16]}")
    assert copy is not None and copy.owner_id == "r1" and copy.goal == "sunset walk then ramen"
    assert [f.value for f in copy.facts] == ["no loud places"]


def test_the_user_may_choose_to_replace_the_registered_workspace(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    env.run_migration(migration["id"], item_ids=["workspace-default"], preference="USE_GUEST")
    assert ws(env, "r1").goal == "sunset walk then ramen"


def test_the_copy_drops_provider_derived_text_evidence_and_pending_actions() -> None:
    e = Env("memory")
    e.seed("r1")
    e.stores.workspaces.put("g1", "default", workspace("g1", with_maps=True), e.clock(), None)
    e.quotas.ensure_record(__import__("account_support").guest("g1"))
    migration = e.start_migration(e.collide()).json()
    e.run_migration(migration["id"], item_ids=["workspace-default"])
    copy = ws(e, "r1", f"migrated-{migration['id'][:16]}")
    assert copy.evidence == [] and copy.pending_actions == []
    assert [f.id for f in copy.facts] == ["f1"] and "MAPSCANARY" not in copy.model_dump_json()


def test_the_guest_allowance_is_never_added_to_the_registered_one(env: Env) -> None:
    before = env.quotas.usage_for("r1")
    migration = env.start_migration(env.collide()).json()
    env.run_migration(migration["id"], item_ids=["workspace-default"])
    after = env.quotas.usage_for("r1")
    assert (after.successful_turns, after.activated_trips) == (7, 0) == (
        before.successful_turns, before.activated_trips)
    usage = env.http.get("/v1/me/usage", headers=H["r1"]).json()
    assert usage["user_turns_used"] == 7 and usage["user_turns_limit"] == 100
    assert env.quotas.usage_for("g1").successful_turns == 0  # the guest ledger is gone, not merged


# --- guest data is deleted only after completion, then the uid is closed ------------------------------------
def test_guest_server_data_is_deleted_only_after_completion_and_the_uid_is_closed(env: Env) -> None:
    env.seed("g1", turns=40, consent=True)
    migration = env.start_migration(env.collide()).json()
    assert ws(env, "g1") is not None  # preview deleted nothing
    env.run_migration(migration["id"], item_ids=["workspace-default"])
    assert ws(env, "g1") is None and env.consents.ledger("g1") == ()
    assert "g1" not in env.dump().replace("g1-linked", "")  # nothing of the guest is left
    assert env.http.get("/v1/me/usage", headers=H["g1"]).status_code == 403  # old guest token: closed
    assert "g1" in env.admin.deleted
    assert env.service.is_deleted("g1")  # type: ignore[union-attr]
    # the registered account still works and sees the merged data
    assert env.http.get("/v1/me/usage", headers=H["r1"]).status_code == 200


def test_choosing_nothing_copies_nothing_and_still_requires_completion(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    view = env.run_migration(migration["id"], item_ids=[]).json()
    assert view["completed"] and not any(i["copied"] for i in view["items"])
    assert ws(env, "r1").goal == "my usual saturday outing"


def test_a_selection_outside_the_manifest_is_refused_and_changes_nothing(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    response = env.run_migration(migration["id"], item_ids=["not-in-the-manifest"])
    assert response.status_code == 422 and response.json()["message"].startswith("UNKNOWN_ITEM")
    assert ws(env, "g1") is not None
    assert env.http.get(f"/v1/account-migrations/{migration['id']}", headers=H["r1"]).json()[
        "state"] == "PREVIEW"


# --- interruption and resume --------------------------------------------------------------------------------
class FlakySource(WorkspaceSource):
    def __init__(self, workspaces, fail_times: int) -> None:
        super().__init__(workspaces)
        self.fail_times = fail_times
        self.copies: list[str] = []

    def copy(self, src_uid, dst_uid, entry_id, target, idempotency_key, now) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("provider down")
        self.copies.append(idempotency_key)
        super().copy(src_uid, dst_uid, entry_id, target, idempotency_key, now)


def flaky(env: Env, fail_times: int) -> FlakySource:
    source = FlakySource(env.stores.workspaces, fail_times)
    env.service.migrations._sources = {source.name: source}  # noqa: SLF001
    return source


def test_an_interrupted_merge_resumes_and_both_identities_survive_until_success(env: Env) -> None:
    source = flaky(env, fail_times=1)
    migration = env.start_migration(env.collide()).json()
    first = env.run_migration(migration["id"], item_ids=["workspace-default"])
    assert first.status_code == 503 and first.json()["message"].startswith("INTERRUPTED")
    # both identities and all their data are intact; the journal says RUNNING
    assert ws(env, "g1") is not None and env.quotas.usage_for("g1").successful_turns == 40
    assert ws(env, "r1").goal == "my usual saturday outing" and ws(env, "r1", f"migrated-{migration['id'][:16]}") is None
    assert not env.service.is_deleted("g1") and "g1" not in env.admin.deleted  # type: ignore[union-attr]
    assert env.http.get("/v1/me/usage", headers=H["g1"]).status_code == 200
    state = env.http.get(f"/v1/account-migrations/{migration['id']}", headers=H["r1"]).json()
    assert state["state"] == "RUNNING"
    resumed = env.run_migration(migration["id"])  # the selection is in the journal
    assert resumed.status_code == 200 and resumed.json()["completed"] is True
    assert source.copies == [f"{migration['id']}:workspace-default"]  # one copy, its own key
    assert ws(env, "g1") is None and ws(env, "r1", f"migrated-{migration['id'][:16]}") is not None


def test_a_failed_cleanup_leaves_the_copies_and_resumes(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    env.admin.fail_delete = True  # best effort for a guest: the data still goes
    broken = env.service.deleters._deleters["workspaces"]  # noqa: SLF001
    calls = {"n": 0}

    def flaky_delete(uid: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store down")
        return broken(uid)

    env.service.deleters._deleters["workspaces"] = flaky_delete  # noqa: SLF001
    first = env.run_migration(migration["id"], item_ids=["workspace-default"])
    assert first.status_code == 503
    assert ws(env, "r1", f"migrated-{migration['id'][:16]}") is not None  # copies are complete
    assert env.http.get(f"/v1/account-migrations/{migration['id']}", headers=H["r1"]).json()[
        "state"] == "CLEANING"
    done = env.run_migration(migration["id"])
    assert done.status_code == 200 and done.json()["completed"] is True
    assert ws(env, "g1") is None


def test_a_completed_merge_is_replayed_not_repeated(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    first = env.run_migration(migration["id"], item_ids=["workspace-default"]).json()
    second = env.run_migration(migration["id"], item_ids=["workspace-default"]).json()
    assert first == second and second["completed"]


def test_racing_runs_complete_once(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    codes: list[int] = []
    barrier = threading.Barrier(6)

    def go() -> None:
        barrier.wait()
        codes.append(env.run_migration(migration["id"], item_ids=["workspace-default"]).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert set(codes) <= {200, 503} and 200 in codes
    final = env.run_migration(migration["id"]).json()
    assert final["completed"] is True
    assert ws(env, "r1", f"migrated-{migration['id'][:16]}") is not None
    assert env.quotas.usage_for("r1").successful_turns == 7


# --- tenant isolation --------------------------------------------------------------------------------------------
def test_another_account_cannot_read_or_run_a_migration(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    for method, path, body in (
        ("GET", f"/v1/account-migrations/{migration['id']}", None),
        ("POST", f"/v1/account-migrations/{migration['id']}:run", {"item_ids": []}),
    ):
        response = env.http.request(method, path, json=body, headers=H["r2"])
        assert response.status_code == 404 and response.json()["message"].startswith("NOT_FOUND")
    assert ws(env, "g1") is not None
    unknown = env.http.get("/v1/account-migrations/mig-nope", headers=H["r1"])
    assert unknown.status_code == 404


def test_a_guest_cannot_start_a_migration(env: Env) -> None:
    intent = env.start_intent("g1")
    assert env.start_migration(intent, who="g1").status_code == 403


def test_extra_source_registers_without_editing_the_service(env: Env) -> None:
    class Saved:
        name = "saved_places"

        def __init__(self) -> None:
            self.copied: list[str] = []

        def manifest(self, src_uid, now):
            return (ManifestEntry("saved-1", "saved_place", "Home"),)

        def plan(self, dst_uid, entry_id, preference, migration_id, now):
            return "saved"

        def copy(self, src_uid, dst_uid, entry_id, target, idempotency_key, now):
            self.copied.append(idempotency_key)

    source = Saved()
    env.service.register_migration_source(source)  # type: ignore[union-attr]
    migration = env.start_migration(env.collide()).json()
    assert {i["id"] for i in migration["items"]} == {"workspace-default", "saved-1"}
    env.run_migration(migration["id"], item_ids=["saved-1"])
    assert source.copied == [f"{migration['id']}:saved-1"]


def test_migration_journals_hold_no_tokens_and_no_user_text_once_complete(env: Env) -> None:
    migration = env.start_migration(env.collide()).json()
    env.run_migration(migration["id"], item_ids=["workspace-default"])
    blob = env.account_dump()
    assert blob != "[]" and "tok-" not in blob and "sunset walk" not in blob
    assert '"g1"' not in blob and "'g1'" not in blob and "src_uid='g1'" not in blob
    assert user_fact("x", "y")  # (builder import is exercised)
