"""Automated verification of the security controls installed by this remediation.

One group per vulnerability identifier, in the order V1, V2, V3, V4, V5, V6, V7, V9,
V12. Every test name carries the identifier of the vulnerability it covers, so the
coverage recorded in ``documentation/Security Traceability Matrix.md`` is checkable by
collecting this module.

Two vulnerabilities are verified elsewhere and are absent here. V10, the Firestore
security rules, is verified with the Firebase emulator; V11, the Cloud Function invoker
binding, is verified against a deployed function. Both procedures are recorded in
``documentation/Security Traceability Matrix.md``.

No owner-scoped authorization test appears here. No in-handler ownership check is part of
this remediation, so an authenticated caller can still address another caller's
``workbook_id``; that residual is recorded in ``SECURITY.md``.

Every test runs offline. Nothing here reaches Secret Manager, a Firebase project, Cloud
Storage or PostgreSQL: ``backend/tests/conftest.py`` neutralises the import-time couplings,
and each test that reaches a Google client replaces it.

The identity lookup at ``backend/app/core/security.py`` L450-L471 calls ``get_db()``
directly, so ``app.dependency_overrides`` does not intercept it and the lookup addresses
PostgreSQL. The V3 group exercises token verification failures, which are answered before
that lookup runs.

Rationale for the choices made here is recorded in
``documentation/Security Decision Log.md``.
"""

import contextlib
import importlib
import importlib.util
import inspect
import typing
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Tuple
from unittest import mock

import conftest
import pytest
import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth import credentials as google_credentials
from jose import jwt

from backend.app.core import security
from backend.app.core.rate_limit import register_rate_limiting
from backend.app.core.security_headers import (
    CONTENT_SECURITY_POLICY,
    CONTENT_SECURITY_POLICY_HEADER,
    CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER,
    STATIC_SECURITY_HEADERS,
    SecurityHeadersMiddleware,
)
from backend.app.db import database
from backend.app.services import file_storage

# ---------------------------------------------------------------------------
# The five protected routes
# ---------------------------------------------------------------------------

# Path parameters. Any value routes, since no handler reads storage: the four service
# doubles in backend/tests/conftest.py answer every call.
WORKBOOK_ID = "wb-1"
WORKSHEET_ID = "ws-1"

WORKBOOKS_PATH = "/workbooks"
WORKSHEETS_PATH = f"/workbooks/{WORKBOOK_ID}/worksheets"
CELLS_PATH = f"/workbooks/{WORKBOOK_ID}/worksheets/{WORKSHEET_ID}/cells"
SHARE_PATH = f"/workbooks/{WORKBOOK_ID}/share"

# The same four routes as the OpenAPI document names them.
PUBLISHED_WORKSHEETS_PATH = "/workbooks/{workbook_id}/worksheets"
PUBLISHED_CELLS_PATH = "/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells"
PUBLISHED_SHARE_PATH = "/workbooks/{workbook_id}/share"

UNAUTHORIZED_STATUS = 401
TOO_MANY_REQUESTS_STATUS = 429
BAD_REQUEST_STATUS = 400
OK_STATUS = 200
NOT_FOUND_STATUS = 404

# The challenge backend/app/core/security.py L433-L437 attaches to every rejection, and
# which FastAPI's own OAuth2PasswordBearer attaches when no Authorization header is sent.
WWW_AUTHENTICATE_HEADER = "WWW-Authenticate"
BEARER_CHALLENGE = "Bearer"


class ProtectedRoute(NamedTuple):
    """One route that requires an authenticated caller.

    Attributes:
        name: Identifier used as the parametrisation id, so a failure names the route.
        method: HTTP method.
        path: Request path. The five routes are mounted with no router prefix.
        published_path: The same route as it appears in the OpenAPI document, with path
            parameters unsubstituted.
        build_body: Returns a fresh request body, or None for a route that carries none.
        authenticated_status: Status an authenticated caller receives.
        expected_json: Decoded body an authenticated caller receives.
        service: Class name of the service double the handler constructs.
        service_method: Method name the handler calls on that double.
    """

    name: str
    method: str
    path: str
    published_path: str
    build_body: Callable[[], Optional[Any]]
    authenticated_status: int
    expected_json: Any
    service: str
    service_method: str


# The complete set of routes backend/app/main.py L49 mounts. V1 is verified against every
# entry, so the five-route coverage is collected.
#
# get_worksheets answers 404: the WorksheetService double returns no worksheets and
# backend/app/api/worksheets.py L18-L19 raises on an empty result. The handler body runs
# only after get_current_user has resolved, so that 404 establishes that authentication
# succeeded.
PROTECTED_ROUTES: Tuple[ProtectedRoute, ...] = (
    ProtectedRoute(
        name="get_workbooks",
        method="GET",
        path=WORKBOOKS_PATH,
        published_path=WORKBOOKS_PATH,
        build_body=lambda: None,
        authenticated_status=OK_STATUS,
        expected_json=[],
        service="WorkbookService",
        service_method="get_workbooks",
    ),
    ProtectedRoute(
        name="create_workbook",
        method="POST",
        path=WORKBOOKS_PATH,
        published_path=WORKBOOKS_PATH,
        build_body=conftest.workbook_payload,
        authenticated_status=OK_STATUS,
        # The double echoes the validated schema, and WorkbookSchema.settings is optional
        # and omitted from the request body, so it serialises as null.
        expected_json=dict(conftest.workbook_payload(), settings=None),
        service="WorkbookService",
        service_method="create_workbook",
    ),
    ProtectedRoute(
        name="get_worksheets",
        method="GET",
        path=WORKSHEETS_PATH,
        published_path=PUBLISHED_WORKSHEETS_PATH,
        build_body=lambda: None,
        authenticated_status=NOT_FOUND_STATUS,
        expected_json={"detail": "No worksheets found for the given workbook"},
        service="WorksheetService",
        service_method="get_worksheets",
    ),
    ProtectedRoute(
        name="update_cells",
        method="PUT",
        path=CELLS_PATH,
        published_path=PUBLISHED_CELLS_PATH,
        build_body=lambda: [conftest.cell_payload()],
        authenticated_status=OK_STATUS,
        expected_json={"message": "Successfully updated 1 cells"},
        service="CellService",
        service_method="update_cells",
    ),
    ProtectedRoute(
        name="share_workbook",
        method="POST",
        path=SHARE_PATH,
        published_path=PUBLISHED_SHARE_PATH,
        build_body=lambda: [conftest.collaborator_payload()],
        authenticated_status=OK_STATUS,
        expected_json={"message": "Workbook shared successfully"},
        service="CollaborationService",
        service_method="share_workbook",
    ),
)

