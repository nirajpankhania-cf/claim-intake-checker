"""FastAPI entry point for Claim Intake Check.

Flag-and-report only: no route here (or anywhere else) may approve, deny,
pay, cancel, or contact a policyholder.
"""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Claim Intake Check")


@app.get("/", response_class=PlainTextResponse)
def root() -> str:
    """Health check / landing route."""
    return "Claim Intake Check — OK"
