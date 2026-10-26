"""Review finding 10: export never 500s on a long consent ledger (compaction), and the append-only
ledger is bounded (a grant past the bound is refused, a withdrawal always works)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from history_support import HEnv

from app.consent import MAX_EXPORT_CONSENTS, MAX_LEDGER_ENTRIES, compact_for_export
from app.domain import ConsentProcessor, ConsentPurpose, ConsentRecord, ConsentScope, evolve

LOC = ConsentPurpose.LOCATION_FOREGROUND


def record(env: HEnv, i: int, *, withdrawn: bool) -> list[ConsentRecord]:
    granted = env.clock() - timedelta(days=2) + timedelta(seconds=i)
    base = ConsentRecord(
        id=f"consent-seed-{i:06d}", owner_id="reg-a", purpose=LOC, notice_version="location-notice-1",
        processors=(ConsentProcessor.GOOGLE,), scope=ConsentScope(), granted_at=granted,
        context="location.foreground")
    rows = [base]
    if withdrawn:
        rows.append(evolve(base, withdrawn_at=granted + timedelta(seconds=1)))
    return rows


def seed(env: HEnv, pairs: int, singles: int = 0) -> int:
    rows: list[ConsentRecord] = []
    for i in range(pairs):
        rows += record(env, i, withdrawn=True)
    for j in range(singles):
        rows += record(env, pairs + j, withdrawn=True)[1:]  # a lone withdrawn line
    env.runtime.stores.consents.update("reg-a", lambda _: (tuple(rows), None))
    return len(rows)


@pytest.fixture(params=["memory", "fake-firestore"])
def env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> HEnv:
    return HEnv(request.param, monkeypatch)


def test_export_with_1200_ledger_entries_does_not_500(env: HEnv) -> None:
    assert seed(env, pairs=600) == 1200
    response = env.call("POST", "/v1/me:export")
    assert response.status_code == 200, response.text
    consents = response.json()["consents"]
    assert len(consents) == 600  # each withdrawn grant/withdraw pair is one line
    assert all(c["withdrawn_at"] for c in consents)


def test_export_with_1200_distinct_withdrawn_consents_keeps_the_newest_and_all_active(
        env: HEnv) -> None:
    seed(env, pairs=0, singles=1200)
    env.grant_all("a")  # two ACTIVE consents on top
    response = env.call("POST", "/v1/me:export")
    assert response.status_code == 200, response.text
    consents = response.json()["consents"]
    assert len(consents) == MAX_EXPORT_CONSENTS
    assert sum(1 for c in consents if not c["withdrawn_at"]) == 2
    ids = {c["id"] for c in consents}
    assert "consent-seed-001199" in ids and "consent-seed-000000" not in ids  # oldest dropped


def test_compaction_is_pure_and_ordered(env: HEnv) -> None:
    rows = tuple(r for i in range(5) for r in record(env, i, withdrawn=True))
    out = compact_for_export(rows)
    assert [r.id for r in out] == [f"consent-seed-{i:06d}" for i in range(5)]
    assert all(r.withdrawn_at for r in out)


def test_a_grant_past_the_ledger_bound_is_refused_and_a_withdrawal_still_works(
        env: HEnv) -> None:
    env.grant_all("a")
    seed_rows = []
    for i in range(MAX_LEDGER_ENTRIES):
        seed_rows += record(env, i, withdrawn=True)[1:]
    env.runtime.stores.consents.update("reg-a", lambda _: (tuple(seed_rows), None))
    refused = env.call("POST", "/v1/me/consents", "a",
                       {"purpose": "LOCATION_FOREGROUND", "notice_version": "location-notice-1"})
    assert refused.status_code == 409 and refused.json()["code"] == "CONFLICT"
    assert env.call("DELETE", "/v1/me/consents/HISTORY_STORE").status_code == 204