ROUTE_IDS: List[str] = [route.name for route in PROTECTED_ROUTES]

# The paths and methods the public contract exposed before the authentication dependency
# was attached. A dependency parameter is not part of the request contract, so this set is
# unchanged by V1's fix.
EXPECTED_OPENAPI_OPERATIONS: Dict[str, List[str]] = {
    WORKBOOKS_PATH: ["get", "post"],
    PUBLISHED_WORKSHEETS_PATH: ["get"],
    PUBLISHED_CELLS_PATH: ["put"],
    PUBLISHED_SHARE_PATH: ["post"],
}

# The query parameters GET /workbooks declared before the dependency was attached.
EXPECTED_GET_WORKBOOKS_PARAMETERS: List[str] = ["limit", "skip"]


def request_route(
    test_client: Any, route: ProtectedRoute, headers: Optional[Dict[str, str]] = None
) -> Any:
    """Send ``route``'s request through ``test_client``.

    Args:
        test_client: The ``TestClient`` to send through. Which one decides whether the
            request carries a credential: ``client`` attaches none, and
            ``authenticated_client`` overrides ``get_current_user``.
        route: The route to address.
        headers: Extra request headers, or None.

    Returns:
        The response.
    """
    return test_client.request(
        route.method, route.path, json=route.build_body(), headers=headers
    )


def recorded_calls(calls: List[conftest.ServiceCall]) -> List[Tuple[str, str]]:
    """Return ``(service, method)`` for each recorded stub service invocation."""
    return [(call.service, call.method) for call in calls]


# ---------------------------------------------------------------------------
# Rejected credentials
# ---------------------------------------------------------------------------

# A bearer value that is not a JWT. Verification fails while decoding it, before any
# signing certificate is fetched, so no test reaches the network.
FORGED_TOKEN = "not.a.real.token"

# The only body a rejected caller receives, from the exception built at
# backend/app/core/security.py L433-L437.
CREDENTIALS_REJECTION_BODY: Dict[str, str] = {
    "detail": "Could not validate credentials"
}

# Substrings that would indicate the verification failure reached the caller. Checked
# case-insensitively against the serialised body.
LEAKED_CAUSE_MARKERS: Tuple[str, ...] = (
    "Traceback",
    "firebase",
    "ValueError",
    "Expecting",
    "jose",
    "JWT",
)

# Carried by every verification failure raised below. Distinctive enough that finding it
# anywhere in a response body proves the cause was echoed.
VERIFICATION_FAILURE_DETAIL = (
    "signature mismatch for signing key material 0xDEADBEEF"
)


class VerificationFault(NamedTuple):
    """One failure the token verification path must answer with a rejection.

    Attributes:
        name: Parametrisation id.
        error: The exception ``verify_id_token`` raises.
    """

    name: str
    error: BaseException


# Every failure backend/app/core/security.py L221-L250 catches from
# ``firebase_auth.verify_id_token``. The bare ValueError is raised when the Admin SDK
# cannot be used; without its clause that fault would surface as a 500 rather than a 401.
VERIFICATION_FAULTS: Tuple[VerificationFault, ...] = (
    VerificationFault(
        name="invalid_token",
        error=security.firebase_auth.InvalidIdTokenError(VERIFICATION_FAILURE_DETAIL),
    ),
    VerificationFault(
        name="expired_token",
        error=security.firebase_auth.ExpiredIdTokenError(
            VERIFICATION_FAILURE_DETAIL, cause=None
        ),
    ),
    VerificationFault(
        name="revoked_token",
        error=security.firebase_auth.RevokedIdTokenError(VERIFICATION_FAILURE_DETAIL),
    ),
    VerificationFault(
        name="certificate_fetch_failure",
        error=security.firebase_auth.CertificateFetchError(
            VERIFICATION_FAILURE_DETAIL, cause=None
        ),
    ),
    VerificationFault(
        name="uninitialised_admin_sdk",
        error=ValueError(VERIFICATION_FAILURE_DETAIL),
    ),
)

FAULT_IDS: List[str] = [fault.name for fault in VERIFICATION_FAULTS]

# ---------------------------------------------------------------------------
# Object storage
# ---------------------------------------------------------------------------

SIGNED_URL_SENTINEL = "https://storage.example.test/signed-object"
UPLOAD_CONTENT = b"workbook-bytes"
UPLOAD_FILE_NAME = "workbook.xlsx"

# Returned by the mocked blob, and passed back to generate_signed_url so the signature is
# bound to the generation just written.
BLOB_GENERATION = 1712345678

# ---------------------------------------------------------------------------
# Cross-origin requests
# ---------------------------------------------------------------------------

DISALLOWED_ORIGIN = "https://evil.example"

# A header no route declares. The pre-fix middleware reflected any requested header
# verbatim, because allow_headers was a wildcard.
ARBITRARY_REQUEST_HEADER = "X-Anything"

# The two headers backend/app/main.py L25 admits.
DECLARED_REQUEST_HEADERS = "authorization,content-type"

# The four methods backend/app/main.py L24 admits. No route uses DELETE.
EXPECTED_ALLOWED_METHODS = {"GET", "POST", "PUT", "OPTIONS"}

CORS_WILDCARD = "*"

# Starlette's body for a preflight from an origin outside the allow-list.
DISALLOWED_ORIGIN_BODY = "Disallowed CORS origin"

ORIGIN_HEADER = "Origin"
REQUEST_METHOD_HEADER = "Access-Control-Request-Method"
REQUEST_HEADERS_HEADER = "Access-Control-Request-Headers"
ALLOW_ORIGIN_HEADER = "Access-Control-Allow-Origin"
ALLOW_CREDENTIALS_HEADER = "Access-Control-Allow-Credentials"
ALLOW_METHODS_HEADER = "Access-Control-Allow-Methods"
ALLOW_HEADERS_HEADER = "Access-Control-Allow-Headers"

