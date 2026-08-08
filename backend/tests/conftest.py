"""Fixtures that make the security modules importable and testable in isolation.

Importing the security stack - ``backend.app.core.security`` and the configuration, database
and model modules it pulls in - reaches four live couplings, and every one of them has to be
neutralised before a test can run:

1. ``Settings`` declares six fields with no defaults, so constructing it with an unset
   environment raises a Pydantic ``ValidationError``.
2. ``Settings.__init__`` performs three live Secret Manager reads on **every**
   construction and overwrites ``DATABASE_URL``, ``REDIS_URL`` and ``SECRET_KEY`` with what
   it fetched.
3. ``backend/app/db/database.py`` calls ``create_engine`` at module scope, so an unusable
   URL fails at import rather than at connect time.
4. ``backend/app/core/security.py`` imports ``get_settings``, ``User`` and ``get_db``, so
   importing the security module alone triggers all three of the above.

The environment variables and the Secret Manager stub are installed at import of this file,
before pytest collects any test module, because coupling 3 happens at import and a fixture
would run too late. Everything else is offered as a fixture.

Nothing here modifies production behaviour: the stub replaces a Google client class on the
``google.cloud.secretmanager`` module object for the life of the test process only, and the
in-memory database is created per test.
"""

import os
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# Coupling 1: the six required Settings fields.
#
# setdefault, not assignment, so a caller may override any of them from the real
# environment. The values are inert local placeholders and none of them is a credential
# for anything that exists.
# ---------------------------------------------------------------------------
REQUIRED_SETTINGS_ENVIRONMENT: Dict[str, str] = {
    "PROJECT_ID": "excel-clone-test",
    # A synchronous PostgreSQL psycopg2 URL, because that is the only form Settings accepts and
    # the only dialect the engine's psycopg2-only connect_args belong to. It names a loopback
    # host and is never connected to: create_engine resolves the dialect and imports the DBAPI
    # without opening a socket, and every test that needs real tables uses the in-memory SQLite
    # session from the in_memory_database fixture instead. The credentials are inert
    # placeholders for a database that does not exist.
    "DATABASE_URL": (
        "postgresql+psycopg2://excel_app:not-a-real-password"
        "@127.0.0.1:5432/main-database"
    ),
    "REDIS_URL": "memory://",
    "SECRET_KEY": "test-only-signing-key-not-a-real-secret-0123456789",
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "15",
}

# Optional settings the tests rely on having a known value rather than a default.
# REDIS_URL above is "memory://", which is what the throttling tiers derive their window
# store from, so counting happens in this process and no broker is required.
DEFAULT_TEST_ENVIRONMENT: Dict[str, str] = {
    "ALLOWED_ORIGINS": '["https://app.example.com", "http://localhost:3000"]',
    "gcs_bucket_name": "excel-clone-test-user-uploads",
}

for _name, _value in REQUIRED_SETTINGS_ENVIRONMENT.items():
    os.environ.setdefault(_name, _value)
for _name, _value in DEFAULT_TEST_ENVIRONMENT.items():
    os.environ.setdefault(_name, _value)


# ---------------------------------------------------------------------------
# Coupling 2: the three Secret Manager reads.
# ---------------------------------------------------------------------------
class _StubSecretPayload:
    """The ``payload`` of an ``access_secret_version`` response."""

    def __init__(self, value: str) -> None:
        self.data = value.encode("utf-8")


class _StubSecretVersion:
    """An ``access_secret_version`` response."""

    def __init__(self, value: str) -> None:
        self.payload = _StubSecretPayload(value)


class StubSecretManagerServiceClient:
    """Answers the three secret reads ``Settings.__init__`` performs.

    Supports the context-manager protocol because ``Settings.__init__`` uses ``with``.
    Every read is served from :data:`REQUIRED_SETTINGS_ENVIRONMENT`, so the value a secret
    resolves to is the same value the environment already carries and no test depends on a
    reachable Secret Manager.
    """

    #: Secret names, in the order ``Settings.__init__`` reads them.
    SECRET_NAMES: Tuple[str, ...] = ("DATABASE_URL", "REDIS_URL", "SECRET_KEY")

    def __enter__(self) -> "StubSecretManagerServiceClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def access_secret_version(self, request: Optional[Dict[str, str]] = None):
        """Return the value for the secret named in ``request['name']``.

        Raises:
            AssertionError: if the request names a secret the application does not read,
                which would mean the configuration module had started reading something
                this stub does not model.
        """
        name = (request or {})["name"]
        for secret in self.SECRET_NAMES:
            if name.endswith("/secrets/%s/versions/latest" % secret):
                return _StubSecretVersion(os.environ[secret])
        raise AssertionError("unexpected Secret Manager read: %r" % name)


