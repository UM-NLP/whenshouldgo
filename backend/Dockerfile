# Production image (decision 26). Build context is backend/. No secrets, no .env, no dev
# entry points. Configuration arrives at runtime through the platform environment.
# Pinned by digest (the multi-arch index of python:3.12-slim resolved 2026-10-26). To update, run
# `docker pull python:3.12-slim`, review the new digest and change both FROM lines together.
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
# The optional extras production needs: ``auth`` (PyJWT, cryptography: Firebase ID and App Check
# token verification) and ``gcp`` (the Firestore client; production refuses in-memory state).
RUN python -m venv /opt/venv && /opt/venv/bin/pip install ".[auth,gcp]"

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:$PATH"
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
USER 10001
EXPOSE 8080
# create_production_app fails closed (ConfigurationError) without FIREBASE_PROJECT_ID, Firestore
# state, a spend budget, the two provider keys, FIREBASE_PROJECT_NUMBER and an explicit
# APP_CHECK_ENFORCE. Access logs are off: a request line can carry a place ID in a query string.
CMD ["sh", "-c", "exec uvicorn app.server:create_production_app --factory --no-access-log --host 0.0.0.0 --port ${PORT:-8080}"]