# ---------------------------------------------------------------------------
# Database transport
# ---------------------------------------------------------------------------

# Module name under which backend/app/db/database.py is executed a second time. Distinct
# from the real module's name, so the already-imported module keeps its identity and the
# get_db object that backend/tests/conftest.py registered in app.dependency_overrides
# stays the object the routes depend on.
DATABASE_PROBE_MODULE_NAME = "_security_test_database_probe"

# Module name under which backend/app/core/security.py is executed a second time.
SECURITY_PROBE_MODULE_NAME = "_security_test_security_probe"

CONNECT_ARGS_KEYWORD = "connect_args"
SSL_MODE_KEYWORD = "sslmode"

# The libpq transport modes that cannot negotiate plaintext, matching the DatabaseSslMode
# literal at backend/app/core/config.py L32. disable, allow and prefer are excluded.
TLS_ONLY_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})

# ---------------------------------------------------------------------------
# Security response headers
# ---------------------------------------------------------------------------

# The required set. csp_report_only is false in the test settings, so the policy arrives
# under the enforcing header name.
REQUIRED_SECURITY_HEADERS: Tuple[str, ...] = (
    CONTENT_SECURITY_POLICY_HEADER,
    "Strict-Transport-Security",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
)

# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

PROBE_PATH = "/probe"

# A window wide enough that the tier holding it never rejects during a test.
GENEROUS_WINDOW = "1000/minute"

# The write tier's window while the write tier is under test, and the rendering of it that
# reaches the caller in the rejection body.
WRITE_PROBE_WINDOW = "2/minute"
WRITE_PROBE_LIMIT = 2
WRITE_PROBE_WINDOW_DESCRIPTION = "2 per 1 minute"

# The ceiling tier's window while the ceiling tier is under test.
CEILING_PROBE_WINDOW = "3/minute"
CEILING_PROBE_LIMIT = 3
CEILING_PROBE_WINDOW_DESCRIPTION = "3 per 1 minute"

# Requests sent past a limit, so the sequence shows the transition and that the rejection
# persists.
REQUESTS_PAST_LIMIT = 2

RETRY_AFTER_HEADER = "Retry-After"

RATE_LIMIT_DEFAULT_SETTING = "rate_limit_default"
RATE_LIMIT_WRITE_SETTING = "rate_limit_write"

# Class names of the two tiers backend/app/core/rate_limit.py L249-L273 installs.
CEILING_TIER_MIDDLEWARE_NAME = "SlowAPIMiddleware"
WRITE_TIER_MIDDLEWARE_NAME = "_WriteMethodRateLimitMiddleware"

# ---------------------------------------------------------------------------
# Token lifetime
# ---------------------------------------------------------------------------

TOKEN_CLAIMS: Dict[str, str] = {"sub": "1"}

# A lifetime passed explicitly, distinct from ACCESS_TOKEN_EXPIRE_MINUTES so the two
# directions are distinguishable.
EXPLICIT_LIFETIME = timedelta(minutes=5)

# The lifetime backend/app/core/security.py L25 hardcoded before V12 was fixed.
PREVIOUSLY_HARDCODED_LIFETIME = timedelta(minutes=15)

# Absorbs the second the exp claim loses to integer truncation, and scheduling delay
# between minting and decoding. Far below the smallest gap between the lifetimes compared
# here, so it cannot mask one being served for another.
LIFETIME_TOLERANCE = timedelta(seconds=60)

EXPIRY_CLAIM = "exp"
EXPIRES_DELTA_PARAMETER = "expires_delta"
EXPECTED_TOKEN_PARAMETERS: List[str] = ["data", EXPIRES_DELTA_PARAMETER]



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def missing_security_headers(response: Any) -> List[str]:
    """Return the names in :data:`REQUIRED_SECURITY_HEADERS` absent from ``response``."""
    return [name for name in REQUIRED_SECURITY_HEADERS if name not in response.headers]


def preflight(
    test_client: Any, origin: str, requested_headers: Optional[str] = None
) -> Any:
    """Send a CORS preflight for ``GET /workbooks`` from ``origin``.

    ``Access-Control-Request-Method`` is always sent: without it Starlette does not treat
    the request as a preflight and the CORS middleware does not answer it.

    Args:
        test_client: The ``TestClient`` to send through.
        origin: Value of the ``Origin`` header.
        requested_headers: Value of ``Access-Control-Request-Headers``, or None to omit it.

    Returns:
        The preflight response.
    """
    headers = {ORIGIN_HEADER: origin, REQUEST_METHOD_HEADER: "GET"}
    if requested_headers is not None:
        headers[REQUEST_HEADERS_HEADER] = requested_headers
    return test_client.options(WORKBOOKS_PATH, headers=headers)


def execute_module_afresh(module_name: str, source_module: Any) -> Any:
    """Execute ``source_module``'s file again under ``module_name`` and return the result.

    The new module object is not registered in ``sys.modules``, so the already-imported
    module keeps its identity and every object other modules hold a reference to - the
    ``get_db`` function among them - stays the object they hold.

    Args:
        module_name: Name to execute under. Distinct from the real module's name.
        source_module: The imported module whose ``__file__`` is executed.

    Returns:
        The freshly executed module.
    """
    spec = importlib.util.spec_from_file_location(module_name, source_module.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_engine_call() -> Any:
    """Return the ``create_engine`` call ``backend/app/db/database.py`` makes.

    Reports the arguments of the call. The engine object is not introspected; see
    ``documentation/Security Decision Log.md``.

    Returns:
        The ``unittest.mock`` call object recording the single invocation.
    """
    with mock.patch.object(sqlalchemy, "create_engine") as spy:
        execute_module_afresh(DATABASE_PROBE_MODULE_NAME, database)
    assert spy.call_count == 1
    return spy.call_args


def token_lifetime(
    signing_key: str, algorithm: str, expires_delta: Optional[timedelta]
) -> timedelta:
    """Return how long a token minted with ``expires_delta`` stays valid.

    The ``exp`` claim is read with expiry verification switched off, so the measurement
    never depends on the clock having advanced past it.

    Args:
        signing_key: ``Settings.SECRET_KEY``, the key the token is signed with.
        algorithm: ``Settings.ALGORITHM``, the algorithm it is signed with.
        expires_delta: Passed through to ``create_access_token``.

    Returns:
        The interval between minting and the ``exp`` claim.
    """
    issued_at = datetime.utcnow()
    token = security.create_access_token(dict(TOKEN_CLAIMS), expires_delta)
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=[algorithm],
        options={"verify_exp": False},
    )
    return datetime.utcfromtimestamp(claims[EXPIRY_CLAIM]) - issued_at


