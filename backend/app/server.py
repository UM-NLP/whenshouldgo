"""Production ASGI entry point (``uvicorn app.server:create_production_app --factory``).

It FAILS CLOSED: the Firebase ``IdentityVerifier`` is built from ``FIREBASE_PROJECT_ID`` (needs the
optional ``auth`` extra) and ``build_runtime`` refuses process-local state, so
``create_production_app`` raises ``ConfigurationError`` without that variable, without Firestore
(``FIRESTORE_PROJECT_ID`` plus a whole-pilot spend budget) and without the two provider keys. It can
never serve traffic anonymously or on process-local state by accident. This module is not imported
by ``app.main``; the module-level ``app`` there is unchanged.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from .composition import (
    ConfigurationError,
    build_runtime,
    firebase_admin_from_env,
    firebase_identity_from_env,
)


def create_production_app() -> FastAPI:
    env = dict(os.environ)
    admin, revocation = firebase_admin_from_env(env)  # (None, None) unless configured
    verifier, disabled_uids = firebase_identity_from_env(env, revocation)
    return build_runtime(env, identity_verifier=verifier, disabled_uids=disabled_uids,
                         firebase_admin=admin, require_app_check=True).app


__all__ = ["ConfigurationError", "create_production_app"]
