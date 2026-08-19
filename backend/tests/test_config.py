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