@contextlib.contextmanager
def throttling_probe(
    ceiling_window: str, write_window: str
) -> Iterator[Any]:
    """Yield a ``TestClient`` for a throwaway application carrying both throttling tiers.

    Registration order mirrors ``backend/app/main.py`` L45-L48: ``register_rate_limiting``
    first, then ``SecurityHeadersMiddleware`` last, so the header middleware is outermost
    here as it is there. This application is additional to the assertions made against the
    real one, not a substitute for them: the real application is asserted to carry both
    tiers and to have the header middleware outermost.

    Both handlers are undecorated and take no ``request`` parameter, which is what the
    middleware design requires of a route handler.

    The windows are narrowed through the environment for the duration of the registration
    call only. ``get_settings()`` is uncached, so the narrowed values reach
    ``register_rate_limiting`` and nothing else observes them. Each tier builds its own
    in-memory window store, so no counter is shared with the real application, and both
    stores are cleared on entry and on exit.

    Args:
        ceiling_window: ``rate_limit_default`` for this application.
        write_window: ``rate_limit_write`` for this application.

    Yields:
        A ``TestClient`` addressing :data:`PROBE_PATH`.
    """
    application = FastAPI()

    @application.get(PROBE_PATH)
    def read_probe() -> Dict[str, bool]:
        return {"served": True}

    @application.post(PROBE_PATH)
    def write_probe() -> Dict[str, bool]:
        return {"served": True}

    with mock.patch.dict(
        "os.environ",
        {
            RATE_LIMIT_DEFAULT_SETTING: ceiling_window,
            RATE_LIMIT_WRITE_SETTING: write_window,
        },
    ):
        register_rate_limiting(application)
        application.add_middleware(SecurityHeadersMiddleware)

    conftest.reset_rate_limit_state(application)
    test_client = TestClient(application)
    try:
        yield test_client
    finally:
        test_client.close()
        conftest.reset_rate_limit_state(application)


def send_repeatedly(test_client: Any, method: str, attempts: int) -> List[Any]:
    """Send ``attempts`` requests to :data:`PROBE_PATH` and return every response."""
    return [test_client.request(method, PROBE_PATH) for _ in range(attempts)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def allowed_origin(settings: Any) -> str:
    """Return the single origin ``backend/app/main.py`` L22 admits."""
    return settings.ALLOWED_ORIGINS[0]


class Upload(NamedTuple):
    """The outcome of one ``FileStorageService.upload_file`` call against mocked storage.

    Attributes:
        client: The mocked ``storage.Client``.
        bucket: The mocked bucket the client returned.
        blob: The mocked blob the bucket returned.
        returned: The value ``upload_file`` returned.
    """

    client: Any
    bucket: Any
    blob: Any
    returned: str


@pytest.fixture
def upload(settings: Any) -> Upload:
    """Run ``FileStorageService.upload_file`` with Cloud Storage and credentials mocked.

    ``FileStorageService.__init__`` constructs a ``storage.Client`` and reads
    ``gcs_bucket_name``, ``signed_url_expiry_minutes`` and ``signer_service_account``;
    ``upload_file`` resolves Application Default Credentials before writing. The client is
    replaced, and the credentials are replaced by an object that satisfies
    ``google.auth.credentials.Signing``, which is the branch at
    ``backend/app/services/file_storage.py`` L88-L92 that signs without minting an access
    token. Nothing reaches Cloud Storage, IAM or the metadata server.

    Returns:
        Upload: The mocks and the returned value, for the V4 assertions to read.
    """
    blob = mock.MagicMock()
    blob.generate_signed_url.return_value = SIGNED_URL_SENTINEL
    blob.generation = BLOB_GENERATION

    bucket = mock.MagicMock()
    bucket.blob.return_value = blob

    client = mock.MagicMock()
    client.bucket.return_value = bucket

    local_signer = mock.MagicMock(spec=google_credentials.Signing)

    with mock.patch.object(
        file_storage.storage, "Client", return_value=client
    ), mock.patch(
        "google.auth.default", return_value=(local_signer, settings.PROJECT_ID)
    ):
        service = file_storage.FileStorageService()
        returned = service.upload_file(UPLOAD_CONTENT, UPLOAD_FILE_NAME)

    return Upload(client=client, bucket=bucket, blob=blob, returned=returned)



# ---------------------------------------------------------------------------
# Test cases for V1 - authentication enforced on every route
# ---------------------------------------------------------------------------
#
# Threat: every route was reachable with no credential, so any caller who knew a URL
# read and wrote workbook data.
#
# `authenticated_client` installs its `get_current_user` override while fixtures are set
# up, so every request a test makes after holding that fixture is admitted. The
# unauthenticated cases take `client` alone.


@pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=ROUTE_IDS)
def test_v1_route_rejects_an_unauthenticated_request(
    client: Any, route: ProtectedRoute
) -> None:
    """A request carrying no Authorization header is answered 401 with a challenge."""
    response = request_route(client, route)

    assert response.status_code == UNAUTHORIZED_STATUS
    assert response.headers.get(WWW_AUTHENTICATE_HEADER) == BEARER_CHALLENGE


@pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=ROUTE_IDS)
def test_v1_route_does_not_reach_its_handler_unauthenticated(
    client: Any, service_calls: List[conftest.ServiceCall], route: ProtectedRoute
) -> None:
    """A rejected request runs no handler body, so no service call is recorded.

    Each handler constructs its service on the first line of its body, and a body runs
    only once every dependency it declares has resolved.
    """
    request_route(client, route)

    assert recorded_calls(service_calls) == []


@pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=ROUTE_IDS)
def test_v1_route_admits_an_authenticated_caller(
    authenticated_client: Any, route: ProtectedRoute
) -> None:
    """A request resolving to a user is served the route's own outcome.

    get_worksheets answers 404 on a workbook with no worksheets. That status is reached
    from inside the handler body, so it establishes that authentication succeeded.
    """
    response = request_route(authenticated_client, route)

    assert response.status_code != UNAUTHORIZED_STATUS
    assert response.status_code == route.authenticated_status
    assert response.json() == route.expected_json


@pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=ROUTE_IDS)
def test_v1_route_reaches_its_handler_authenticated(
    authenticated_client: Any,
    service_calls: List[conftest.ServiceCall],
    route: ProtectedRoute,
) -> None:
    """An admitted request runs exactly the handler body the route names.

    Establishes that the recorder registers a handler that runs, so the empty recorder
    asserted for a rejected request records an absence rather than a recorder that never
    records.
    """
    request_route(authenticated_client, route)

    assert recorded_calls(service_calls) == [(route.service, route.service_method)]


def test_v1_openapi_paths_are_unchanged_by_the_auth_dependency() -> None:
    """The published paths and methods are the four paths and five operations of before."""
    paths = conftest.app.openapi()["paths"]

    assert {
        path: sorted(operations) for path, operations in paths.items()
    } == EXPECTED_OPENAPI_OPERATIONS


def test_v1_auth_dependency_is_absent_from_the_request_contract() -> None:
    """The dependency parameter added to each handler is not a published parameter.

    A FastAPI dependency parameter does not appear in the request contract, so no client
    sends a value for it and no Pydantic request or response schema changed.
    """
    operation = conftest.app.openapi()["paths"][WORKBOOKS_PATH]["get"]
    parameters = operation.get("parameters", [])

    assert (
        sorted(parameter["name"] for parameter in parameters)
        == EXPECTED_GET_WORKBOOKS_PARAMETERS
    )


@pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=ROUTE_IDS)
def test_v1_route_declares_the_bearer_security_requirement(
    route: ProtectedRoute,
) -> None:
    """Every published operation declares a bearer credential as a requirement."""
    operation = conftest.app.openapi()["paths"][route.published_path][
        route.method.lower()
    ]

    assert operation.get("security")


# ---------------------------------------------------------------------------
# Test cases for V2 - the security module is importable
# ---------------------------------------------------------------------------
#
# Threat: the module annotated `Optional[timedelta]` without importing `typing`, and
# Python 3.9 evaluates that annotation when the function is defined, so importing the
# module raised `NameError: name 'Optional' is not defined` and every authentication
# control in it was unreachable.


def test_v2_security_module_imports() -> None:
    """Importing the security module yields the module."""
    assert importlib.import_module("backend.app.core.security") is security


def test_v2_optional_annotation_resolves() -> None:
    """The annotation that raised NameError evaluates in the module's own namespace.

    ``typing.get_type_hints`` re-evaluates the annotation against the module globals,
    which is what raised while ``Optional`` was unbound.
    """
    hints = typing.get_type_hints(security.create_access_token)

    assert hints[EXPIRES_DELTA_PARAMETER] == Optional[timedelta]


def test_v2_security_module_executes_from_source() -> None:
    """Executing the module's file evaluates every annotation without raising.

    Exercises definition-time annotation evaluation against the file on disk rather than
    against the copy already imported.
    """
    module = execute_module_afresh(SECURITY_PROBE_MODULE_NAME, security)

    assert callable(module.create_access_token)
    assert callable(module.get_current_user)



# ---------------------------------------------------------------------------
# Test cases for V3 - the bearer token is verified server-side
# ---------------------------------------------------------------------------
#
# Threat: the caller's asserted identity was never checked. The client sent no token and
# the server decoded a token nothing issued, so identity could be claimed rather than
# proved.
#
# Every case here is a verification failure. A token that verifies reaches the identity
# lookup, which calls get_db() directly and would open a PostgreSQL connection.


def test_v3_forged_token_is_rejected(client: Any) -> None:
    """A bearer value that is not a valid ID token is answered 401 with a challenge."""
    response = client.get(
        WORKBOOKS_PATH, headers=conftest.bearer_headers(FORGED_TOKEN)
    )

    assert response.status_code == UNAUTHORIZED_STATUS
    assert response.headers.get(WWW_AUTHENTICATE_HEADER) == BEARER_CHALLENGE


def test_v3_forged_token_does_not_reach_the_handler(
    client: Any, service_calls: List[conftest.ServiceCall]
) -> None:
    """A forged token is rejected before any handler body runs."""
    client.get(WORKBOOKS_PATH, headers=conftest.bearer_headers(FORGED_TOKEN))

    assert recorded_calls(service_calls) == []


def test_v3_rejection_body_carries_no_failure_cause(client: Any) -> None:
    """The rejection body is fixed and names nothing about why verification failed.

    Guards against the pattern that returns ``str(exception)`` to the caller, which
    discloses internal detail. Two such sites remain at ``backend/app/api/cells.py`` L22
    and ``backend/app/api/collaboration.py`` L31; both are recorded in ``SECURITY.md`` and
    neither is asserted on here.
    """
    response = client.get(
        WORKBOOKS_PATH, headers=conftest.bearer_headers(FORGED_TOKEN)
    )

    assert response.json() == CREDENTIALS_REJECTION_BODY
    body = response.text.casefold()
    assert [
        marker for marker in LEAKED_CAUSE_MARKERS if marker.casefold() in body
    ] == []


@pytest.mark.parametrize(
    "fault", [fault.error for fault in VERIFICATION_FAULTS], ids=FAULT_IDS
)
def test_v3_verification_fault_is_rejected_not_served(
    client: Any, fault: BaseException
) -> None:
    """Each verification failure is answered 401 rather than escaping as a 500.

    The bare ValueError case covers the Admin SDK being unusable, which is raised as a
    plain ValueError rather than as a Firebase error class.
    """
    with mock.patch.object(
        security.firebase_auth, "verify_id_token", side_effect=fault
    ):
        response = client.get(
            WORKBOOKS_PATH, headers=conftest.bearer_headers(FORGED_TOKEN)
        )

    assert response.status_code == UNAUTHORIZED_STATUS
    assert response.json() == CREDENTIALS_REJECTION_BODY


