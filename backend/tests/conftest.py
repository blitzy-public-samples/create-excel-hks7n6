"""Fixtures that make the security modules importable and testable in isolation.

Importing anything under ``backend.app`` reaches four live couplings, and every one of them
has to be neutralised before a test can run:

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
    "DATABASE_URL": "sqlite://",
    "REDIS_URL": "memory://",
    "SECRET_KEY": "test-only-signing-key-not-a-real-secret-0123456789",
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "15",
}

# Optional settings the tests rely on having a known value rather than a default.
DEFAULT_TEST_ENVIRONMENT: Dict[str, str] = {
    "ALLOWED_ORIGINS": '["https://app.example.com", "http://localhost:3000"]',
    "gcs_bucket_name": "excel-clone-test-user-uploads",
    "rate_limit_storage_uri": "memory://",
    "rate_limit_trusted_proxies": "[]",
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

    Note that only the tables are created. Configuring the ORM mappers is a separate step
    that the models module cannot currently complete, which the tests that need a real
    query assert directly rather than working around.
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
def override_get_db(in_memory_database):
    """Return a callable that points an application's ``get_db`` at the test database.

    Installs the override through ``app.dependency_overrides`` and removes it again when
    the test ends, so no application object is left carrying it.
    """
    from backend.app.db.database import get_db

    applied: List[object] = []

    def _apply(app) -> None:
        def _get_test_db() -> Iterator[object]:
            session = in_memory_database()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = _get_test_db
        applied.append(app)

    yield _apply

    for app in applied:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def captured_logs():
    """Return a callable that captures records emitted by one named logger.

    ``caplog`` is not used because these assertions are about a specific application logger
    rather than about the root logger's propagation, and because a handler attached
    directly cannot be affected by another test's level changes.
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
