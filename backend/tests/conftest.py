"""Test configuration.

Sets the environment BEFORE app modules import, because app.core.config validates
at import time. Tests must not depend on the developer's real .env.
"""

import os

_TEST_ENV = {
    "APP_ENV": "test",
    "LOG_LEVEL": "WARNING",
    "POSTGRES_USER": "eaip",
    "POSTGRES_PASSWORD": "test-password",  # noqa: S105 — test fixture, not a real secret
    "POSTGRES_DB": "eaip",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_READONLY_PASSWORD": "test-readonly",  # noqa: S105
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)
