"""FastAPI application entry point.

Phase 0 exposes a health endpoint and nothing else. No enterprise data, no model
credentials, no tool surface. See CLAUDE.md for what each phase unlocks.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1.chat import router as chat_router
from app.api.v1.documents import router as documents_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.me import router as me_router
from app.api.v1.observability import router as observability_router
from app.api.v1.search import router as search_router
from app.core.config import settings

logging.basicConfig(level=settings.log_level)

app = FastAPI(
    title="Enterprise AI Integration Platform",
    version="0.1.0",
    description="Phase 0 foundation. No integrations are enabled yet.",
)

# The console is served from a different origin than the API, so the browser
# needs explicit permission to read responses.
#
# allow_origins is an allowlist from configuration, never "*". This API is
# authenticated by a bearer token the browser holds; a wildcard origin would
# let any site the user visits read their tenant's data with it.
#
# allow_credentials stays False: the token travels in an Authorization header
# rather than a cookie, so there is nothing to send credentials for — and
# FastAPI refuses to combine credentials with a wildcard anyway, which is worth
# not relying on as the only guard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health_router)
app.include_router(me_router)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(documents_router)
app.include_router(jobs_router)
app.include_router(observability_router)
