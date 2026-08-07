"""Authentication primitives for the FastAPI application.

Provides the password hashing helpers, the access-token minting helper, and
``get_current_user`` — the request dependency that resolves the caller's bearer token to
a :class:`~backend.app.db.models.User`.

``get_current_user`` verifies the token server-side. Two settings, both read on every
request, govern it: ``auth_token_verifier`` selects the verification path (``firebase``
validates a Firebase ID token through the Admin SDK, rejecting one that has been revoked
or belongs to a disabled account, and resolves the user by the token's ``email`` claim;
``legacy_jwt`` validates the locally-issued HS256 token and resolves by ``sub``), and
``auth_enforcement_enabled`` set false serves requests on unverified token claims, logging
a warning and marking each such response once the caller has been admitted.

The Firebase Admin SDK is initialised on first use inside the request path, never at
import, with one app per configured project, and the blocking verification and lookup run
in a worker thread rather than on the event loop. That lookup opens and closes its own
Session, so every request returns its database connection and the resolved user crosses
back detached.
:class:`AuthEnforcementBypassMarkerMiddleware` emits the bypass marker header and must be
registered as the innermost middleware for that header to reach clients.
"""

import logging
import threading
from contextvars import ContextVar
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
# SECURITY: restores importability of this module — Optional was annotated but
# never imported
from typing import Any, Dict, Optional, Tuple
import firebase_admin
from firebase_admin import auth as firebase_auth, credentials
from firebase_admin import exceptions as firebase_exceptions
from google.auth import exceptions as google_auth_exceptions
from sqlalchemy.orm.attributes import InstrumentedAttribute
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from backend.app.core.config import Settings, get_settings
from backend.app.db.models import User
from backend.app.db.database import get_db

logger = logging.getLogger(__name__)

# The verifier value that selects the locally-issued HS256 token path. Every other
# accepted value selects Firebase ID token verification.
LEGACY_JWT_VERIFIER = "legacy_jwt"

FIREBASE_IDENTITY_CLAIM = "email"
LEGACY_JWT_IDENTITY_CLAIM = "sub"

# Name prefix of the Firebase Admin app used for token verification. The configured
# project is appended, so one app exists per project rather than one per process.
FIREBASE_APP_NAME_PREFIX = "excel-clone-auth"

# App-name suffix used when no project is configured and the SDK resolves one itself.
FIREBASE_AMBIENT_PROJECT_APP_SUFFIX = "ambient"

# Marks a response served while authentication enforcement was disabled.
AUTH_ENFORCEMENT_BYPASS_HEADER = "X-Auth-Enforcement-Bypassed"
AUTH_ENFORCEMENT_BYPASS_HEADER_VALUE = "true"

# Read while the response headers are being sent. ``get_current_user`` sets it from the
# event loop, which is the context a pure ASGI middleware sends from, so the flag is
# visible there. The default is false, and AuthEnforcementBypassMarkerMiddleware resets it
# as each request begins.
_auth_enforcement_bypassed: ContextVar[bool] = ContextVar(
    "auth_enforcement_bypassed", default=False
)

# Method and path of the request being served, set by the middleware for the bypass audit
# record. Empty when no middleware has run.
_request_description: ContextVar[str] = ContextVar("request_description", default="")

# Key of the single-entry mapping the bypass travels through on its way out of
# :func:`_resolve_current_user`. A worker thread runs on a copy of the caller's context,
# so a ContextVar set inside one is invisible to the caller and cannot carry it.
_BYPASS_STATE_KEY = "bypassed"

# Serialises first use of the Admin SDK: initialising the same app twice raises.
_firebase_app_lock = threading.Lock()

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    settings = get_settings()
    to_encode = data.copy()
    # A supplied duration wins, including an explicit zero.
    if expires_delta is not None:
        expire = datetime.utcnow() + expires_delta
    else:
        # SECURITY: token lifetime governed by ACCESS_TOKEN_EXPIRE_MINUTES — it was
        # hardcoded to 15 minutes and the setting was never read
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def _firebase_app_name(project_id: str) -> str:
    """Return the Admin SDK app name that verifies tokens for ``project_id``.

    The name carries the project, so each configured project gets its own app.

    Args:
        project_id: The project whose ID tokens are accepted. Empty is not a supported
            input; :func:`_firebase_app` rejects it before calling this.

    Returns:
        str: The app name.
    """
    suffix = project_id or FIREBASE_AMBIENT_PROJECT_APP_SUFFIX
    return f"{FIREBASE_APP_NAME_PREFIX}:{suffix}"


