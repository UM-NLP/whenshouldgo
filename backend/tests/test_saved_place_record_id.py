"""Saved places have a server-issued opaque record id; DELETE /v1/saved-places/{id} carries it in
the PATH, never the Google place ID. The old ?place_id= route stays one more release, deprecated."""
from __future__ import annotations

import re

import pytest
from history_support import PLACE, PLACE2, HEnv

from app.domain import ConsentPurpose

RECORD_ID = re.compile(r"^sp_[A-Za-z0-9_-]{16,64}$")


@pytest.fixture(params=["memory", "fake-firestore"])
def ready(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> HEnv:
    env = HEnv(request.param, monkeypatch)
    assert env.consent("a", ConsentPurpose.SAVED_PLACE).status_code == 200
    return env


def places(env: HEnv, who: str = "a") -> list[dict]:
    return env.call("GET", "/v1/saved-places", who).json()["places"]


def test_a_saved_place_carries_a_random_opaque_record_id(ready: HEnv) -> None:
    ready.save_via_action("a", key="save-key-0001")
    ready.save_via_action("a", key="save-key-0002", target=PLACE2, user_label="Gym",
                          label_kind="CUSTOM")
    ids = [p["record_id"] for p in places(ready)]
    assert all(RECORD_ID.match(i) for i in ids) and len(set(ids)) == 2
    assert PLACE not in "".join(ids) and PLACE2 not in "".join(ids)
    assert [p["record_id"] for p in places(ready)] == ids  # stable across reads
    relabel = ready.call("PUT", "/v1/saved-places", "a", {
        "place_id": PLACE, "user_label": "Mum's", "label_kind": "HOME",
        "position": places(ready)[0]["position"]})
    assert relabel.json()["record_id"] == ids[0]  # a relabel keeps the id


def test_delete_by_path_id_removes_one_and_is_idempotent(ready: HEnv) -> None:
    ready.save_via_action("a", key="save-key-0001")
    ready.save_via_action("a", key="save-key-0002", target=PLACE2, user_label="Gym",
                          label_kind="CUSTOM")
    gym = next(p for p in places(ready) if p["place_id"] == PLACE2)
    done = ready.call("DELETE", f"/v1/saved-places/{gym['record_id']}", "a")
    assert done.status_code == 200 and done.json()["deleted"] == 1
    assert [p["place_id"] for p in places(ready)] == [PLACE]
    assert ready.call("DELETE", f"/v1/saved-places/{gym['record_id']}", "a").json()["deleted"] == 0


def test_a_path_id_never_reaches_another_tenant_and_a_place_id_is_not_an_id(ready: HEnv) -> None:
    ready.save_via_action("a", key="save-key-0001")
    mine = places(ready)[0]["record_id"]
    assert ready.call("DELETE", f"/v1/saved-places/{mine}", "b").json()["deleted"] == 0
    assert len(places(ready)) == 1
    assert ready.call("DELETE", f"/v1/saved-places/{PLACE}", "a").status_code == 422
    assert len(places(ready)) == 1


def test_the_old_query_route_still_works_and_is_marked_deprecated(ready: HEnv) -> None:
    from app.main import create_app

    ready.save_via_action("a", key="save-key-0001")
    assert ready.call("DELETE", f"/v1/saved-places?place_id={PLACE}", "a").json()["deleted"] == 1
    schema = create_app().openapi()["paths"]
    assert schema["/v1/saved-places"]["delete"].get("deprecated") is True
    assert "deprecated" not in schema["/v1/saved-places/{record_id}"]["delete"]


def test_a_delete_by_path_leaves_the_place_id_out_of_logs(ready: HEnv, caplog) -> None:
    ready.save_via_action("a", key="save-key-0001")
    rid = places(ready)[0]["record_id"]
    ready.call("DELETE", f"/v1/saved-places/{rid}", "a")
    assert PLACE not in caplog.text


def test_a_document_without_a_record_id_gets_a_stable_derived_one(monkeypatch) -> None:
    env = HEnv("fake-firestore", monkeypatch)
    env.consent("a", ConsentPurpose.SAVED_PLACE)
    env.save_via_action("a", key="save-key-0001")
    [(_path, doc)] = env.fs.documents("saved_places")
    del doc["place"]["record_id"]  # as written before record ids existed
    first = places(env)[0]["record_id"]
    assert RECORD_ID.match(first) and places(env)[0]["record_id"] == first
    assert env.call("DELETE", f"/v1/saved-places/{first}", "a").json()["deleted"] == 1
