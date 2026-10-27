"""Selects the Firestore backend a store contract test runs against.

``"fake-firestore"`` / ``"firestore"`` use the in-process fake. ``"emulator"`` uses the REAL
``google-cloud-firestore`` client against the Firestore emulator and is skipped unless
``FIRESTORE_EMULATOR_HOST`` is set (the default suite is unchanged). Every emulator client gets a
unique throwaway project id, so tests cannot see each other's data, and ``conftest.py`` clears the
project afterwards. No credentials are used: the client is built with anonymous credentials.

What the emulator does NOT prove: TTL policy deletion (never enforced), real latency or
contention limits, security rules, and production index requirements (the emulator serves
queries without composite indexes).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from app.stores.fake_firestore import FakeFirestoreClient, fake_transactional

EMULATOR_HOST_VAR = "FIRESTORE_EMULATOR_HOST"
SKIP_REASON = f"{EMULATOR_HOST_VAR} is not set (start the Firestore emulator to run these)"


def emulator_enabled() -> bool:
    return bool(os.environ.get(EMULATOR_HOST_VAR))


EMULATOR = pytest.param("emulator", marks=pytest.mark.skipif(not emulator_enabled(),
                                                             reason=SKIP_REASON))
MEMORY_FAKE_EMULATOR = ["memory", "fake-firestore", EMULATOR]
MEMORY_FIRESTORE_EMULATOR = ["memory", "firestore", EMULATOR]

_projects: list[str] = []


class EmulatorClient:
    """The real client plus the one test helper the fake also has (``documents``)."""

    def __init__(self, project: str) -> None:
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore

        self.project = project
        self._client = firestore.Client(project=project, credentials=AnonymousCredentials())
        self.transactional = firestore.transactional

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def run_ttl_sweep(self, field: str = "expires_at") -> int:
        """The fake's TTL sweep. The emulator never enforces TTL policies: deletes nothing."""
        return 0

    def documents(self, collection: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        names = [collection] if collection else [c.id for c in self._client.collections()]
        return [(snap.reference.path, snap.to_dict()) for name in names
                for snap in self._client.collection(name).stream()]


def clear_emulator_projects() -> None:
    import urllib.request

    host = os.environ.get(EMULATOR_HOST_VAR)
    while _projects and host:
        project = _projects.pop()
        request = urllib.request.Request(
            f"http://{host}/emulator/v1/projects/{project}/databases/(default)/documents",
            method="DELETE")
        try:
            urllib.request.urlopen(request, timeout=10).close()  # noqa: S310 - local emulator
        except OSError:
            pass


def client_for(kind: str, clock: Callable[[], datetime] | None = None) -> Any:
    if kind == "emulator":
        if not emulator_enabled():
            pytest.skip(SKIP_REASON)
        project = f"wsg-test-{uuid.uuid4().hex[:12]}"
        _projects.append(project)
        return EmulatorClient(project)
    return FakeFirestoreClient(clock)


def txn_for(client: Any) -> Callable[..., Any]:
    return client.transactional if isinstance(client, EmulatorClient) else fake_transactional
