"""Developer CLI.

Phase 1 scope: create tenants and users, and mint development tokens so the API
can be exercised by hand. Ingestion arrives in stage 3.

    uv run python -m app.cli create-tenant acme "Acme Corp"
    uv run python -m app.cli create-user acme alice@acme.com --labels public,finance
    uv run python -m app.cli token acme alice@acme.com
"""

import argparse
import sys
import uuid

from sqlalchemy import text

from app.core.security import issue_token
from app.db.session import owner_engine


def _create_tenant(slug: str, name: str) -> None:
    """Create a tenant. Uses the owner connection: this is administrative work
    that necessarily happens outside any single tenant's context."""
    with owner_engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": slug}
        ).scalar()
        if existing:
            print(f"tenant '{slug}' already exists: {existing}")
            return
        tenant_id = conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:id, :slug, :name) RETURNING id"),
            {"id": uuid.uuid4(), "slug": slug, "name": name},
        ).scalar()
    print(f"created tenant '{slug}': {tenant_id}")


def _create_user(tenant_slug: str, email: str, roles: list[str], labels: list[str]) -> None:
    with owner_engine.begin() as conn:
        tenant_id = conn.execute(
            text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": tenant_slug}
        ).scalar()
        if not tenant_id:
            sys.exit(f"no such tenant: {tenant_slug}")

        existing = conn.execute(
            text("SELECT id FROM app_user WHERE tenant_id = :t AND email = :e"),
            {"t": tenant_id, "e": email},
        ).scalar()
        if existing:
            print(f"user already exists: {existing}")
            return

        user_id = conn.execute(
            text("""
                INSERT INTO app_user (id, tenant_id, email, roles, allowed_labels)
                VALUES (:id, :t, :e, :r, :l) RETURNING id
            """),
            {"id": uuid.uuid4(), "t": tenant_id, "e": email, "r": roles, "l": labels},
        ).scalar()
    print(f"created user {email}: {user_id}")


def _token(tenant_slug: str, email: str) -> None:
    """Mint a token for an existing user.

    Claims are read from the database, never from the command line. A token whose
    tenant or labels came from an argument would make the CLI a way to mint
    arbitrary authority — exactly what invariant #1 forbids of request input.
    """
    with owner_engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT u.id, u.tenant_id, u.email, u.roles, u.allowed_labels
                FROM app_user u JOIN tenant t ON t.id = u.tenant_id
                WHERE t.slug = :slug AND u.email = :email
            """),
            {"slug": tenant_slug, "email": email},
        ).one_or_none()

    if row is None:
        sys.exit(f"no such user: {email} in tenant {tenant_slug}")

    print(
        issue_token(
            tenant_id=row.tenant_id,
            user_id=row.id,
            email=row.email,
            roles=tuple(row.roles),
            allowed_labels=tuple(row.allowed_labels),
        )
    )


def _split_csv(value: str) -> list[str]:
    """Parse a comma-separated option, dropping empties and surrounding space."""
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-tenant")
    p.add_argument("slug")
    p.add_argument("name")

    p = sub.add_parser("create-user")
    p.add_argument("tenant_slug")
    p.add_argument("email")
    p.add_argument("--roles", default="")
    p.add_argument("--labels", default="")

    p = sub.add_parser("token")
    p.add_argument("tenant_slug")
    p.add_argument("email")

    args = parser.parse_args()

    if args.command == "create-tenant":
        _create_tenant(args.slug, args.name)
    elif args.command == "create-user":
        _create_user(
            args.tenant_slug,
            args.email,
            _split_csv(args.roles),
            _split_csv(args.labels),
        )
    elif args.command == "token":
        _token(args.tenant_slug, args.email)


if __name__ == "__main__":
    main()
