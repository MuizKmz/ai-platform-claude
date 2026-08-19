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
