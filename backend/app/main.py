"""FastAPI application entry point.

Phase 0 exposes a health endpoint and nothing else. No enterprise data, no model
credentials, no tool surface. See CLAUDE.md for what each phase unlocks.
"""

import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import settings

logging.basicConfig(level=settings.log_level)

app = FastAPI(
    title="Enterprise AI Integration Platform",
    version="0.1.0",
    description="Phase 0 foundation. No integrations are enabled yet.",
)

app.include_router(health_router)