def _install_secret_manager_stub() -> None:
    """Replace the Secret Manager client class for the life of the test process."""
    from google.cloud import secretmanager

    secretmanager.SecretManagerServiceClient = StubSecretManagerServiceClient


_install_secret_manager_stub()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def settings_environment() -> Dict[str, str]:
    """The environment the settings under test were built from."""
    combined = dict(REQUIRED_SETTINGS_ENVIRONMENT)
    combined.update(DEFAULT_TEST_ENVIRONMENT)
    return combined


@pytest.fixture
def set_settings(monkeypatch: pytest.MonkeyPatch):
    """Return a callable that overrides settings for the duration of one test.

    Values are written to the process environment through ``monkeypatch``, so they are
    removed when the test ends. ``get_settings()`` is uncached and re-reads the environment
    on every call, which is what makes an override take effect without reloading a module.
    """

    def _apply(**overrides: object) -> None:
        for name, value in overrides.items():
            monkeypatch.setenv(name, str(value))

    return _apply


@pytest.fixture
def in_memory_database():
    """Yield a SQLAlchemy session factory bound to a fresh in-memory SQLite database.

    ``StaticPool`` keeps one connection, which is what makes ``sqlite://`` behave as a
    single shared database rather than a new empty one per connection.

    ``Base.metadata.create_all`` creates the tables the mapped classes declare, which is the
    only schema this repository has - no migration tooling and no committed DDL exist - so a
    test that needs a real query gets the mapped shape rather than a production-verified one.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.app.db.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def authentication_database(in_memory_database, monkeypatch):
    """Point the authentication identity lookup at the in-memory database.

    ``_resolve_current_user`` calls the ``get_db`` name that ``backend.app.core.security``
    imported, not a FastAPI dependency, so ``app.dependency_overrides[get_db]`` cannot reach
    it - an override installed that way is accepted and then simply never consulted, which
    reads as a working fixture while the lookup still talks to the production engine. The
    binding on the security module is therefore what gets patched.

    Yields a callable that seeds :class:`~backend.app.db.models.User` rows and returns the
    session factory, so a test can arrange an identity and then drive the real dependency
    against it.
    """
    from backend.app.core import security

    def _get_test_db() -> Iterator[object]:
        session = in_memory_database()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(security, "get_db", _get_test_db)

    def _seed(*users: object):
        session = in_memory_database()
        try:
            for user in users:
                session.add(user)
            session.commit()
        finally:
            session.close()
        return in_memory_database

    return _seed


@pytest.fixture
def failing_authentication_database(monkeypatch):
    """Point the identity lookup at a session whose query raises.

    Used to assert that the lookup releases its Session on the unexpected-error path too,
    which is the path no successful test exercises.
    """
    from sqlalchemy.exc import OperationalError

    from backend.app.core import security

    closed: List[bool] = []

    class _FailingSession:
        def query(self, *args: object, **kwargs: object):
            raise OperationalError("SELECT 1", {}, Exception("connection lost"))

        def close(self) -> None:
            closed.append(True)

    def _get_failing_db() -> Iterator[object]:
        session = _FailingSession()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(security, "get_db", _get_failing_db)
    return closed


@pytest.fixture
def captured_logs():
    """Return a callable that captures records emitted by one named logger.

    Called with a logger name, it attaches a recording handler directly to that logger and
    returns the handler, whose ``records`` list holds every ``LogRecord`` the logger emits
    from then on. Capture therefore depends on neither root-logger propagation nor the level
    any other test has set. Every handler attached this way is removed when the test ends.
    """
    import logging

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: List[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    attached: List[Tuple[object, _Capture]] = []

    def _capture(logger_name: str) -> _Capture:
        handler = _Capture()
        logger = logging.getLogger(logger_name)
        logger.addHandler(handler)
        attached.append((logger, handler))
        return handler

    yield _capture

    for logger, handler in attached:
        logger.removeHandler(handler)
