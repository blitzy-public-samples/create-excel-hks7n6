"""Pytest fixtures for the Excel Clone backend test suite.

``backend.app.main`` cannot be imported by a test module as the application stands. Five
couplings run at import time - four in application code, one in the package layout - and
this module neutralises every one of them before it performs that import:

* ``backend/app/core/config.py`` L36-L41 declares ``PROJECT_ID``, ``DATABASE_URL``,
  ``REDIS_URL``, ``SECRET_KEY``, ``ALGORITHM`` and ``ACCESS_TOKEN_EXPIRE_MINUTES`` with no
  defaults, so ``Settings()`` raises a Pydantic ``ValidationError`` while the environment
  is unset.
* ``backend/app/core/config.py`` L199-L217 opens a ``SecretManagerServiceClient`` and
  performs three ``access_secret_version`` reads on every instantiation, then overwrites
  ``DATABASE_URL``, ``REDIS_URL`` and ``SECRET_KEY`` with what they returned.
* ``backend/app/db/database.py`` L5-L10 calls ``get_settings()`` and ``create_engine()`` at
  module scope, so an unparseable ``DATABASE_URL`` raises
  ``sqlalchemy.exc.ArgumentError`` during import rather than on first connect.
* ``backend/app/core/security.py`` L43-L45 imports ``get_settings``, ``User`` and
  ``get_db``, so importing the security module alone triggers the three couplings above.
* No ``__init__.py`` exists anywhere under ``backend/``, so every ``backend.app`` package
  is a PEP 420 namespace package that re-exports nothing. The four route modules
  nevertheless import ``get_db`` from ``backend.app.db``, their schemas from
  ``backend.app.schema`` and their services from ``backend.app.services``; and
  ``CollaboratorSchema`` (imported by ``backend/app/api/collaboration.py`` L5),
  ``init_db`` (imported by ``backend/app/main.py`` L8) and the four ``*Service`` classes
  exist nowhere in the repository. :func:`_install_missing_module_attributes` binds test
  doubles for those names onto the existing packages; it creates no module file and
  modifies no application file.

Ordering is load-bearing. A consuming module binds ``get_settings`` into its own namespace
at the moment it is imported, so patching ``backend.app.core.config.get_settings``
afterwards does not reach it. Every override below is therefore installed before the first
``backend.app`` import that consumes it, in the order given by :data:`_BOOTSTRAP_STEPS`.

Nothing in this module reaches the network, and two application modules are never
imported: ``backend/app/tasks/background_jobs.py`` calls ``get_settings()`` at module scope
for the Celery broker, and ``backend/app/services/real_time_sync.py`` annotates ``Any`` and
``Callable`` without importing ``typing``, which raises ``NameError`` on Python 3.9.

Import of this module never raises. pytest imports ``conftest.py`` before collecting any
test module in the directory, and a conftest that raises aborts collection for the whole
directory. Any bootstrap failure is captured in :data:`_BOOTSTRAP_ERROR` and reported by the
autouse :func:`_require_application` fixture, which fails the tests that need the
application and leaves the collection of the other modules in this directory untouched.

Rationale for the choices made here is recorded in
``documentation/Security Decision Log.md``.
"""

import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# sys.path
# ---------------------------------------------------------------------------

# backend/tests/conftest.py -> backend/tests -> backend -> repository root.
_REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]