@pytest.mark.parametrize(
    "fault", [fault.error for fault in VERIFICATION_FAULTS], ids=FAULT_IDS
)
def test_v3_verification_fault_detail_is_not_echoed(
    client: Any, fault: BaseException
) -> None:
    """The message a verification failure carries never reaches the caller."""
    with mock.patch.object(
        security.firebase_auth, "verify_id_token", side_effect=fault
    ):
        response = client.get(
            WORKBOOKS_PATH, headers=conftest.bearer_headers(FORGED_TOKEN)
        )

    assert VERIFICATION_FAILURE_DETAIL not in response.text


def test_v3_verification_receives_the_presented_token(
    client: Any, settings: Any
) -> None:
    """The value from the Authorization header is what verification is asked about.

    Establishes that the credential is taken from the request rather than assumed, and
    that revocation and account state are checked alongside the signature.
    """
    with mock.patch.object(
        security.firebase_auth,
        "verify_id_token",
        side_effect=security.firebase_auth.InvalidIdTokenError(
            VERIFICATION_FAILURE_DETAIL
        ),
    ) as verifier:
        client.get(WORKBOOKS_PATH, headers=conftest.bearer_headers(FORGED_TOKEN))

    assert verifier.call_args.args == (FORGED_TOKEN,)
    assert verifier.call_args.kwargs["check_revoked"] is True
    assert verifier.call_args.kwargs["app"].project_id == settings.firebase_project_id


# ---------------------------------------------------------------------------
# Test cases for V4 - uploaded objects are not world-readable
# ---------------------------------------------------------------------------
#
# Threat: every uploaded workbook was granted to allUsers and the returned URL was
# permanent, so anyone who guessed an object name downloaded it.


def test_v4_upload_sets_no_public_acl(upload: Upload) -> None:
    """No public ACL is granted on the uploaded object."""
    assert upload.blob.make_public.called is False


def test_v4_upload_returns_a_signed_url(upload: Upload) -> None:
    """The returned URL is the signed URL, and is not the object's public URL."""
    assert upload.blob.generate_signed_url.called is True
    assert upload.returned == SIGNED_URL_SENTINEL
    assert upload.returned is not upload.blob.public_url


def test_v4_signed_url_expiry_is_bounded(upload: Upload, settings: Any) -> None:
    """The signature is valid for a bounded interval taken from configuration."""
    expiration = upload.blob.generate_signed_url.call_args.kwargs["expiration"]

    assert expiration is not None
    assert isinstance(expiration, timedelta)
    assert expiration > timedelta(0)
    assert expiration == timedelta(minutes=settings.signed_url_expiry_minutes)


def test_v4_signed_url_is_bound_to_the_written_generation(upload: Upload) -> None:
    """The signature names the generation just written, not the object name alone."""
    keywords = upload.blob.generate_signed_url.call_args.kwargs

    assert keywords["version"] == "v4"
    assert keywords["method"] == "GET"
    assert keywords["generation"] == BLOB_GENERATION


def test_v4_upload_targets_the_configured_bucket(
    upload: Upload, settings: Any
) -> None:
    """The object is written to the bucket configuration names."""
    assert upload.client.bucket.call_args.args == (settings.gcs_bucket_name,)
    assert upload.bucket.blob.call_args.args == (UPLOAD_FILE_NAME,)
    assert upload.blob.upload_from_string.called is True


def test_v4_signed_upload_is_retained(upload: Upload) -> None:
    """An object whose URL was signed is left in place."""
    assert upload.blob.delete.called is False


# ---------------------------------------------------------------------------
# Test cases for V5 - the cross-origin policy is an explicit allow-list
# ---------------------------------------------------------------------------
#
# Threat: wildcard methods and headers with credentials enabled made the preflight echo
# any requesting origin and reflect any requested header, so a page on an
# attacker-controlled origin could read responses cross-origin.


def test_v5_allowed_origin_preflight_is_admitted(
    client: Any, allowed_origin: str
) -> None:
    """A preflight from the configured origin is answered with that exact origin."""
    response = preflight(client, allowed_origin)

    assert response.status_code == OK_STATUS
    assert response.headers[ALLOW_ORIGIN_HEADER] == allowed_origin
    assert response.headers[ALLOW_ORIGIN_HEADER] != CORS_WILDCARD
    assert response.headers[ALLOW_CREDENTIALS_HEADER] == "true"


def test_v5_disallowed_origin_preflight_is_rejected(client: Any) -> None:
    """A preflight from an origin outside the allow-list is refused with no grant."""
    response = preflight(client, DISALLOWED_ORIGIN)

    assert response.status_code == BAD_REQUEST_STATUS
    assert response.text == DISALLOWED_ORIGIN_BODY
    assert ALLOW_ORIGIN_HEADER not in response.headers


def test_v5_disallowed_origin_simple_request_carries_no_grant(client: Any) -> None:
    """A simple request from an origin outside the allow-list receives no grant."""
    response = client.get(WORKBOOKS_PATH, headers={ORIGIN_HEADER: DISALLOWED_ORIGIN})

    assert ALLOW_ORIGIN_HEADER not in response.headers


def test_v5_allowed_origin_simple_request_carries_that_origin(
    client: Any, allowed_origin: str
) -> None:
    """A simple request from the configured origin receives that origin, not a wildcard."""
    response = client.get(WORKBOOKS_PATH, headers={ORIGIN_HEADER: allowed_origin})

    assert response.headers[ALLOW_ORIGIN_HEADER] == allowed_origin
    assert response.headers[ALLOW_ORIGIN_HEADER] != CORS_WILDCARD


def test_v5_arbitrary_request_header_is_not_reflected(
    client: Any, allowed_origin: str
) -> None:
    """A header no route declares is refused and never appears in the grant.

    The wildcard the fix removed reflected whatever the preflight asked for.
    """
    response = preflight(
        client, allowed_origin, requested_headers=ARBITRARY_REQUEST_HEADER
    )

    assert response.status_code == BAD_REQUEST_STATUS
    granted = response.headers.get(ALLOW_HEADERS_HEADER, "")
    assert ARBITRARY_REQUEST_HEADER.casefold() not in granted.casefold()


