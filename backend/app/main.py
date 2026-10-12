from fastapi import FastAPI

from app.policy import CURRENT_POLICY

app = FastAPI(title="WhenShouldGo API", version="0.0.1")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "policyVersion": CURRENT_POLICY.version}
