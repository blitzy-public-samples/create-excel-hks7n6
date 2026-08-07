"""Authentication primitives for the FastAPI application.

Provides the password hashing helpers, the access-token minting helper, and
``get_current_user`` — the request dependency that resolves the caller's bearer token to
a :class:`~backend.app.db.models.User`.

``get_current_user`` verifies the token server-side. Two settings, both read on every
request, govern it: ``auth_token_verifier`` selects the verification path (``firebase``
validates a Firebase ID token through the Admin SDK and resolves the user by the verified
``email`` claim; ``legacy_jwt`` validates the locally-issued HS256 token and resolves by
``sub``), and ``auth_enforcement_enabled`` set false serves requests on unverified token
claims, logging a warning and marking each such response.

The Firebase Admin SDK is initialised on first use inside the request path, never at
import. :class:`AuthEnforcementBypassMarkerMiddleware` emits the bypass marker header and
must be registered on the application for that header to reach clients.
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
from typing import Any, Dict, Optional
import firebase_admin
from firebase_admin import auth as firebase_auth, credentials
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from backend.app.core.config import Settings, get_settings
from backend.app.db.models import User
from backend.app.db.database import get_db

logger = logging.getLogger(__name__)

# The verifier value that selects the locally-issued HS256 token path. Every other
# accepted value selects Firebase ID token verification.
LEGACY_JWT_VERIFIER = "legacy_jwt"

# The claim each verification path treats as the caller's identity.
FIREBASE_IDENTITY_CLAIM = "email"
LEGACY_JWT_IDENTITY_CLAIM = "sub"

# Marks a response served while authentication enforcement was disabled.
AUTH_ENFORCEMENT_BYPASS_HEADER = "X-Auth-Enforcement-Bypassed"
AUTH_ENFORCEMENT_BYPASS_HEADER_VALUE = "true"

# Set on the bypass path and read while the response headers are being sent. A pure ASGI
# middleware sends from the same context the dependency ran in, so the flag is visible
# there. The default is false, and the middleware resets it as each request begins.
_auth_enforcement_bypassed: ContextVar[bool] = ContextVar(
    "auth_enforcement_bypassed", default=False
)

# Serialises first use of the Admin SDK: initialising it twice raises.
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
    if expires_delta:
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

def _firebase_app(project_id: str) -> firebase_admin.App:
    """Return the default Firebase app, initialising it on first use.

    Initialisation happens here, inside the request path, rather than at module import.
    ``firebase_admin.get_app`` raises :class:`ValueError` while no app exists and
    ``firebase_admin.initialize_app`` raises :class:`ValueError` once one does, so the
    check is repeated under a lock: concurrent first requests then produce exactly one
    initialisation and every later call is a lock-free lookup.

    Args:
        project_id: Firebase project the token issuer must match. When empty, the SDK
            resolves the project from the ambient credentials or environment.

    Returns:
        firebase_admin.App: The initialised default app.

    Raises:
        ValueError: If the app cannot be initialised.
    """
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass
    options = {"projectId": project_id} if project_id else None
    with _firebase_app_lock:
        try:
            return firebase_admin.get_app()
        except ValueError:
            # SECURITY: application default credentials only — no service account key
            # file is read
            return firebase_admin.initialize_app(
                credentials.ApplicationDefault(), options
            )


def _verified_claims(
    token: str, verifier: str, settings: Settings, credentials_exception: HTTPException
) -> Dict[str, Any]:
    """Return the claims of ``token`` once its signature and validity are established.

    Args:
        token: The bearer token value taken from the ``Authorization`` header.
        verifier: The configured ``auth_token_verifier`` value.
        settings: The settings instance for this request.
        credentials_exception: The 401 raised for every verification failure.

    Returns:
        Dict[str, Any]: The verified claims.

    Raises:
        HTTPException: ``credentials_exception``, for every failure, with the cause
            recorded in the server log and absent from the response.
    """
    if verifier == LEGACY_JWT_VERIFIER:
        try:
            return jwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
        except JWTError as exc:
            # SECURITY: the exception class is logged, never its message — a token
            # library's message can carry the offending token itself
            logger.warning(
                "Rejected a bearer token: local JWT verification failed (%s)",
                type(exc).__name__,
            )
            raise credentials_exception from None
    try:
        return firebase_auth.verify_id_token(
            token, app=_firebase_app(settings.firebase_project_id)
        )
    except (
        firebase_auth.InvalidIdTokenError,
        firebase_auth.ExpiredIdTokenError,
        firebase_auth.RevokedIdTokenError,
        # Raised rather than an auth error when the Admin SDK itself could not be used.
        ValueError,
    ) as exc:
        logger.warning(
            "Rejected a bearer token: Firebase ID token verification failed (%s)",
            type(exc).__name__,
        )
        raise credentials_exception from None


def _unverified_claims(
    token: str, credentials_exception: HTTPException
) -> Dict[str, Any]:
    """Return the claims of ``token`` without checking its signature.

    Reached only while ``auth_enforcement_enabled`` is false. Every such request is
    logged at warning level and its response is marked.

    Args:
        token: The bearer token value taken from the ``Authorization`` header.
        credentials_exception: The 401 raised when no claims can be read at all.

    Returns:
        Dict[str, Any]: The token's unverified claims.

    Raises:
        HTTPException: ``credentials_exception``, if the token carries no readable claims.
    """
    _auth_enforcement_bypassed.set(True)
    logger.warning(
        "Authentication enforcement is disabled: serving a request on unverified token "
        "claims. Set auth_enforcement_enabled true to restore verification."
    )
    try:
        return jwt.get_unverified_claims(token)
    except JWTError as exc:
        logger.warning(
            "Rejected a bearer token: no readable claims (%s)", type(exc).__name__
        )
        raise credentials_exception from None


def auth_enforcement_bypassed() -> bool:
    """Whether the request being served bypassed token verification."""
    return _auth_enforcement_bypassed.get()


class AuthEnforcementBypassMarkerMiddleware:
    """Marks responses served while authentication enforcement was disabled.

    Adds :data:`AUTH_ENFORCEMENT_BYPASS_HEADER` to any response whose request reached
    :func:`get_current_user` with ``auth_enforcement_enabled`` false. The header reaches
    clients only while this middleware is registered on the application; the warning
    ``get_current_user`` logs for the same request does not depend on it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _auth_enforcement_bypassed.set(False)

        async def send_with_marker(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and _auth_enforcement_bypassed.get()
            ):
                # SECURITY: a response served without token verification is marked
                MutableHeaders(raw=message["headers"])[
                    AUTH_ENFORCEMENT_BYPASS_HEADER
                ] = AUTH_ENFORCEMENT_BYPASS_HEADER_VALUE
            await send(message)

        await self.app(scope, receive, send_with_marker)


# HUMAN ASSISTANCE NEEDED
# This function needs additional error handling and might require adjustments based on the actual database implementation
async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    settings = get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    verifier = settings.auth_token_verifier
    if settings.auth_enforcement_enabled:
        # SECURITY: the bearer token is verified server-side — the caller's asserted
        # identity was never checked
        claims = _verified_claims(token, verifier, settings, credentials_exception)
    else:
        claims = _unverified_claims(token, credentials_exception)
    if verifier == LEGACY_JWT_VERIFIER:
        identity = claims.get(LEGACY_JWT_IDENTITY_CLAIM)
        identity_column = User.id
    else:
        identity = claims.get(FIREBASE_IDENTITY_CLAIM)
        identity_column = User.email
    if not identity:
        # SECURITY: a token carrying no identity claim is rejected — it would otherwise
        # match a row on a null comparison
        logger.warning(
            "Rejected a bearer token: no identity claim for the %s verifier", verifier
        )
        raise credentials_exception
    db = next(get_db())
    user = db.query(User).filter(identity_column == identity).first()
    if user is None:
        raise credentials_exception
    return user