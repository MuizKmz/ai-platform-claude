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
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import issue_token
from app.db.session import owner_engine
from app.knowledge.embedding import EmbeddingError, get_embedding_provider
from app.knowledge.ingest import ingest_directory
from app.knowledge.lifecycle import reindex_document


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


def _token(tenant_slug: str, email: str, *, mcp: bool = False) -> None:
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

    # An MCP token carries a different audience, so a token minted for the
    # console cannot be used as a machine credential against the MCP server.
    # That separation is RFC 8707 and the spec's no-passthrough rule.
    audience = None
    if mcp:
        from app.mcp.auth import mcp_audience

        audience = mcp_audience()

    print(
        issue_token(
            tenant_id=row.tenant_id,
            user_id=row.id,
            email=row.email,
            roles=tuple(row.roles),
            allowed_labels=tuple(row.allowed_labels),
            audience=audience,
        )
    )


def _ingest(directory: str, tenant_slug: str, labels: list[str]) -> None:
    """Ingest a directory for one tenant.

    Everything happens in one transaction: a failure part-way through leaves no
    half-ingested document, which is what makes re-running safe.
    """
    try:
        provider = get_embedding_provider()
    except EmbeddingError as exc:
        sys.exit(str(exc))

    with Session(owner_engine) as session, session.begin():
        tenant_id = session.execute(
            text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": tenant_slug}
        ).scalar()
        if not tenant_id:
            sys.exit(f"no such tenant: {tenant_slug}")

        if not labels:
            # Default-deny: an unlabelled document is visible to nobody, so
            # ingesting without labels is almost always a mistake worth stopping on.
            sys.exit("--labels is required; a document with no labels is visible to nobody")

        try:
            result = ingest_directory(
                session,
                directory=Path(directory),
                tenant_id=tenant_id,
                labels=labels,
                provider=provider,
            )
        except EmbeddingError as exc:
            sys.exit(f"ingestion failed: {exc}")

    print(f"{result} (model: {provider.model})")


def _reindex(tenant_slug: str) -> None:
    """Re-embed every live document for a tenant with the current provider.

    Needed after changing EMBEDDING_MODEL: a corpus embedded with two models is
    unsearchable, because cosine distance between vectors from different models
    is a meaningless number rather than an error.
    """
    try:
        provider = get_embedding_provider()
    except EmbeddingError as exc:
        sys.exit(str(exc))

    with Session(owner_engine) as session, session.begin():
        tenant_id = session.execute(
            text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": tenant_slug}
        ).scalar()
        if not tenant_id:
            sys.exit(f"no such tenant: {tenant_slug}")

        document_ids = list(
            session.execute(
                text("""
                    SELECT id FROM document
                    WHERE tenant_id = :t AND superseded_at IS NULL
                """),
                {"t": tenant_id},
            ).scalars()
        )

        documents = chunks = 0
        for document_id in document_ids:
            result = reindex_document(
                session, tenant_id=tenant_id, document_id=document_id, provider=provider
            )
            documents += result.documents_reindexed
            chunks += result.chunks_reembedded

    print(f"{documents} document(s) reindexed, {chunks} chunk(s) re-embedded ({provider.model})")


def _retention(dry_run: bool) -> None:
    """Delete data past its retention period.

    Meant to run on a schedule — a nightly cron in a real deployment. Exposed
    as a command rather than a background task because deleting data should be
    something an operator can run, inspect, and explain, not something that
    happens invisibly.
    """
    from sqlalchemy.orm import Session

    from app.core.retention import POLICIES, apply_retention

    print("Retention policy:")
    for policy in POLICIES:
        print(f"  {policy.table:22} {policy.days:>4} days  — {policy.reason}")
    print()

    with Session(owner_engine) as session:
        result = apply_retention(session, dry_run=dry_run)
        if not dry_run:
            session.commit()

    verb = "Would delete" if dry_run else "Deleted"
    print(f"{verb}: {result}")
    if dry_run and result.total:
        print()
        print("Run without --dry-run to apply.")


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
    p.add_argument(
        "--mcp",
        action="store_true",
        help="mint a token for the MCP server rather than the API",
    )

    p = sub.add_parser("reindex")
    p.add_argument("tenant_slug")

    p = sub.add_parser("ingest")
    p.add_argument("directory")
    p.add_argument("--tenant", required=True)
    p.add_argument("--labels", default="", help="comma-separated; required")

    p = sub.add_parser("retention", help="delete data past its retention period")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be deleted without deleting it",
    )

    args = parser.parse_args()

    if args.command == "retention":
        _retention(args.dry_run)
        return

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
        _token(args.tenant_slug, args.email, mcp=args.mcp)
    elif args.command == "reindex":
        _reindex(args.tenant_slug)
    elif args.command == "ingest":
        _ingest(args.directory, args.tenant, _split_csv(args.labels))


if __name__ == "__main__":
    main()