def _ensure_repo_root_on_sys_path() -> None:
    """Put the repository root on ``sys.path`` so ``backend.app.*`` resolves.

    ``python -m pytest`` from the repository root prepends the working directory, which
    makes the ``backend`` namespace package importable. Bare ``pytest`` does not: its
    prepend import mode inserts only the rootdir of each test file, which for this suite is
    ``backend/tests``. This adds the repository root under both invocations.

    Only the repository root is added. ``backend/``, ``backend/app/`` and
    ``backend/app/services/`` are deliberately never added: doing so would make
    ``app.main`` and ``calculation_engine`` resolvable and would silently change the
    documented collection errors of ``test_api.py`` and ``test_calculation_engine.py``.
    """
    root = str(_REPOSITORY_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


# ---------------------------------------------------------------------------
# Settings environment
# ---------------------------------------------------------------------------

# Every field ``backend/app/core/config.py`` L35-L107 declares, in that file's own casing.
# Pydantic v1 ``BaseSettings`` is case-insensitive, and the lower-case names are the names
# the application declares. ``ALLOWED_ORIGINS`` is a ``List[str]``, which Pydantic v1 reads
# from the environment as JSON. No value here is a real credential.
TEST_ENVIRONMENT: Dict[str, str] = {
    "PROJECT_ID": "test-project",
    # A parseable SQLAlchemy URL, required by the module-scope ``create_engine`` call in
    # backend/app/db/database.py L8-L10. The psycopg2 dialect is resolved without
    # connecting, and ``sslmode`` is a psycopg2 argument. No connection is ever opened
    # through this engine: ``_override_get_db`` replaces the ``get_db`` dependency for
    # every test.
    "DATABASE_URL": "postgresql+psycopg2://test:test@localhost:5432/testdb",
    "REDIS_URL": "redis://localhost:6379/0",
    "SECRET_KEY": "conftest-placeholder-signing-key-not-a-real-secret",
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "60",
    # A single loopback origin. The ``ALLOWED_ORIGINS`` validator at config.py L146-L195
    # permits http only for a loopback host.
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
    "gcs_bucket_name": "test-bucket",
    "signed_url_expiry_minutes": "15",
    # Empty selects the runtime's own identity; the validator at config.py L128-L142 skips
    # an empty value.
    "signer_service_account": "",
    "db_sslmode": "require",
    "auth_token_verifier": "firebase",
    # False would serve requests on unverified token claims, so no route would answer 401.
    "auth_enforcement_enabled": "true",
    "firebase_project_id": "test-project",
    # False would make ``register_rate_limiting`` register no middleware at all
    # (backend/app/core/rate_limit.py L234-L235).
    "rate_limit_enabled": "true",
    # Every request in a run shares one bucket: the counters are keyed by client address,
    # and ``TestClient`` presents a single constant host. Both windows are therefore wider
    # than any single test needs. A test that exercises a threshold narrows the window
    # itself.
    "rate_limit_default": "1000/minute",
    "rate_limit_write": "1000/minute",
    # ``resolve_storage`` (rate_limit.py L61-L98) falls back to ``REDIS_URL`` when this is
    # empty, and then calls ``storage_from_string`` and ``Storage.check()``, which opens a
    # socket. ``memory://`` returns a ``MemoryStorage`` without any network work.
    "rate_limit_storage_uri": "memory://",
    "rate_limit_trusted_proxy_hops": "0",
    # False makes the emitted header ``Content-Security-Policy``; true makes it
    # ``Content-Security-Policy-Report-Only`` (backend/app/core/security_headers.py
    # L71-L75).
    "csp_report_only": "false",
    # Empty means one origin serves the SPA and the API; the validator at config.py
    # L111-L124 skips an empty value.
    "api_origin": "",
}


def _populate_test_environment() -> None:
    """Set every ``Settings`` field in ``os.environ``, neutralising coupling 1.

    Addresses ``backend/app/core/config.py`` L36-L41, whose six defaultless fields make
    ``Settings()`` raise a Pydantic ``ValidationError`` while the environment is unset.

    Assignment is unconditional, so an ambient value never reaches ``Settings``.
    ``PROJECT_ID`` is already set in this build environment, and it selects both the Secret
    Manager paths built at config.py L203-L212 and the Firebase project pinned at
    security.py L216-L218.
    """
    for name, value in TEST_ENVIRONMENT.items():
        os.environ[name] = value


# ---------------------------------------------------------------------------
# Secret Manager
# ---------------------------------------------------------------------------

# Every secret name passed to the stubbed client, in call order. Appended to by
# :class:`_StubSecretManagerClient` and exposed by the :func:`secret_manager_requests`
# fixture.
SECRET_MANAGER_REQUESTS: List[str] = []

# Marks the segment of a Secret Manager resource name that precedes the secret's own id,
# as built at config.py L210-L212.
_SECRET_NAME_SEPARATOR: str = "/secrets/"


class _StubSecretManagerPayload:
    """The ``payload`` of a stubbed ``access_secret_version`` response."""

    def __init__(self, data: bytes) -> None:
        self.data = data


class _StubSecretManagerResponse:
    """A stubbed ``access_secret_version`` response exposing ``payload.data``."""

    def __init__(self, data: bytes) -> None:
        self.payload = _StubSecretManagerPayload(data)


class _StubSecretManagerClient:
    """Answers ``access_secret_version`` from :data:`TEST_ENVIRONMENT` without a network.

    Stands in for ``google.cloud.secretmanager.SecretManagerServiceClient``, which
    ``backend/app/core/config.py`` L209 constructs as a context manager and L210-L212 reads
    three secrets from on every ``Settings()`` instantiation. Implements the context
    manager protocol for that reason.

    Each secret resolves to its :data:`TEST_ENVIRONMENT` value, so the three fields
    config.py L215-L217 overwrites keep the values this module set.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._closed = False

    def __enter__(self) -> "_StubSecretManagerClient":
        """Return this client, as the ``with`` statement at config.py L209 expects."""
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        """Close this client and let any exception propagate."""
        self.close()
        return False

    def close(self) -> None:
        """Mark this client closed, matching the real client's closable contract."""
        self._closed = True

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has run."""
        return self._closed

    def access_secret_version(
        self, request: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> _StubSecretManagerResponse:
        """Return the :data:`TEST_ENVIRONMENT` value of the secret ``request`` names.

        Args:
            request: The mapping config.py L210-L212 passes, carrying a ``name`` of the
                form ``projects/{project}/secrets/{secret}/versions/latest``.
            **kwargs: Accepted and ignored, matching the client's keyword-argument form.

        Returns:
            _StubSecretManagerResponse: A response whose ``payload.data`` holds the
            UTF-8 encoded value. A secret this module sets no value for resolves to empty
            bytes.
        """
        name = (request or {}).get("name", "")
        SECRET_MANAGER_REQUESTS.append(name)
        secret_id = ""
        if _SECRET_NAME_SEPARATOR in name:
            secret_id = name.split(_SECRET_NAME_SEPARATOR, 1)[1].split("/", 1)[0]
        return _StubSecretManagerResponse(
            TEST_ENVIRONMENT.get(secret_id, "").encode("UTF-8")
        )


def _install_secret_manager_stub() -> None:
    """Replace the Secret Manager client class, neutralising coupling 2.

    Addresses ``backend/app/core/config.py`` L199-L217, which constructs a
    ``SecretManagerServiceClient`` and performs three live ``access_secret_version`` reads
    on every ``Settings()`` instantiation.

    The name is rebound on the ``secretmanager`` module object that config.py L2 imported,
    which is the attribute L209 looks the class up on.
    """
    import backend.app.core.config as config_module

    config_module.secretmanager.SecretManagerServiceClient = _StubSecretManagerClient


def _install_settings_override() -> None:
    """Rebind ``get_settings`` to a factory that constructs against the stub.

    Covers couplings 1, 2 and 3 together: with the environment populated and the Secret
    Manager client stubbed, ``Settings()`` validates, performs no network read, and yields
    a ``DATABASE_URL`` the module-scope ``create_engine`` call at
    ``backend/app/db/database.py`` L8-L10 can parse.

    The replacement is uncached, matching ``backend/app/core/config.py`` L219-L220. A
    changed value in ``os.environ`` is therefore picked up by the next call, with no cache
    to invalidate.
    """
    import backend.app.core.config as config_module

    def _test_get_settings() -> "config_module.Settings":
        return config_module.Settings()

    config_module.get_settings = _test_get_settings


# ---------------------------------------------------------------------------
# Service call recorder
# ---------------------------------------------------------------------------


class ServiceCall(BaseModel):
    """One recorded invocation of a stub service method.

    Attributes:
        service: Class name of the stub whose method ran.
        method: Method name.
        arguments: The call's arguments, keyed by parameter name. Values are held as
            repr strings so a recorded call never keeps a schema instance or a Session
            alive past the test that produced it.
    """

    service: str
    method: str
    arguments: Dict[str, str] = {}


# Every stub service method invocation, in call order. A route handler that is never
# reached appends nothing, which is what makes "the handler did not run" assertable.
# Exposed by the :func:`service_calls` fixture, which clears it around each test.
SERVICE_CALLS: List[ServiceCall] = []


def _record(service: str, method: str, **arguments: Any) -> None:
    """Append one invocation to :data:`SERVICE_CALLS`."""
    SERVICE_CALLS.append(
        ServiceCall(
            service=service,
            method=method,
            arguments={name: repr(value) for name, value in arguments.items()},
        )
    )


# ---------------------------------------------------------------------------
# Group B test doubles
# ---------------------------------------------------------------------------


class CollaboratorSchema(BaseModel):
    """Stub for the schema ``backend/app/api/collaboration.py`` L5 imports.

    ``backend/app/schema/workbook_schema.py`` defines ``CellSchema``,
    ``WorksheetSchema``, ``WorkbookSchema``, ``FormulaSchema`` and ``ChartSchema``, and no
    ``CollaboratorSchema``. Bound onto ``backend.app.schema`` by
    :func:`_install_missing_module_attributes`.

    Attributes:
        user_id: Identifier of the collaborator being added.
        permission: Optional access level. Optional so a body carrying ``user_id`` alone
            validates.
    """

    user_id: str
    permission: Optional[str] = None


class _StubService:
    """Base of the four service doubles, holding the Session the route passes.

    Each route constructs its service with the ``get_db`` dependency's Session. The doubles
    accept it and read nothing from it. Database state is reached through the
    :func:`db_session` fixture.
    """

    def __init__(self, db: Any = None) -> None:
        self.db = db


class WorkbookService(_StubService):
    """Stub for the service ``backend/app/api/workbooks.py`` L6 imports."""

    def get_workbooks(self, skip: int = 0, limit: int = 100) -> List[Any]:
        """Record the call and return no workbooks.

        An empty list validates against the handler's ``List[WorkbookSchema]`` return
        annotation, so ``GET /workbooks`` answers 200 with ``[]``.
        """
        _record("WorkbookService", "get_workbooks", skip=skip, limit=limit)
        return []

    def create_workbook(self, workbook: Any) -> Any:
        """Record the call and return ``workbook`` unchanged.

        The argument is already a validated ``WorkbookSchema``, so returning it validates
        against the handler's ``WorkbookSchema`` return annotation.
        """
        _record("WorkbookService", "create_workbook", workbook=workbook)
        return workbook


class WorksheetService(_StubService):
    """Stub for the service ``backend/app/api/worksheets.py`` L6 imports."""

    def get_worksheets(self, workbook_id: str) -> List[Any]:
        """Record the call and return no worksheets.

        ``backend/app/api/worksheets.py`` L18-L19 raises ``HTTPException(404)`` on an empty
        result, so ``GET /workbooks/{workbook_id}/worksheets`` answers 404. The handler body
        runs only after ``get_current_user`` has resolved, so a 404 from this route
        establishes that authentication succeeded.
        """
        _record("WorksheetService", "get_worksheets", workbook_id=workbook_id)
        return []


class CellService(_StubService):
    """Stub for the service ``backend/app/api/cells.py`` L6 imports."""

    def update_cells(
        self, workbook_id: str, worksheet_id: str, cells: List[Any]
    ) -> List[Any]:
        """Record the call and echo ``cells``.

        ``backend/app/api/cells.py`` L20 reports ``len()`` of this return value, so the
        response message counts the cells the request carried.
        """
        _record(
            "CellService",
            "update_cells",
            workbook_id=workbook_id,
            worksheet_id=worksheet_id,
            cells=cells,
        )
        return list(cells)


class CollaborationService(_StubService):
    """Stub for the service ``backend/app/api/collaboration.py`` L6 imports."""

    def share_workbook(self, workbook_id: str, collaborators: List[Any]) -> bool:
        """Record the call and report success.

        ``backend/app/api/collaboration.py`` L29-L30 discards this return value and answers
        with a fixed message.
        """
        _record(
            "CollaborationService",
            "share_workbook",
            workbook_id=workbook_id,
            collaborators=collaborators,
        )
        return True


# The four service names the route modules import from ``backend.app.services``, none of
# which exists in that package: it holds calculation_engine.py, file_storage.py and
# real_time_sync.py only.
_SERVICE_DOUBLES = (
    WorkbookService,
    WorksheetService,
    CellService,
    CollaborationService,
)

# The schema names re-exported from ``backend.app.schema.workbook_schema`` onto
# ``backend.app.schema``, which the route modules import them from.
_RE_EXPORTED_SCHEMAS = (
    "CellSchema",
    "WorksheetSchema",
    "WorkbookSchema",
    "FormulaSchema",
    "ChartSchema",
)


async def _init_db_noop() -> None:
    """Stand in for the ``init_db`` ``backend/app/main.py`` L8 imports.

    ``backend/app/db/database.py`` defines ``settings``, ``engine``, ``SessionLocal`` and
    ``get_db``, and no ``init_db``. ``main.py`` L12-L14 awaits it from the startup event
    handler, so a coroutine function that does nothing keeps that handler harmless whether
    or not ``TestClient`` is entered as a context manager.
    """
    return None


def _install_missing_module_attributes() -> None:
    """Bind the names the route modules and ``main.py`` import, neutralising coupling 5.

    No ``__init__.py`` exists under ``backend/``, so ``backend.app.db``,
    ``backend.app.schema`` and ``backend.app.services`` are PEP 420 namespace packages that
    re-export nothing. This binds onto those existing package objects:

    * ``backend.app.db.get_db``, aliasing ``backend.app.db.database.get_db``. The same
      function object is bound, so the key :func:`_override_get_db` registers in
      ``app.dependency_overrides`` is the object the route modules depend on.
    * ``backend.app.db.database.init_db``, read by ``main.py`` L8 at import time.
    * ``backend.app.schema`` re-exports of :data:`_RE_EXPORTED_SCHEMAS`, plus the
      :class:`CollaboratorSchema` double.
    * ``backend.app.services`` bindings for :data:`_SERVICE_DOUBLES`.

    Importing ``backend.app.db.database`` here is safe because
    :func:`_install_settings_override` has already run, so its module-scope
    ``create_engine`` call receives a parseable URL. No module file is created and no
    application file is modified.
    """
    import backend.app.db
    import backend.app.db.database
    import backend.app.schema
    import backend.app.schema.workbook_schema as workbook_schema
    import backend.app.services

    setattr(backend.app.db, "get_db", backend.app.db.database.get_db)
    setattr(backend.app.db.database, "init_db", _init_db_noop)

    for schema_name in _RE_EXPORTED_SCHEMAS:
        setattr(backend.app.schema, schema_name, getattr(workbook_schema, schema_name))
    setattr(backend.app.schema, "CollaboratorSchema", CollaboratorSchema)

    for service in _SERVICE_DOUBLES:
        setattr(backend.app.services, service.__name__, service)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

# The application object, and the application symbols the fixtures below need. All stay
# None while the bootstrap has not completed, and :data:`_BOOTSTRAP_ERROR` then holds the
# reason.
app: Optional[Any] = None
Base: Optional[Any] = None
User: Optional[Any] = None
get_db: Optional[Callable[..., Any]] = None
get_current_user: Optional[Callable[..., Any]] = None

# The exception the bootstrap failed with, or None. Reported by
# :func:`_require_application` rather than raised at import, so a failure here cannot abort
# collection of the whole directory.
_BOOTSTRAP_ERROR: Optional[Exception] = None

# The overrides, in the order they must run. Each one is installed before the first
# ``backend.app`` import that would consume what it replaces.
_BOOTSTRAP_STEPS = (
    _install_secret_manager_stub,
    _install_settings_override,
    _install_missing_module_attributes,
)


def _bootstrap_application() -> None:
    """Run the overrides in order, then import the application.

    ``backend/app/main.py`` L43-L49 calls ``configure_cors()``,
    ``register_rate_limiting(app)`` and ``include_routers()`` at module scope, so CORS
    configuration, both throttling tiers, the security-header middleware and all five
    routes are import side effects. Every override is therefore in place before the import
    on the last line of this function.
    """
    global app, Base, User, get_db, get_current_user

    for step in _BOOTSTRAP_STEPS:
        step()

    import backend.app.db
    from backend.app.core.security import get_current_user as _get_current_user
    from backend.app.db.models import Base as _Base
    from backend.app.db.models import User as _User
    from backend.app.main import app as _app

    app = _app
    Base = _Base
    User = _User
    get_db = backend.app.db.get_db
    get_current_user = _get_current_user


_ensure_repo_root_on_sys_path()
_populate_test_environment()
try:
    _bootstrap_application()
except Exception as exc:  # Captured, not propagated: see this module's docstring.
    _BOOTSTRAP_ERROR = exc


# ---------------------------------------------------------------------------
# Rate-limit window stores
# ---------------------------------------------------------------------------


def _rate_limit_storages(application: Any) -> List[Any]:
    """Return every window store the two throttling tiers count in.

    ``backend/app/core/rate_limit.py`` installs two tiers that hold separate stores: the
    write tier receives one as the ``storage`` keyword of its middleware registration
    (L249-L254), and slowapi's ``Limiter`` builds its own from a URI (L264-L271). Both are
    read here.

    The write tier's store is read from ``app.user_middleware``, where Starlette records
    each registration's keyword arguments, so it is reachable before the middleware stack
    has been built. Every attribute is probed with ``getattr``: slowapi's
    ``_fallback_storage`` exists only when its in-memory fallback is enabled, which
    ``register_rate_limiting`` does not enable.

    Args:
        application: The FastAPI application to inspect.

    Returns:
        List[Any]: The distinct stores exposing ``reset``, deduplicated by identity.
    """
    found: List[Any] = []
    seen = set()

    def collect(candidate: Any) -> None:
        if candidate is None or not hasattr(candidate, "reset"):
            return
        if id(candidate) in seen:
            return
        seen.add(id(candidate))
        found.append(candidate)

    limiter = getattr(getattr(application, "state", None), "limiter", None)
    if limiter is not None:
        collect(getattr(limiter, "_storage", None))
        collect(getattr(limiter, "_fallback_storage", None))
        collect(getattr(getattr(limiter, "limiter", None), "storage", None))

    for middleware in getattr(application, "user_middleware", ()) or ():
        candidates = list((getattr(middleware, "kwargs", None) or {}).values())
        candidates.extend(getattr(middleware, "args", ()) or ())
        for candidate in candidates:
            collect(candidate)
            collect(getattr(getattr(candidate, "_limiter", None), "storage", None))

    return found


def reset_rate_limit_state(application: Optional[Any] = None) -> None:
    """Clear the counters of both throttling tiers.

    Both tiers registered by ``backend/app/core/rate_limit.py`` L249-L273 count in the
    ``MemoryStorage`` that ``resolve_storage`` (L61-L98) returns for a ``memory://`` URI, and
    both key their windows on the client address ``resolve_client_key`` (L101-L131) derives.
    ``TestClient`` presents one constant host, so every test in a run spends the same budget.
    Without this the number of requests a test may make depends on how many the tests before
    it made.

    Args:
        application: The application whose stores are cleared. Defaults to the application
            this module bootstrapped. A no-op while that bootstrap has not completed, and
            while ``rate_limit_enabled`` is false, in which case there is no store to clear.
    """
    target = application if application is not None else app
    if target is None:
        return
    for storage in _rate_limit_storages(target):
        storage.reset()


# ---------------------------------------------------------------------------
# Request body builders
# ---------------------------------------------------------------------------

# Fixed timestamp for the two datetime fields ``WorkbookSchema`` requires, so a request
# body is byte-for-byte the same on every run.
_FIXED_TIMESTAMP: str = "2024-01-01T00:00:00"


def workbook_payload(**overrides: Any) -> Dict[str, Any]:
    """Return a body that satisfies ``WorkbookSchema``.

    ``backend/app/schema/workbook_schema.py`` L20-L27 requires ``id``, ``name``,
    ``owner_id``, ``worksheets``, ``created_at`` and ``modified_at``; ``settings`` is
    optional and omitted.

    Args:
        **overrides: Field values replacing the defaults.

    Returns:
        Dict[str, Any]: The request body.
    """
    body: Dict[str, Any] = {
        "id": "test-workbook",
        "name": "Test Workbook",
        "owner_id": "1",
        "worksheets": [],
        "created_at": _FIXED_TIMESTAMP,
        "modified_at": _FIXED_TIMESTAMP,
    }
    body.update(overrides)
    return body


def cell_payload(**overrides: Any) -> Dict[str, Any]:
    """Return a body that satisfies ``CellSchema``.

    ``backend/app/schema/workbook_schema.py`` L5-L8 requires ``value`` and ``style``;
    ``formula`` is optional and omitted.

    Args:
        **overrides: Field values replacing the defaults.

    Returns:
        Dict[str, Any]: The request body.
    """
    body: Dict[str, Any] = {"value": "1", "style": {}}
    body.update(overrides)
    return body


def collaborator_payload(**overrides: Any) -> Dict[str, Any]:
    """Return a body that satisfies the :class:`CollaboratorSchema` double.

    Args:
        **overrides: Field values replacing the defaults.

    Returns:
        Dict[str, Any]: The request body.
    """
    body: Dict[str, Any] = {"user_id": "collaborator-1", "permission": "view"}
    body.update(overrides)
    return body


def bearer_headers(token: str) -> Dict[str, str]:
    """Return an ``Authorization`` header carrying ``token`` as a bearer credential.

    ``oauth2_scheme`` (``backend/app/core/security.py`` L88) reads the credential from this
    header.

    Args:
        token: The bearer token value.

    Returns:
        Dict[str, str]: The request headers.
    """
    return {"Authorization": "Bearer {0}".format(token)}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _require_application() -> None:
    """Fail the test if the bootstrap did not complete.

    pytest imports ``backend/tests/conftest.py`` before it collects any module in
    ``backend/tests``, and an exception raised during that import is reported as a single
    conftest error that aborts collection of the whole directory. The ordered bootstrap
    therefore stores its failure in :data:`_BOOTSTRAP_ERROR`, and this reports it here:
    against the tests that need the application, instead of against the directory.
    """
    if _BOOTSTRAP_ERROR is not None:
        pytest.fail(
            "The application could not be bootstrapped: "
            "{0}: {1}".format(type(_BOOTSTRAP_ERROR).__name__, _BOOTSTRAP_ERROR),
            pytrace=False,
        )


@pytest.fixture
def db_session(_require_application: None) -> Iterator[Any]:
    """Yield a Session bound to a private in-memory SQLite database.

    ``backend/app/db/database.py`` L8-L10 builds its engine at module scope with
    ``connect_args={"sslmode": ...}``, a psycopg2 argument SQLite's driver rejects, and that
    engine addresses PostgreSQL. This fixture builds its own engine, so no test reaches that
    one.

    ``StaticPool`` keeps every checkout on one connection, which is what makes a
    ``:memory:`` database outlive an individual connection and stay visible to the request
    handled by ``TestClient``.

    ``backend/app/db/models.py`` L29 declares ``Workbook.owner`` with
    ``back_populates="workbooks"``, which requires a matching ``User.workbooks``. L17
    declares it, so the reciprocal is attached below only if it is absent, and
    ``configure_mappers()`` then reports any remaining mapper fault here rather than on the
    first query.

    The schema is created per test and dropped on teardown, so no row survives into another
    test.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import configure_mappers, relationship, sessionmaker
    from sqlalchemy.pool import StaticPool

    if not hasattr(User, "workbooks"):
        User.workbooks = relationship("Workbook", back_populates="owner")
    configure_mappers()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def _override_get_db(_require_application: None, db_session: Any) -> Iterator[None]:
    """Route the ``get_db`` dependency to :func:`db_session` for every test.

    Registered under the exact function object the four route modules depend on. Each of
    ``backend/app/api/workbooks.py``, ``worksheets.py``, ``cells.py`` and
    ``collaboration.py`` L4 imports ``get_db`` from ``backend.app.db``, and
    :func:`_install_missing_module_attributes` binds that name to the ``get_db`` defined at
    ``backend/app/db/database.py`` L13-L18, so both import paths name one object. A different
    object as the key would leave the override unused and every route would open a
    PostgreSQL connection through the module-scope engine at database.py L8-L10.

    Autouse, so no test reaches that engine by omitting the fixture. The key is removed on
    teardown.
    """

    def _get_test_db() -> Iterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _get_test_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limits(_require_application: None) -> Iterator[None]:
    """Clear both throttling tiers' counters around every test.

    Calls :func:`reset_rate_limit_state` before and after each test, so a test starts with a
    full budget however many requests the tests before it made, and leaves a full budget
    behind. The counters belong to the two tiers ``register_rate_limiting`` installs at
    ``backend/app/core/rate_limit.py`` L249-L273 and outlive an individual test, since they
    are held in process memory for as long as the application object exists.
    """
    reset_rate_limit_state()
    try:
        yield
    finally:
        reset_rate_limit_state()


@pytest.fixture
def client(_require_application: None) -> Iterator[Any]:
    """Yield a ``TestClient`` for the application, with no credential attached.

    Constructed without the context-manager form, so ``main.py`` L12-L14's startup event is
    not run. Requests carry no ``Authorization`` header, which is what the 401 assertions
    need.
    """
    from fastapi.testclient import TestClient

    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()


@pytest.fixture
def fake_user(_require_application: None) -> Any:
    """Return a ``User`` instance that is attached to no Session.

    ``get_current_user`` (``backend/app/core/security.py`` L479-L511) returns a user
    detached from the Session that resolved it, so the column values are readable and no
    attribute lazy-loads. This instance behaves the same way.
    """
    from datetime import datetime

    return User(
        id=1,
        email="test-user@example.test",
        name="Test User",
        created_at=datetime(2024, 1, 1),
    )


@pytest.fixture
def authenticated_client(
    _require_application: None, fake_user: Any
) -> Iterator[Any]:
    """Yield a ``TestClient`` whose requests resolve to :func:`fake_user`.

    Overrides the ``get_current_user`` defined at ``backend/app/core/security.py``
    L479-L511, which the five handlers depend on. The override replaces token verification
    and the identity lookup for the duration of the test, and its key is removed on
    teardown.

    Not autouse and not applied by :func:`client`. A test asserting that an unauthenticated
    request answers 401 must use :func:`client`: with this override installed every request
    is admitted, so no 401 assertion can hold.
    """
    from fastapi.testclient import TestClient

    def _override_get_current_user() -> Any:
        return fake_user

    app.dependency_overrides[get_current_user] = _override_get_current_user
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def service_calls() -> Iterator[List[ServiceCall]]:
    """Yield the stub service invocation log, empty at the start of the test.

    Each of the five handlers constructs its service on the first line of its body -
    ``backend/app/api/workbooks.py`` L15 and L24, ``worksheets.py`` L15, ``cells.py`` L18 and
    ``collaboration.py`` L28 - and a handler body runs only after every dependency it
    declares has resolved. A request rejected by ``get_current_user`` therefore records
    nothing here, and an empty log after such a request is what establishes that the handler
    was never reached.
    """
    SERVICE_CALLS.clear()
    try:
        yield SERVICE_CALLS
    finally:
        SERVICE_CALLS.clear()


@pytest.fixture
def secret_manager_requests() -> Iterator[List[str]]:
    """Yield the Secret Manager resource names requested, empty at the start of the test.

    Every entry was answered by :class:`_StubSecretManagerClient`, so the presence of
    entries records reads that were served locally rather than reads that reached Google.
    """
    SECRET_MANAGER_REQUESTS.clear()
    try:
        yield SECRET_MANAGER_REQUESTS
    finally:
        SECRET_MANAGER_REQUESTS.clear()


@pytest.fixture
def settings(_require_application: None) -> Any:
    """Return a freshly constructed ``Settings`` for the current environment.

    Built through the override :func:`_install_settings_override` installed, so the values
    are the ones :data:`TEST_ENVIRONMENT` set and the three Secret Manager reads at
    ``backend/app/core/config.py`` L210-L212 are answered locally.
    """
    from backend.app.core.config import get_settings

    return get_settings()
