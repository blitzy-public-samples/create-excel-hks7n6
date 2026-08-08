"""Authentication primitives for the FastAPI application.

Provides the password hashing helpers, the access-token minting helper, and
``get_current_user`` — the request dependency that resolves the caller's bearer token to
a :class:`~backend.app.db.models.User`.

``get_current_user`` verifies the token server-side. Two settings, both read on every
request, govern it: ``auth_token_verifier`` selects the verification path (``firebase``
validates a Firebase ID token through the Admin SDK, rejecting one that has been revoked
or belongs to a disabled account, and resolves the user by the token's ``email`` claim;
``legacy_jwt`` validates the locally-issued HS256 token and resolves by ``sub``), and
``auth_enforcement_enabled`` set false serves requests on unverified token claims, logging a
warning and marking each such response once the caller has been admitted.

Every rejection is the same response: 401 with a ``WWW-Authenticate: Bearer`` challenge and
no cause. A request carrying no ``Authorization: Bearer`` header is refused by
:data:`oauth2_scheme` before this module's code runs, whatever ``auth_enforcement_enabled``
is set to, so the switch never admits an uncredentialed caller. What separates a caller
fault from a deployment or provider fault - an unresolvable signing credential, an Admin SDK
that will not initialise, unreachable signing certificates, or a provider that does not
answer - is the server log: the former is recorded at warning level with the exception type
alone, the latter at error level with its server-side exception context.

The Firebase Admin SDK is initialised on first use inside the request path, never at
import, with one app per configured project, and the blocking verification and lookup run
in a worker thread rather than on the event loop. That lookup opens and closes its own
Session, so every request returns its database connection and the resolved user crosses
back detached.
:class:`AuthEnforcementBypassMarkerMiddleware` emits the bypass marker header. It must sit
inside every ``BaseHTTPMiddleware`` on the application for that header to reach clients;
only the pure-ASGI
:class:`~backend.app.core.security_headers.ServerErrorBoundaryMiddleware` may be registered
inside it.
"""

import logging
import threading
from contextvars import ContextVar
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
# SECURITY: Optional is annotated below and must be imported for this module to import at
# all; every authentication control lives here.
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
from backend.app.core.security_headers import describe_request
from backend.app.db.models import User
from backend.app.db.database import get_db

logger = logging.getLogger(__name__)

# The verifier value that selects the locally-issued HS256 token path. Every other
# accepted value selects Firebase ID token verification.
LEGACY_JWT_VERIFIER = "legacy_jwt"

FIREBASE_IDENTITY_CLAIM = "email"

# Claim proving the token's holder controls the address in FIREBASE_IDENTITY_CLAIM. Firebase
# exposes it as a distinct state: an account may hold an address it has never confirmed.
FIREBASE_EMAIL_VERIFIED_CLAIM = "email_verified"

LEGACY_JWT_IDENTITY_CLAIM = "sub"

# Name prefix of the Firebase Admin app used for token verification. The configured
# project is appended, so one app exists per project rather than one per process.
FIREBASE_APP_NAME_PREFIX = "excel-clone-auth"

FIREBASE_AMBIENT_PROJECT_APP_SUFFIX = "ambient"

AUTH_ENFORCEMENT_BYPASS_HEADER = "X-Auth-Enforcement-Bypassed"
AUTH_ENFORCEMENT_BYPASS_HEADER_VALUE = "true"

# Read while the response headers are being sent. ``get_current_user`` sets it from the
# event loop, which is the context a pure ASGI middleware sends from, so the flag is
# visible there. The default is false, and AuthEnforcementBypassMarkerMiddleware resets it
# as each request begins.
_auth_enforcement_bypassed: ContextVar[bool] = ContextVar(
    "auth_enforcement_bypassed", default=False
)

# Method and path of the request being served, escaped by
# security_headers.describe_request and set by the middleware for the bypass audit record.
# Empty when no middleware has run.
_request_description: ContextVar[str] = ContextVar("request_description", default="")

