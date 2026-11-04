"""Firestore queries use ``FieldFilter`` (the positional ``where(field, op, value)`` form warns)."""
from __future__ import annotations

import re
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.stores.fake_firestore import FakeFirestoreClient
from app.stores.firestore import where

APP = Path(__file__).resolve().parents[1] / "app"


def test_no_positional_where_calls_remain_in_app_code() -> None:
    pattern = re.compile(r"\.where\(\s*[\"']")
    offenders = [str(p.relative_to(APP)) for p in APP.rglob("*.py") if pattern.search(p.read_text("utf-8"))]
    assert offenders == []


def test_the_helper_works_on_the_fake_with_every_operator() -> None:
    client = FakeFirestoreClient(lambda: datetime(2026, 11, 3, tzinfo=UTC))
    col = client.collection("c")
    for i in range(3):
        col.document(f"d{i}").set({"n": i, "o": "a" if i < 2 else "b"})
    assert [s.id for s in where(col, "o", "==", "a").stream()] == ["d0", "d1"]
    assert [s.id for s in where(where(col, "o", "==", "a"), "n", ">=", 1).stream()] == ["d1"]


def test_the_helper_builds_a_real_query_without_a_user_warning() -> None:
    pytest.importorskip("google.cloud.firestore")
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore

    client = firestore.Client(project="wsg-offline", credentials=AnonymousCredentials())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        query = where(client.collection("c"), "owner_id", "==", "u1")
    assert query is not None