def test_v5_declared_request_headers_are_admitted(
    client: Any, allowed_origin: str
) -> None:
    """The two headers the application sends are admitted, so the allow-list is workable."""
    response = preflight(
        client, allowed_origin, requested_headers=DECLARED_REQUEST_HEADERS
    )

    assert response.status_code == OK_STATUS


def test_v5_allowed_methods_are_an_explicit_list(
    client: Any, allowed_origin: str
) -> None:
    """The granted methods are the four the application uses, with no wildcard."""
    response = preflight(client, allowed_origin)
    granted = {
        method.strip() for method in response.headers[ALLOW_METHODS_HEADER].split(",")
    }

    assert CORS_WILDCARD not in granted
    assert granted == EXPECTED_ALLOWED_METHODS


# ---------------------------------------------------------------------------
# Test cases for V6 - the database connection requires TLS
# ---------------------------------------------------------------------------
#
# Threat: the engine was created with no transport setting, so the driver negotiated
# whatever the server permitted and credentials and row data crossed the network in
# cleartext.


def test_v6_engine_requires_sslmode() -> None:
    """The engine is created with a transport mode in its connect arguments."""
    call = create_engine_call()
    connect_args = call.kwargs[CONNECT_ARGS_KEYWORD]

    assert SSL_MODE_KEYWORD in connect_args
    assert connect_args[SSL_MODE_KEYWORD]


def test_v6_sslmode_comes_from_configuration(settings: Any) -> None:
    """The transport mode is the configured one and cannot negotiate plaintext."""
    call = create_engine_call()

    assert call.kwargs[CONNECT_ARGS_KEYWORD][SSL_MODE_KEYWORD] == settings.db_sslmode
    assert settings.db_sslmode in TLS_ONLY_SSL_MODES


def test_v6_engine_addresses_the_configured_database(settings: Any) -> None:
    """The transport mode is applied to the configured database URL."""
    call = create_engine_call()

    assert call.args == (settings.DATABASE_URL,)


def test_v6_probe_leaves_the_imported_module_untouched() -> None:
    """Reading the call arguments does not rebind the live engine, session or dependency.

    ``get_db`` is registered as an ``app.dependency_overrides`` key by identity, so a
    rebound ``get_db`` would leave every later test opening real connections.
    """
    original_get_db = database.get_db
    original_engine = database.engine

    create_engine_call()

    assert database.get_db is original_get_db
    assert database.engine is original_engine
    assert conftest.get_db is original_get_db



# ---------------------------------------------------------------------------
# Test cases for V7 - security response headers on every response
# ---------------------------------------------------------------------------
#
# Threat: no response carried any security header, so the browser restricted no script
# source, permitted framing, sniffed MIME types and leaked full referrers.
#
# The 401, preflight and 429 cases are what establish that the header middleware is
# outermost. Registered any deeper, error and preflight responses would carry no header:
# the CORS middleware answers a preflight without calling downstream, and a rejection is
# produced below whichever middleware raised or returned it.


def test_v7_security_headers_on_a_successful_response(
    authenticated_client: Any,
) -> None:
    """A served response carries every required header."""
    response = authenticated_client.get(WORKBOOKS_PATH)

    assert response.status_code == OK_STATUS
    assert missing_security_headers(response) == []


def test_v7_security_headers_on_an_unauthorized_response(client: Any) -> None:
    """A 401 carries every required header."""
    response = client.get(WORKBOOKS_PATH)

    assert response.status_code == UNAUTHORIZED_STATUS
    assert missing_security_headers(response) == []


def test_v7_security_headers_on_a_cors_preflight_response(
    client: Any, allowed_origin: str
) -> None:
    """A preflight answered by the CORS middleware carries every required header."""
    response = preflight(client, allowed_origin)

    assert response.status_code == OK_STATUS
    assert missing_security_headers(response) == []


def test_v7_security_headers_on_a_rejected_cors_response(client: Any) -> None:
    """A refused cross-origin preflight carries every required header."""
    response = preflight(client, DISALLOWED_ORIGIN)

    assert response.status_code == BAD_REQUEST_STATUS
    assert missing_security_headers(response) == []


def test_v7_security_headers_on_a_too_many_requests_response() -> None:
    """A throttling rejection carries every required header.

    Driven through the throwaway application described by :func:`throttling_probe`, whose
    registration order mirrors ``backend/app/main.py`` L45-L48. The real application's
    windows are wide enough that no other test is throttled.
    """
    with throttling_probe(GENEROUS_WINDOW, WRITE_PROBE_WINDOW) as probe:
        responses = send_repeatedly(
            probe, "POST", WRITE_PROBE_LIMIT + REQUESTS_PAST_LIMIT
        )

    rejected = responses[-1]
    assert rejected.status_code == TOO_MANY_REQUESTS_STATUS
    assert missing_security_headers(rejected) == []


def test_v7_header_middleware_is_outermost_on_the_application() -> None:
    """The header middleware is the first registered, which makes it the outermost."""
    assert conftest.app.user_middleware[0].cls is SecurityHeadersMiddleware


def test_v7_header_values_are_the_canonical_set(authenticated_client: Any) -> None:
    """Every header carries the value the middleware defines, not a weaker one."""
    response = authenticated_client.get(WORKBOOKS_PATH)

    assert response.headers[CONTENT_SECURITY_POLICY_HEADER] == CONTENT_SECURITY_POLICY
    assert [
        name
        for name, value in STATIC_SECURITY_HEADERS.items()
        if response.headers.get(name) != value
    ] == []


def test_v7_content_security_policy_is_enforced_not_reported(
    authenticated_client: Any, settings: Any
) -> None:
    """The policy arrives under the enforcing header name.

    ``csp_report_only`` selects the header name; false is the enforcing name.
    """
    response = authenticated_client.get(WORKBOOKS_PATH)

    assert settings.csp_report_only is False
    assert CONTENT_SECURITY_POLICY_HEADER in response.headers
    assert CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER not in response.headers


