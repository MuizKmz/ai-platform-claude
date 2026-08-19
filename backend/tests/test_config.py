"""Config must fail at startup on a missing variable, not at first use."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings


def test_missing_required_var_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required variable with no value is a startup failure."""
    monkeypatch.delenv("POSTGRES_USER", raising=False)

    with pytest.raises(ValidationError) as exc:
        # _env_file=None so a developer's real .env cannot mask the failure.
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "postgres_user" in str(exc.value).lower()


def test_urls_are_assembled_from_parts() -> None:
    """The app builds connection strings; they are never pasted in whole."""
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.postgres_db in settings.database_url


def test_readonly_url_uses_the_readonly_role() -> None:
    """Security invariant #4: the read-only URL must not carry app credentials."""
    assert "app_readonly" in settings.readonly_database_url
    assert settings.postgres_password not in settings.readonly_database_url


def test_env_file_is_found_regardless_of_working_directory() -> None:
    """The app is started from backend/ but .env lives at the repo root.

    Resolving the path from the module location rather than the working directory
    is what makes `cd backend && uvicorn ...` work. A relative "env" string here
    silently yields no configuration and an 8-error ValidationError at startup.
    """
    from app.core.config import ENV_FILE

    assert ENV_FILE.is_absolute()
    assert ENV_FILE.name == ".env"
    # Repo root is the directory containing backend/, not backend/ itself.
    assert (ENV_FILE.parent / "backend").is_dir()
