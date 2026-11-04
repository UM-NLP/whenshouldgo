# Backend dependency lock

`backend/requirements.lock` pins every third-party package the production image installs, with
SHA-256 hashes. The Dockerfile installs it with `pip install --require-hashes -r requirements.lock`
and then `pip install --no-deps .` (the application itself). It covers `pyproject.toml` plus the
`auth` and `gcp` extras. CI and local development are unchanged: they install
`-e ".[dev,auth,gcp]"` from the version ranges in `pyproject.toml`, so a range that drifts from the
lock is caught by the test suite, not by the lock.

## Refresh (do this when `pyproject.toml` dependencies change, and at least monthly)

Compile on the SAME platform and Python as the image (Linux, Python 3.12), so platform markers such
as `uvloop` resolve for the image and not for your laptop. From the repository root, with Docker:

```sh
docker run --rm -v "$PWD/backend:/src" -w /work python:3.12-slim sh -c '
  cp /src/pyproject.toml . && mkdir app && touch app/__init__.py &&
  pip install -q pip-tools &&
  pip-compile --upgrade --generate-hashes --extra auth --extra gcp --strip-extras \
      --output-file=/src/requirements.lock pyproject.toml'
```

(Use the digest-pinned `python:3.12-slim` from the Dockerfile.) Without `--upgrade`, pip-compile
keeps existing pins and only adds what changed. Then review `git diff backend/requirements.lock`,
run the backend suite, and build the image (`docker build backend`). Never edit the lock by hand.