# Key of the single-entry mapping the bypass travels through on its way out of
# :func:`_resolve_current_user`. A worker thread runs on a copy of the caller's context,
# so a ContextVar set inside one is invisible to the caller and cannot carry it.
_BYPASS_STATE_KEY = "bypassed"

# Serialises first use of the Admin SDK: initialising the same app twice raises.
_firebase_app_lock = threading.Lock()

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')
# SECURITY: a request carrying no Authorization: Bearer header is refused here, with the 401
# challenge, before any route code runs - no route required a credential at all.
# CONTRACT: extraction refuses it whatever auth_enforcement_enabled is set to, so the
# break-glass switch relaxes verification of a presented token and never admits a request
# that presents none.
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
        # SECURITY: token lifetime is governed by ACCESS_TOKEN_EXPIRE_MINUTES, which
        # Settings bounds to 1..1440.
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
    lock-free lookup. :func:`_verified_claims` catches every exception listed below and
    answers the frozen 401, recording the cause at error level so a provider fault is
    distinguishable in the log from a refused credential.

    Args:
        project_id: Firebase project the token issuer must match. The caller passes
            ``firebase_project_id`` when it is set and the required ``PROJECT_ID``
            otherwise, so the accepted issuer is always one named project.

    Returns:
        firebase_admin.App: The initialised app for this project.

    Raises:
        ValueError: If no project is configured, or the Admin SDK refuses to initialise the
            app.
        firebase_admin.exceptions.FirebaseError: If the Admin SDK reports a provider error
            while initialising.
        google.auth.exceptions.GoogleAuthError: If Application Default Credentials cannot
            be resolved.
    """
    if not project_id:
        # SECURITY: verification is pinned to one configured Firebase project; an
        # unconstrained issuer accepts a token minted by any Firebase project. The caller
        # answers 401 and records this at error level.
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
    token: str,
    verifier: str,
    settings: Settings,
    credentials_exception: HTTPException,
) -> Dict[str, Any]:
    """Return the claims of ``token`` once its signature and validity are established.

    Blocking: Firebase ID token verification fetches Google's signing certificates on a
    cold cache. This function is synchronous throughout and is called from a worker
    thread, never on the event loop.

    Every failure raises ``credentials_exception``, carrying no cause, no token and no claim
    value. Two kinds of failure are separated in the server log rather than in the response.
    A caller-credential failure - the token was checked and refused - is recorded at warning
    level with the exception type alone. A deployment or provider failure - the signing
    credential is unresolvable, the Admin SDK could not be initialised, Google's signing
    certificates could not be fetched, or the provider did not answer - is recorded at error
    level with the server-side exception context, which is what makes an outage alertable.

    Args:
        token: The bearer token value taken from the ``Authorization`` header.
        verifier: The configured ``auth_token_verifier`` value.
        settings: The settings instance for this request.
        credentials_exception: The 401 raised for every rejected credential.

    Returns:
        Dict[str, Any]: The verified claims.

    Raises:
        HTTPException: ``credentials_exception``, for a local JWT that fails to decode, for
            an invalid, expired or revoked Firebase ID token, for a disabled account, for a
            malformed token value, for a credential that cannot be resolved, for an Admin SDK
            that cannot be initialised, for a signing-certificate fetch failure and for a
            provider that cannot be reached.
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

    # SECURITY: a verifier that cannot be initialised refuses the credential and is recorded
    # at error level with its exception context - an unconfigured project and unresolvable
    # Application Default Credentials propagated as an un-logged 500
    try:
        verifying_app = _firebase_app(
            settings.firebase_project_id or settings.PROJECT_ID
        )
    except (
        ValueError,
        firebase_exceptions.FirebaseError,
        google_auth_exceptions.GoogleAuthError,
    ):
        logger.error(
            "Could not initialise Firebase ID token verification; answering %s",
            credentials_exception.status_code,
            exc_info=True,
        )
        raise credentials_exception from None

    try:
        # SECURITY: revocation and account state are checked, so an already-issued token is
        # refused once its user signs out, resets a password or is disabled.
        return firebase_auth.verify_id_token(
            token,
            app=verifying_app,
            check_revoked=True,
        )
    except (
        firebase_auth.InvalidIdTokenError,
        firebase_auth.ExpiredIdTokenError,
        firebase_auth.RevokedIdTokenError,
        firebase_auth.UserDisabledError,
        # Raised for an empty or non-string token value.
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
    ):
        # SECURITY: a provider failure refuses the credential and is recorded at error level
        # with its server-side exception context - it propagated as an un-logged 500.
        # CertificateFetchError reaches this clause through FirebaseError: Google's signing
        # keys were unreachable.
        logger.error(
            "Firebase ID token verification could not be completed; answering %s",
            credentials_exception.status_code,
            exc_info=True,
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
    non-string, or NUL-bearing value never reaches the query, AND only when the token's
    ``email_verified`` claim is exactly ``True``.

    The legacy path resolves ``sub`` against the integer ``User.id`` and admits a truthy claim
    only when it already *is* the integer the column holds - either a non-boolean ``int``, or a
    string that is the one canonical spelling of one. A value the column cannot hold, and a
    value that would silently resolve to a *different* identifier than it reads as, are both
    refused rather than converted. Every rejection is recorded in the server log.

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
            # SECURITY: an identity claim is required; a token carrying none names no user
            # and is refused before the query is built.
            logger.warning(
                "Rejected a bearer token: no %s claim for the %s verifier",
                LEGACY_JWT_IDENTITY_CLAIM,
                verifier,
            )
            raise credentials_exception
        try:
            # SECURITY: the claim is converted to the integer User.id holds before it is
            # queried, so only a value the column can hold reaches the database driver. A
            # bool is excluded because int() accepts it.
            if isinstance(identity, bool):
                raise ValueError("a boolean is not a user identifier")
            # SECURITY: only a canonical integer is admitted, because int() converted values
            # that are not user identifiers into ones that select a row belonging to somebody
            # else. A float 1.9 truncated to user 1; " 1 ", "+1", "01" and "-0" each resolved
            # to a user too; and "1_0" resolved to user 10. A string must therefore survive
            # the round trip int() -> str() unchanged, which admits exactly one spelling of
            # each identifier, and a non-string must already be the integer the column holds.
            if isinstance(identity, str):
                if str(int(identity)) != identity:
                    raise ValueError("not a canonical decimal integer")
            elif not isinstance(identity, int):
                raise ValueError("neither an integer nor a decimal integer string")
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
    # SECURITY: the email claim is admitted only as a non-empty, NUL-free string, so only a
    # value User.email can hold reaches the database driver.
    if not isinstance(identity, str) or not identity or "\x00" in identity:
        logger.warning(
            "Rejected a bearer token: no usable %s claim for the %s verifier",
            FIREBASE_IDENTITY_CLAIM,
            verifier,
        )
        raise credentials_exception
    # SECURITY: the address must be confirmed verified before it selects a stored user —
    # signature verification proves the token came from the configured project, not that its
    # holder controls the address it names, so anyone able to self-register could sign up with
    # a known local user's address and be handed that user's row (CWE-287)
    if claims.get(FIREBASE_EMAIL_VERIFIED_CLAIM) is not True:
        logger.warning(
            "Rejected a bearer token: its %s claim is not verified, so the address it "
            "names cannot be used to select a user",
            FIREBASE_IDENTITY_CLAIM,
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

    Registration contract: no ``BaseHTTPMiddleware`` may sit between this middleware and
    the router. The bypass reaches it through a ``ContextVar`` set while the route is being
    served, and Starlette's ``BaseHTTPMiddleware`` runs everything downstream of itself in a
    separate task whose context copy does not propagate back, so registering this outside
    one would still admit the request and still log the warning, but would leave this header
    silently absent. Only pure-ASGI middleware is registered inside this one - the throttling
    tiers, the request-body size limit and
    :class:`~backend.app.core.security_headers.ServerErrorBoundaryMiddleware` - so the 500
    the boundary produces still carries the marker.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _auth_enforcement_bypassed.set(False)
        _request_description.set(describe_request(scope))

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


def _resolve_current_user(
    token: str, bypass_state: Dict[str, bool]
) -> User:
    """Resolve ``token`` to the :class:`~backend.app.db.models.User` it identifies.

    Blocking throughout: reading the settings issues Secret Manager calls, verification
    reaches the Firebase Admin SDK or the local signing key, and the identity lookup
    queries the database. :func:`get_current_user` runs it on a worker thread.

    Owns the Session the lookup runs in and releases it before returning - on the admitted
    path, on the 401 path and on an unexpected database error alike - so no request leaves a
    Session holding a pooled connection. The caller receives a detached instance: the column
    values the lookup loaded are readable, and an attribute that would need a further query
    is not.

    Args:
        token: The bearer token value :data:`oauth2_scheme` took from the ``Authorization``
            header. A request carrying no such header never reaches this function.
        bypass_state: Single-entry mapping this sets under :data:`_BYPASS_STATE_KEY` once a
            caller is admitted while ``auth_enforcement_enabled`` is false, so the caller
            can record the bypass. A rejected request is never recorded as one.

    Returns:
        User: The caller, resolved by the identity claim the configured verifier names and
            detached from the Session that resolved it.

    Raises:
        HTTPException: 401 with a ``WWW-Authenticate: Bearer`` challenge if the token fails
            verification, could not be checked at all, carries no usable identity claim, or
            names no known user. No cause reaches the response; every outcome is recorded in
            the server log.
    """
    settings = get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    verifier = settings.auth_token_verifier
    if not token:
        # SECURITY: an empty credential is refused whatever the enforcement switch says.
        # oauth2_scheme refuses a request carrying no Authorization header before this
        # runs, so this guards an empty "Bearer " value, which no setting can admit.
        logger.warning("Rejected a request: the bearer credential was empty")
        raise credentials_exception
    if settings.auth_enforcement_enabled:
        # SECURITY: the bearer token is verified server-side, so the caller's asserted
        # identity is never taken on trust.
        claims = _verified_claims(
            token,
            verifier,
            settings,
            credentials_exception,
        )
    else:
        claims = _unverified_claims(token, credentials_exception)
    identity_column, identity = _resolve_identity(
        claims, verifier, credentials_exception
    )
    # SECURITY: the identity lookup owns its Session and releases it on every path, so no
    # request leaves a pooled connection held.
    db_context = get_db()
    try:
        db = next(db_context)
        user = db.query(User).filter(identity_column == identity).first()
        if user is None:
            # SECURITY: the cause is recorded server-side only, and names neither the
            # identity nor the token.
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
        # SECURITY: only an admitted request is reported as a bypass; a rejected one is not.
        bypass_state[_BYPASS_STATE_KEY] = True
    return user


async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    """Resolve the caller's bearer token to a :class:`~backend.app.db.models.User`.

    The request dependency every protected route depends on. The work is blocking and runs
    in :func:`_resolve_current_user` on a Starlette worker thread, so a slow Secret Manager
    read, token verification or database query cannot stall the event loop.

    The parameter is annotated ``str`` because this signature is a published contract that
    callers depend on, and the annotation is honest: :data:`oauth2_scheme` carries its
    own refusal unrelaxed, so a request with no ``Authorization: Bearer`` header is answered
    401 by the scheme itself and this function is never entered without a string.

    Args:
        token: The bearer token value, extracted from the ``Authorization`` header by
            :data:`oauth2_scheme`, which refuses a request carrying no such header before
            this function runs.

    Returns:
        User: The caller the token names - verified while ``auth_enforcement_enabled`` is
            true, and read from unverified claims while it is false. Detached from the
            Session that resolved it, which is closed before this returns, so the column
            values loaded during the lookup are readable and nothing lazy-loads.

    Raises:
        HTTPException: 401 with a ``WWW-Authenticate: Bearer`` challenge for any token that
            cannot be resolved to a known user, including one that could not be checked at
            all.
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