def test_v7_content_security_policy_restricts_framing_and_sources(
    authenticated_client: Any,
) -> None:
    """The policy denies framing and constrains default and script sources."""
    policy = authenticated_client.get(WORKBOOKS_PATH).headers[
        CONTENT_SECURITY_POLICY_HEADER
    ]
    directives = {directive.strip() for directive in policy.split(";")}

    assert "frame-ancestors 'none'" in directives
    assert "default-src 'self'" in directives
    assert "object-src 'none'" in directives


# ---------------------------------------------------------------------------
# Test cases for V9 - request volume is bounded per client
# ---------------------------------------------------------------------------
#
# Threat: no route was throttled, so brute force, credential stuffing and scraping ran at
# whatever rate the caller could sustain.
#
# Each tier is exercised on a throwaway application with a narrowed window, so the real
# application's budget stays wide and no other test in the run is throttled. Both tiers
# key their counters on the client address and TestClient presents one constant host, so
# every request in a run would otherwise spend the same budget.


def test_v9_both_tiers_are_registered_on_the_application() -> None:
    """The application carries the ceiling tier and the write tier."""
    registered = {
        middleware.cls.__name__ for middleware in conftest.app.user_middleware
    }

    assert CEILING_TIER_MIDDLEWARE_NAME in registered
    assert WRITE_TIER_MIDDLEWARE_NAME in registered


def test_v9_write_tier_rejects_beyond_its_budget() -> None:
    """Writes past the write budget are refused with a retry interval."""
    with throttling_probe(GENEROUS_WINDOW, WRITE_PROBE_WINDOW) as probe:
        responses = send_repeatedly(
            probe, "POST", WRITE_PROBE_LIMIT + REQUESTS_PAST_LIMIT
        )

    statuses = [response.status_code for response in responses]
    assert statuses == [OK_STATUS] * WRITE_PROBE_LIMIT + [
        TOO_MANY_REQUESTS_STATUS
    ] * REQUESTS_PAST_LIMIT

    rejected = responses[-1]
    assert rejected.json() == {
        "error": f"Rate limit exceeded: {WRITE_PROBE_WINDOW_DESCRIPTION}"
    }
    assert int(rejected.headers[RETRY_AFTER_HEADER]) >= 1


def test_v9_write_tier_leaves_reads_unaffected() -> None:
    """Reads are not counted against the write budget."""
    with throttling_probe(GENEROUS_WINDOW, WRITE_PROBE_WINDOW) as probe:
        attempts = WRITE_PROBE_LIMIT + REQUESTS_PAST_LIMIT + 1
        responses = send_repeatedly(probe, "GET", attempts)

    assert [response.status_code for response in responses] == [OK_STATUS] * attempts


def test_v9_ceiling_tier_rejects_beyond_its_budget() -> None:
    """Requests past the application-wide ceiling are refused.

    The handlers on the throwaway application are undecorated and declare no ``request``
    parameter, so this establishes that the control needs no change to a route handler.
    """
    with throttling_probe(CEILING_PROBE_WINDOW, GENEROUS_WINDOW) as probe:
        responses = send_repeatedly(
            probe, "GET", CEILING_PROBE_LIMIT + REQUESTS_PAST_LIMIT
        )

    statuses = [response.status_code for response in responses]
    assert statuses == [OK_STATUS] * CEILING_PROBE_LIMIT + [
        TOO_MANY_REQUESTS_STATUS
    ] * REQUESTS_PAST_LIMIT
    assert responses[-1].json() == {
        "error": f"Rate limit exceeded: {CEILING_PROBE_WINDOW_DESCRIPTION}"
    }


def test_v9_probe_budget_is_restored_on_exit() -> None:
    """The counters a probe spends are cleared, so no later test starts throttled."""
    with throttling_probe(GENEROUS_WINDOW, WRITE_PROBE_WINDOW) as probe:
        send_repeatedly(probe, "POST", WRITE_PROBE_LIMIT + REQUESTS_PAST_LIMIT)
        conftest.reset_rate_limit_state(probe.app)
        restored = probe.post(PROBE_PATH)

    assert restored.status_code == OK_STATUS


def test_v9_application_budget_is_not_spent_by_a_probe(client: Any) -> None:
    """A probe's counters are its own, so the application still serves after one ran."""
    with throttling_probe(CEILING_PROBE_WINDOW, WRITE_PROBE_WINDOW) as probe:
        send_repeatedly(probe, "POST", CEILING_PROBE_LIMIT + REQUESTS_PAST_LIMIT)

    response = client.get(WORKBOOKS_PATH)

    assert response.status_code == UNAUTHORIZED_STATUS


# ---------------------------------------------------------------------------
# Test cases for V12 - token lifetime is governed by configuration
# ---------------------------------------------------------------------------
#
# Threat: the lifetime was hardcoded to fifteen minutes and
# ACCESS_TOKEN_EXPIRE_MINUTES was never read, so an operator could not shorten it.


def test_v12_default_lifetime_comes_from_configuration(settings: Any) -> None:
    """With no argument the lifetime is the configured one."""
    configured = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    lifetime = token_lifetime(settings.SECRET_KEY, settings.ALGORITHM, None)

    # The configured lifetime differs from the value that was hardcoded, so this
    # distinguishes a lifetime read from configuration from one that was not.
    assert configured != PREVIOUSLY_HARDCODED_LIFETIME
    assert abs(lifetime - configured) <= LIFETIME_TOLERANCE


def test_v12_explicit_expires_delta_wins(settings: Any) -> None:
    """A supplied duration is honoured over the configured one."""
    configured = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    lifetime = token_lifetime(
        settings.SECRET_KEY, settings.ALGORITHM, EXPLICIT_LIFETIME
    )

    assert EXPLICIT_LIFETIME != configured
    assert abs(lifetime - EXPLICIT_LIFETIME) <= LIFETIME_TOLERANCE


def test_v12_create_access_token_signature_is_unchanged() -> None:
    """The parameters and the default are the ones callers already depend on."""
    signature = inspect.signature(security.create_access_token)

    assert list(signature.parameters) == EXPECTED_TOKEN_PARAMETERS
    assert signature.parameters[EXPIRES_DELTA_PARAMETER].default is None

