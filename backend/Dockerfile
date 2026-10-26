# Production image (decision 26). Build context is backend/. No secrets, no .env, no dev
# entry points. Configuration arrives at runtime through the platform environment.
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:$PATH"
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
USER 10001
EXPOSE 8080
# create_production_app fails closed (ConfigurationError) until an identity verifier exists.
CMD ["sh", "-c", "exec uvicorn app.server:create_production_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
