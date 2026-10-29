from fastapi import FastAPI

app = FastAPI(title="bank-text2sql", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check: confirms the API process is up."""
    return {"status": "ok"}