def _firebase_app(project_id: str) -> firebase_admin.App:
    """Return the Firebase app for ``project_id``, initialising it on first use.

    Apps are keyed by project, so a changed ``firebase_project_id`` takes effect on the
    next request. Initialisation happens inside the request path, never at module import.
    Concurrent first calls initialise the app exactly once, and every later call is a
    lock-free lookup.

    Args:
        project_id: Firebase project the token issuer must match. The caller passes
            ``firebase_project_id`` when it is set and the required ``PROJECT_ID``
            otherwise, so the accepted issuer is always one named project.

    Returns:
        firebase_admin.App: The initialised app for this project.

    Raises:
        ValueError: If no project is configured, or the app cannot be initialised.
    """
    if not project_id:
        # SECURITY: no project, no verification - an unconstrained issuer would accept a
        # token minted by any Firebase project. The caller maps this to a 401.
        raise ValueError(
            "No Firebase project is configured: set PROJECT_ID, or "
            "firebase_project_id, so the accepted token issuer is pinned to one project"
        )
    name = _firebase_app_name(project_id)
    # get_app raises ValueError while the named app does not exist; initialize_app raises
    # it once the app does. Hence the lookup, then the same lookup again under the lock.
    try:
        return firebase_admin.get_app(name)
    except ValueError:
        pass
    options = {"projectId": project_id}
    with _firebase_app_lock:
        try:
            return firebase_admin.get_app(name)
        except ValueError:
            # SECURITY: use Application Default Credentials; no credential path or key is
            # embedded in code
            return firebase_admin.initialize_app(
                credentials.ApplicationDefault(), options, name=name
            )


def _verified_claims(
    token: str, verifier: str, settings: Settings, credentials_exception: HTTPException
) -> Dict[str, Any]:
    """Return the claims of ``token`` once its signature and validity are established.

    Blocking: Firebase ID token verification fetches Google's signing certificates on a
    cold cache. This function is synchronous throughout and is called from a worker
    thread, never on the event loop.

    Args:
        token: The bearer token value taken from the ``Authorization`` header.
        verifier: The configured ``auth_token_verifier`` value.
        settings: The settings instance for this request.
        credentials_exception: The 401 raised for the verification failures caught here.

    Returns:
        Dict[str, Any]: The verified claims.

    Raises:
        HTTPException: ``credentials_exception``, for a local JWT that fails to decode, and
            for an invalid, expired or revoked Firebase ID token, a disabled account, or a
            failure to fetch Google's signing certificates, with the cause recorded in the
            server log and absent from the response. Also for a credential that cannot be
            resolved and a provider that cannot be reached, which are recorded at error
            level because the fault is the deployment's rather than the caller's.
    """
    if verifier == LEGACY_JWT_VERIFIER:
        try:
            return jwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
        except JWTError as exc:
            # SECURITY: log only the exception type for rejected credentials
            logger.warning(
                "Rejected a bearer token: local JWT verification failed (%s)",
                type(exc).__name__,
            )
            raise credentials_exception from None
    try:
        # SECURITY: revocation and account state are checked — a signed-out, password-reset
        # or disabled user's already-issued token stayed valid for its full lifetime
        return firebase_auth.verify_id_token(
            token,
            app=_firebase_app(
                settings.firebase_project_id or settings.PROJECT_ID
            ),
            check_revoked=True,
        )
    except (
        firebase_auth.InvalidIdTokenError,
        firebase_auth.ExpiredIdTokenError,
        firebase_auth.RevokedIdTokenError,
        firebase_auth.UserDisabledError,
        # SECURITY: a signing-certificate fetch failure is rejected as a verification
        # failure - it previously escaped this clause as an unhandled 500
        firebase_auth.CertificateFetchError,
        # Raised for an empty or non-string token, and when the Admin SDK could not be
        # used.
        ValueError,
    ) as exc:
        logger.warning(
            "Rejected a bearer token: Firebase ID token verification failed (%s)",
            type(exc).__name__,
        )
        raise credentials_exception from None
    except (
        firebase_exceptions.FirebaseError,
        google_auth_exceptions.GoogleAuthError,
    ) as exc:
        # SECURITY: a credential or provider failure is rejected rather than served - an
        # unresolvable credential and an unreachable provider each escaped as an
        # unhandled 500 that left no trace in the log at all
        logger.error(
            "Rejected a bearer token: Firebase ID token verification could not be "
            "completed (%s)",
            type(exc).__name__,
        )
        raise credentials_exception from None


