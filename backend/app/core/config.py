"""Application configuration.

Settings are read from the environment exactly once, at import time. A missing
required variable raises here — at startup — rather than at first use, so a
misconfigured deployment fails immediately and visibly instead of halfway
through a user request.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import PostgresDsn, RedisDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The single .env lives at the repo root, but the app is normally started from
# backend/. Resolve the path from this file's location so the working directory
# does not decide whether configuration is found.
#   config.py -> core -> app -> backend -> <repo root>
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Runtime settings, sourced only from the environment and .env.

    Never from a request body, header, or model output.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # No defaults: these are required. Omitting one is a startup failure.
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str
    postgres_port: int

    # The role the application connects as at runtime. It is NOT a superuser, which
    # is what makes Row-Level Security apply to it — PostgreSQL exempts superusers
    # from RLS unconditionally, so connecting as the owner would silently disable
    # every tenant isolation policy in the database.
    postgres_app_password: str

    # Used by the Phase 4 SQL tool. Required now so the credential exists
    # before the code that needs it, and is never invented in a hurry later.
    postgres_readonly_password: str

    redis_host: str
    redis_port: int

    # --- Identity ---------------------------------------------------------
    # Signing key for locally-issued development tokens. Required with no default:
    # a default would mean every deployment that forgot to set it shares one key,
    # and anyone who read the source could mint a valid token for any tenant.
    jwt_secret: str
    jwt_issuer: str = "https://eaip.local"
    jwt_audience: str = "eaip-api"
    jwt_ttl_minutes: int = 60

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Owner connection string. Migrations only — this role bypasses RLS."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def app_database_url(self) -> str:
        """Runtime connection string. Non-superuser, so RLS policies apply to it.

        Every request-path query uses this. Using `database_url` at runtime would
        silently disable tenant isolation.
        """
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg",
                username="app_rw",
                password=self.postgres_app_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def readonly_database_url(self) -> str:
        """Read-only connection string. Phase 4 generated SQL uses this and only this."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg",
                username="app_readonly",
                password=self.postgres_readonly_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return str(
            RedisDsn.build(
                scheme="redis",
                host=self.redis_host,
                port=self.redis_port,
                path="0",
            )
        )


@lru_cache
def get_settings() -> Settings:
    """One validated Settings instance per process."""
    return Settings()  # type: ignore[call-arg]


# Import-time validation: a missing variable fails here, not at first request.
settings: Settings = get_settings()
