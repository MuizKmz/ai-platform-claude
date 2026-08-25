"""Test configuration.

Environment must be set BEFORE app modules import, because app.core.config
validates at import time.

Two kinds of test live here:

- Unit tests, which never touch a database. They only need the variables to be
  present and syntactically valid.
- Security tests, which connect to a real Postgres to prove Row-Level Security
  works. Those need the developer's actual credentials.

So: load the real .env if it exists, and fall back to placeholders otherwise (CI,
or a fresh clone with no .env). Placeholders keep unit tests runnable without
infrastructure; the database tests skip themselves when they cannot connect.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

# Real values first — load_dotenv does not overwrite existing environment vars,
# so an explicitly-exported variable still wins over the file.
load_dotenv(REPO_ROOT / ".env")

# Placeholders for anything still unset. Unit tests need these to exist; they are
# not real credentials and any test that actually connects will fail or skip.
_FALLBACK = {
    "APP_ENV": "test",
    "LOG_LEVEL": "WARNING",
    "POSTGRES_USER": "eaip",
    "POSTGRES_PASSWORD": "test-password",  # noqa: S105 — placeholder, not a secret
    "POSTGRES_DB": "eaip",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5433",
    "POSTGRES_APP_PASSWORD": "test-app",  # noqa: S105
    "POSTGRES_READONLY_PASSWORD": "test-readonly",  # noqa: S105
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6380",
}

for key, value in _FALLBACK.items():
    os.environ.setdefault(key, value)

# The suite mints its own HS256 tokens through `issue_token`, so a developer who
# has pointed their .env at a real identity provider would otherwise watch 137
# tests fail on tokens the OIDC path is right to refuse — which reads as "my
# branch is broken" rather than "my environment changed".
#
# Set to empty rather than deleted. Settings reads `env_file=.env` directly, so
# removing the environment variable just lets the file supply the value again;
# only an explicit empty string wins, because load_dotenv does not overwrite and
# pydantic prefers the environment over the file.
#
# The tests that DO exercise provider verification set these with monkeypatch,
# so nothing here reduces their coverage.
#
# APP_ENV is forced for the same reason: `issue_token` refuses to run in
# production, and testing a production-like .env should not cost the suite.
os.environ["OIDC_JWKS_URL"] = ""
os.environ["OIDC_ISSUER"] = ""
os.environ["APP_ENV"] = "test"
