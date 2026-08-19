"""FastAPI application entry point.

Phase 0 exposes a health endpoint and nothing else. No enterprise data, no model
credentials, no tool surface. See CLAUDE.md for what each phase unlocks.
"""

import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1.chat import router as chat_router
from app.api.v1.documents import router as documents_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.me import router as me_router
from app.api.v1.search import router as search_router
from app.core.config import settings

logging.basicConfig(level=settings.log_level)

app = FastAPI(
    title="Enterprise AI Integration Platform",
    version="0.1.0",
    description="Phase 0 foundation. No integrations are enabled yet.",
)

app.include_router(health_router)
app.include_router(me_router)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(documents_router)
app.include_router(jobs_router)
