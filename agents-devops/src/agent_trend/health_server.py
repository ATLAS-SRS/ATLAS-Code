#!/usr/bin/env python3
"""Minimal health check server for the Streamlit dashboard.

Runs on the configured health-check port to provide /health and /ready endpoints
for Kubernetes probes, while Streamlit dashboard runs on port 8501.
"""
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.agent_guardian.config import LOGGER


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager."""
    LOGGER.info("Health check server starting", extra={"port": int(os.getenv("HEALTH_CHECK_PORT", "8002"))})
    yield
    LOGGER.info("Health check server shutting down")


app = FastAPI(title="dashboard-health", lifespan=lifespan)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    """Liveness probe for Kubernetes."""
    return JSONResponse(
        {"status": "healthy", "service": "dashboard"},
        status_code=200,
    )


@app.get("/ready", response_class=JSONResponse)
async def readiness_check():
    """Readiness probe for Kubernetes."""
    try:
        from src.database import get_sync_session
        
        # Try to get a DB connection
        with get_sync_session() as session:
            session.execute(text("SELECT 1"))
        
        return JSONResponse(
            {"status": "ready", "service": "dashboard", "database": "connected"},
            status_code=200,
        )
    except Exception as e:
        LOGGER.error(f"Readiness check failed: {e}")
        return JSONResponse(
            {"status": "not-ready", "service": "dashboard", "database": "error"},
            status_code=503,
        )


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("HEALTH_CHECK_PORT", "8002"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=None)