def _unverified_claims(
    token: str, credentials_exception: HTTPException
) -> Dict[str, Any]:
    """Return the claims of ``token`` without checking its signature.

    Reached only while ``auth_enforcement_enabled`` is false. Reading claims is not an
    admission: a request that gets this far is still rejected when the claims carry no
    usable identity or that identity matches no local user. The bypass is recorded and the
    response marked only once a caller has actually been admitted, by
    :func:`_resolve_current_user` and :func:`get_current_user`.

    Args:
        token: The bearer token value taken from the ``Authorization`` header.
        credentials_exception: The 401 raised when no claims can be read at all.

    Returns:
        Dict[str, Any]: The token's unverified claims.

    Raises:
        HTTPException: ``credentials_exception``, if the token carries no readable claims.
    """
    try:
        return jwt.get_unverified_claims(token)
    except JWTError as exc:
        logger.warning(
            "Rejected a bearer token: no readable claims (%s)", type(exc).__name__
        )
        raise credentials_exception from None


def _resolve_identity(
    claims: Dict[str, Any], verifier: str, credentials_exception: HTTPException
) -> Tuple[InstrumentedAttribute, Any]:
    """Return the column and value that select the caller's :class:`User` row.

    The Firebase path resolves the ``email`` claim against ``User.email`` and admits it only
    when it is a non-empty string carrying no NUL byte, so a null, numeric, otherwise
    non-string, or NUL-bearing value never reaches the query.

    The legacy path resolves ``sub`` against the integer ``User.id`` and admits a truthy
    claim only once it converts to an integer, so a value the column cannot hold never
    reaches the query either. Both rejections are recorded in the server log.

    Args:
        claims: The token claims produced by the active verification path.
        verifier: The configured ``auth_token_verifier`` value.
        credentials_exception: The 401 raised for every rejected identity.

    Returns:
        Tuple[InstrumentedAttribute, Any]: The column to filter on and the value to match.

    Raises:
        HTTPException: ``credentials_exception``, with the reason recorded in the server
            log and absent from the response.
    """
    if verifier == LEGACY_JWT_VERIFIER:
        identity = claims.get(LEGACY_JWT_IDENTITY_CLAIM)
        if not identity:
            # SECURITY: a token carrying no identity claim is rejected — it would
            # otherwise match a row on a null comparison
            logger.warning(
                "Rejected a bearer token: no %s claim for the %s verifier",
                LEGACY_JWT_IDENTITY_CLAIM,
                verifier,
            )
            raise credentials_exception
        try:
            # SECURITY: the claim is resolved to the integer the column holds before it is
            # queried — a claim that is not one reached the database driver as an
            # unhandled 500 that left no trace in the log at all
            if isinstance(identity, bool):
                raise ValueError("a boolean is not a user identifier")
            identity = int(identity)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Rejected a bearer token: its %s claim is not a user identifier for the "
                "%s verifier (%s)",
                LEGACY_JWT_IDENTITY_CLAIM,
                verifier,
                type(exc).__name__,
            )
            raise credentials_exception from None
        return User.id, identity

    identity = claims.get(FIREBASE_IDENTITY_CLAIM)
    # SECURITY: a token carrying no usable identity claim is rejected — a null or
    # non-string value would otherwise reach the query and could match a row, and a value
    # carrying a NUL byte reached the database driver as an unhandled 500 that left no
    # trace in the log at all
    if not isinstance(identity, str) or not identity or "\x00" in identity:
        logger.warning(
            "Rejected a bearer token: no usable %s claim for the %s verifier",
            FIREBASE_IDENTITY_CLAIM,
            verifier,
        )
        raise credentials_exception
    return User.email, identity


def auth_enforcement_bypassed() -> bool:
    """Whether the request being served bypassed token verification."""
    return _auth_enforcement_bypassed.get()


