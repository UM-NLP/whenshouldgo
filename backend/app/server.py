"""Production ASGI entry point (``uvicorn app.server:create_production_app --factory``).

It FAILS CLOSED today: there is no production Firebase ``IdentityVerifier`` yet, and
``build_runtime`` refuses to run without one, so ``create_production_app`` raises
``ConfigurationError`` until a verifier exists and shared state (ACCESS-02, #35) is implemented.
That is deliberate: it can never serve traffic anonymously or on process-local state by accident.
This module is not imported by ``app.main``; the module-level ``app`` there is unchanged.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from .composition import ConfigurationError, build_runtime


def create_production_app() -> FastAPI:
    # TODO(access): pass a Firebase-backed IdentityVerifier and drop allow_in_memory_state once
    # the Firestore-backed state of ACCESS-02 exists. Until then this raises ConfigurationError.
    return build_runtime(dict(os.environ), identity_verifier=None).app


__all__ = ["ConfigurationError", "create_production_app"]
