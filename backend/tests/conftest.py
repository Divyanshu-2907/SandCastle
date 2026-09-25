"""Test fixtures: a throwaway Postgres schema and an authenticated API client."""

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The app DB is Postgres-specific (JSONB, pgvector, native enums), so tests run
# against the real thing in a disposable database rather than SQLite.
ADMIN_URL = os.getenv(
    "TEST_ADMIN_DATABASE_URL",
    "postgresql+psycopg2://sandcastle:sandcastle@postgres:5432/postgres",
)
TEST_DB = f"sandcastle_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))

    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"
    os.environ["DATABASE_URL"] = url
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": TEST_DB},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url: str):
    from app.db.base import Base
    from app import models  # noqa: F401 - registers the tables

    engine = create_engine(database_url)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:
    """A session on a connection whose transaction is rolled back after the test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db: Session, monkeypatch) -> Iterator[TestClient]:
    """API client bound to the test session, with Celery dispatch stubbed out."""
    import app.jobs as jobs_module
    from app.db.session import get_db
    from app.main import app as fastapi_app

    enqueued: list[str] = []

    class FakeTask:
        """Records the enqueue instead of handing it to a live broker."""

        @staticmethod
        def delay(run_id: str) -> None:
            enqueued.append(run_id)

    monkeypatch.setattr(jobs_module, "run_agent_task", FakeTask)

    fastapi_app.dependency_overrides[get_db] = lambda: db
    with TestClient(fastapi_app) as test_client:
        test_client.enqueued = enqueued  # type: ignore[attr-defined]
        yield test_client
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    """A client already registered and carrying a bearer token."""
    response = client.post(
        "/auth/register", json={"email": "owner@example.com", "password": "hunter2hunter2"}
    )
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
def other_client(client: TestClient) -> TestClient:
    """A second, unrelated user -- for checking ownership scoping.

    Shares the app (and therefore the test session) with `client`, so both users
    live in the same transaction.
    """
    second = TestClient(client.app)
    response = second.post(
        "/auth/register", json={"email": "stranger@example.com", "password": "hunter2hunter2"}
    )
    assert response.status_code == 201, response.text
    second.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    return second