class AuthEnforcementBypassMarkerMiddleware:
    """Marks responses served while authentication enforcement was disabled.

    Adds :data:`AUTH_ENFORCEMENT_BYPASS_HEADER` to any response whose request was admitted
    by :func:`get_current_user` with ``auth_enforcement_enabled`` false. The header reaches
    clients only while this middleware is registered on the application; the warning
    ``get_current_user`` logs for the same request does not depend on it.

    Also records the request's method and path, which that warning includes to identify
    the admitted request. Neither value carries a credential or a token claim.

    Registration contract: this must be the innermost middleware, added to the
    application before every other one, so that nothing sits between it and the router.
    The bypass reaches it through a ``ContextVar`` set while the route is being served,
    and Starlette's ``BaseHTTPMiddleware`` runs everything downstream of itself in a
    separate task whose context copy does not propagate back. Registered outside any
    ``BaseHTTPMiddleware`` - which both :class:`SecurityHeadersMiddleware` and the
    write-tier throttling middleware are - the request is still admitted and the warning
    is still logged, but this header is silently absent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _auth_enforcement_bypassed.set(False)
        _request_description.set(
            "{0} {1}".format(scope.get("method", ""), scope.get("path", "")).strip()
        )

        async def send_with_marker(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and _auth_enforcement_bypassed.get()
            ):
                MutableHeaders(raw=message["headers"])[
                    AUTH_ENFORCEMENT_BYPASS_HEADER
                ] = AUTH_ENFORCEMENT_BYPASS_HEADER_VALUE
            await send(message)

        await self.app(scope, receive, send_with_marker)


def _resolve_current_user(token: str, bypass_state: Dict[str, bool]) -> User:
    """Resolve ``token`` to the :class:`~backend.app.db.models.User` it identifies.

    Blocking throughout: reading the settings issues Secret Manager calls, verification
    reaches the Firebase Admin SDK or the local signing key, and the identity lookup
    queries the database. :func:`get_current_user` runs it on a worker thread.

    Owns the Session the lookup runs in and releases it before returning - on the
    admitted path, on the 401 path and on an unexpected database error alike - so no
    request leaves a Session holding a pooled connection. The caller therefore receives a
    detached instance: the column values the lookup loaded are readable, and an attribute
    that would need a further query is not.

    Args:
        token: The bearer token value taken from the ``Authorization`` header.
        bypass_state: Single-entry mapping this sets under :data:`_BYPASS_STATE_KEY` once a
            caller has been admitted while ``auth_enforcement_enabled`` is false, so the
            caller can record the bypass. A rejected request is never recorded as one.

    Returns:
        User: The caller, resolved by the identity claim the configured verifier names and
            detached from the Session that resolved it.

    Raises:
        HTTPException: 401 with a ``WWW-Authenticate: Bearer`` challenge if the token fails
            verification, carries no usable identity claim, or names no known user. No cause
            reaches the response; every rejection is recorded in the server log.
    """
    settings = get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    verifier = settings.auth_token_verifier
    if settings.auth_enforcement_enabled:
        # SECURITY: the bearer token is verified server-side - the caller's asserted
        # identity was never checked
        claims = _verified_claims(token, verifier, settings, credentials_exception)
    else:
        claims = _unverified_claims(token, credentials_exception)
    identity_column, identity = _resolve_identity(
        claims, verifier, credentials_exception
    )
    # SECURITY: the identity lookup owns its Session and releases it on every path - the
    # Session it opened was never closed, so each request left a pooled connection held
    db_context = get_db()
    try:
        db = next(db_context)
        user = db.query(User).filter(identity_column == identity).first()
        if user is None:
            # SECURITY: the cause is recorded server-side only, and names neither the
            # identity nor the token - the rejection previously left no trace at all
            logger.warning(
                "Rejected a bearer token: its identity matches no local user for the %s "
                "verifier",
                verifier,
            )
            raise credentials_exception
        # Detach the row while the values the lookup loaded are still on it, so what
        # crosses back to the event loop reads its own attributes instead of reaching a
        # Session that belongs to this worker thread and is closed by the time it lands.
        db.expunge(user)
    finally:
        # Closing the generator runs the ``finally`` in get_db(), which closes the Session
        # and returns its connection to the pool. Reached whether the lookup admitted the
        # caller, raised the 401, or failed inside the ORM.
        db_context.close()
    if not settings.auth_enforcement_enabled:
        # SECURITY: only an admitted request is reported as a bypass - a rejected request
        # was previously reported as one too
        bypass_state[_BYPASS_STATE_KEY] = True
    return user


async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    """Resolve the caller's bearer token to a :class:`~backend.app.db.models.User`.

    The request dependency every protected route depends on. The work is blocking and runs
    in :func:`_resolve_current_user` on a Starlette worker thread, so a slow Secret Manager
    read, token verification or database query cannot stall the event loop.

    Args:
        token: The bearer token value, extracted from the ``Authorization`` header by
            :data:`oauth2_scheme`.

    Returns:
        User: The caller the token names - verified while ``auth_enforcement_enabled`` is
            true, and read from unverified claims while it is false. Detached from the
            Session that resolved it, which is closed before this returns, so the column
            values loaded during the lookup are readable and nothing lazy-loads.

    Raises:
        HTTPException: 401 with a ``WWW-Authenticate: Bearer`` challenge for any token that
            cannot be resolved to a known user.
    """
    bypass_state: Dict[str, bool] = {_BYPASS_STATE_KEY: False}
    user = await run_in_threadpool(_resolve_current_user, token, bypass_state)
    if bypass_state[_BYPASS_STATE_KEY]:
        # SECURITY: an admitted request that skipped token verification is recorded and its
        # response marked
        _auth_enforcement_bypassed.set(True)
        logger.warning(
            "Authentication enforcement is disabled: admitted %s on unverified token "
            "claims. Set auth_enforcement_enabled true to restore verification.",
            _request_description.get() or "a request",
        )
    return user
