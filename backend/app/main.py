"""FastAPI application entry point.

Phase 0 exposes a health endpoint and nothing else. No enterprise data, no model
credentials, no tool surface. See CLAUDE.md for what each phase unlocks.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1.agent import router as agent_router
from app.api.v1.approvals import router as approvals_router
from app.api.v1.chat import router as chat_router
from app.api.v1.documents import router as documents_router
from app.api.v1.integrations import router as integrations_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.me import router as me_router
from app.api.v1.observability import router as observability_router
from app.api.v1.search import router as search_router
from app.api.v1.users import router as users_router
from app.core.config import settings
from app.mcp.server import router as mcp_router

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
    # Every method any endpoint here actually uses. An allowlist missing a
    # method fails as a 400 on the browser's PREFLIGHT, before the request is
    # sent — so the endpoint looks broken while curl against it works
    # perfectly. test_cors.py pins this list against the routes.
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health_router)
app.include_router(me_router)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(documents_router)
app.include_router(jobs_router)
app.include_router(integrations_router)
app.include_router(approvals_router)
app.include_router(mcp_router)
app.include_router(observability_router)
app.include_router(users_router)
app.include_router(agent_router)
