"""Background ingestion: job scoping, and resumability.

The interesting test here is resumability. A killed job does not resume from a
checkpoint — it runs again, and idempotent ingestion makes that a no-op for every
file already done. "Resume" and "retry" are the same operation, which is why
there is no checkpoint to keep in step with reality.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import issue_token
from app.knowledge.embedding import FakeEmbeddings
from app.knowledge.ingest import IngestResult, ingest_file
from app.main import app


def _database_available() -> bool:
    try:
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not _database_available(), reason="no database reachable"),
]

TENANT = uuid.UUID("4d4d0000-0000-0000-0000-00000000004d")
OTHER_TENANT = uuid.UUID("4d4d0000-0000-0000-0000-0000000000ff")
USER = uuid.UUID("4d4d0000-0000-0000-0000-0000000000dd")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER_TENANT}
            conn.execute(text("DELETE FROM ingest_job WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM chunk WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM document WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'jobs-test', 'Jobs'), (:b, 'jobs-other', 'Other')
            """),
            {"a": TENANT, "b": OTHER_TENANT},
        )
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _token(roles: tuple[str, ...] = ("admin",), tenant: uuid.UUID = TENANT) -> str:
    return issue_token(
        tenant_id=tenant,
        user_id=USER,
        email="jobs@test",
        roles=roles,
        allowed_labels=("public",),
    )


def _headers(roles: tuple[str, ...] = ("admin",), tenant: uuid.UUID = TENANT) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(roles, tenant)}"}


def _insert_job(engine: Engine, tenant_id: uuid.UUID, status: str = "queued") -> uuid.UUID:
    job_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO ingest_job
                  (id, tenant_id, user_id, source_path, labels, status)
                VALUES (:id, :t, :u, '/corpus', 'public', :status)
            """),
            {"id": job_id, "t": tenant_id, "u": USER, "status": status},
        )
    return job_id


# --- authorization ------------------------------------------------------------


def test_submitting_requires_auth(client: TestClient) -> None:
    response = client.post("/v1/jobs/ingest", json={"directory": "/x", "labels": ["public"]})

    assert response.status_code == 401


def test_submitting_requires_the_admin_role(client: TestClient, tmp_path: Path) -> None:
    """Ingestion writes to the corpus everyone reads, and costs money per run."""
    response = client.post(
        "/v1/jobs/ingest",
        json={"directory": str(tmp_path), "labels": ["public"]},
        headers=_headers(("reader",)),
    )

    assert response.status_code == 403


def test_ingest_without_labels_is_rejected(client: TestClient, tmp_path: Path) -> None:
    """A document with no labels is visible to nobody, so an unlabelled ingest
    silently produces a corpus nobody can search."""
    response = client.post(
        "/v1/jobs/ingest",
        json={"directory": str(tmp_path), "labels": []},
        headers=_headers(),
    )

    assert response.status_code == 422


def test_cannot_read_another_tenants_job(client: TestClient, engine: Engine) -> None:
    """A guessed job id from another tenant is a 404, not a 403."""
    foreign_job = _insert_job(engine, OTHER_TENANT)

    response = client.get(f"/v1/jobs/{foreign_job}", headers=_headers())

    assert response.status_code == 404


def test_listing_shows_only_this_tenants_jobs(client: TestClient, engine: Engine) -> None:
    mine = _insert_job(engine, TENANT)
    _insert_job(engine, OTHER_TENANT)

    body = client.get("/v1/jobs", headers=_headers()).json()

    assert [job["id"] for job in body] == [str(mine)]


# --- job records --------------------------------------------------------------


def test_job_status_is_reported(client: TestClient, engine: Engine) -> None:
    job_id = _insert_job(engine, TENANT, status="running")

    body = client.get(f"/v1/jobs/{job_id}", headers=_headers()).json()

    assert body["status"] == "running"
    assert body["labels"] == ["public"]
    assert body["files_done"] == 0


def test_nonexistent_directory_is_rejected_before_queueing(client: TestClient) -> None:
    """Fail at submission, not two minutes later in a worker nobody is watching."""
    response = client.post(
        "/v1/jobs/ingest",
        json={"directory": "/definitely/not/here", "labels": ["public"]},
        headers=_headers(),
    )

    assert response.status_code == 400


# --- resumability -------------------------------------------------------------


def test_worker_job_resumable(engine: Engine, tmp_path: Path) -> None:
    """The roadmap's test: a killed job resumes without duplicating work.

    Simulated by ingesting half the files, then running the whole directory as a
    fresh job. The already-ingested files must be skipped, not re-created.
    """
    for index in range(6):
        (tmp_path / f"doc-{index}.md").write_text(
            f"# Section {index}\n\nContent for section {index}.\n", encoding="utf-8"
        )

    provider = FakeEmbeddings()
    files = sorted(tmp_path.glob("*.md"))

    # First run: interrupted after three files.
    partial = IngestResult()
    for path in files[:3]:
        with Session(engine) as session, session.begin():
            ingest_file(
                session,
                path=path,
                tenant_id=TENANT,
                labels=["public"],
                provider=provider,
                result=partial,
            )
    assert partial.documents_created == 3

    # Second run: the whole directory, as a retry would do.
    resumed = IngestResult()
    for path in files:
        with Session(engine) as session, session.begin():
            ingest_file(
                session,
                path=path,
                tenant_id=TENANT,
                labels=["public"],
                provider=provider,
                result=resumed,
            )

    assert resumed.documents_created == 3, "already-ingested files were re-created"
    assert resumed.documents_skipped == 3

    with engine.connect() as conn:
        total = conn.execute(
            text("SELECT count(*) FROM document WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    assert total == 6, "resuming duplicated documents"


def test_one_unreadable_file_does_not_lose_the_others(engine: Engine, tmp_path: Path) -> None:
    """Per-file transactions: a bad file at 499 of 500 must not discard the work
    already paid for in embeddings."""
    (tmp_path / "good-1.md").write_text("# One\n\nFirst document.\n", encoding="utf-8")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 truncated garbage")
    (tmp_path / "good-2.md").write_text("# Two\n\nSecond document.\n", encoding="utf-8")

    provider = FakeEmbeddings()
    result = IngestResult()
    for path in sorted(tmp_path.iterdir()):
        with Session(engine) as session, session.begin():
            ingest_file(
                session,
                path=path,
                tenant_id=TENANT,
                labels=["public"],
                provider=provider,
                result=result,
            )

    assert result.documents_created == 2
    assert result.documents_skipped == 1


def test_job_rows_are_tenant_isolated_at_the_database(engine: Engine) -> None:
    """RLS applies to ingest_job like every other tenant table.

    A job record names a tenant's documents, so it is tenant data.
    """
    _insert_job(engine, TENANT)
    _insert_job(engine, OTHER_TENANT)

    app_engine = create_engine(settings.app_database_url)
    try:
        with app_engine.begin() as conn:
            conn.execute(text(f"SET LOCAL app.tenant_id = '{TENANT}'"))
            visible = conn.execute(text("SELECT count(*) FROM ingest_job")).scalar()
        with app_engine.connect() as conn:
            without_context = conn.execute(text("SELECT count(*) FROM ingest_job")).scalar()
    finally:
        app_engine.dispose()

    assert visible == 1
    assert without_context == 0, "no tenant context should mean no rows"
