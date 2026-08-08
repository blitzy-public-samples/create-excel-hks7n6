"""HTTP security headers for every API response.

Emits the canonical header set on whatever response leaves the application, including
handled errors, throttling rejections, CORS preflight responses and unhandled server
errors. The header values here are the same values infrastructure/docker/nginx.conf and
the Terraform backend bucket emit.

Two middlewares cooperate, and their registration positions are part of the contract:

* :class:`SecurityHeadersMiddleware` is pure ASGI and stamps the headers onto the
  ``http.response.start`` message of any response that passes through it. It must be
  registered LAST on the application, so it is the OUTERMOST middleware and every response
  produced anywhere inside the stack passes through it.
* :class:`ServerErrorBoundaryMiddleware` is pure ASGI and converts an unhandled exception
  into a 500 response. It must be registered FIRST, so it is the INNERMOST middleware and
  the response it produces travels outward through every other wrapper - the authentication
  bypass marker, the throttling tiers, CORS and the header middleware included. Starlette
  places its own ``ServerErrorMiddleware`` outside all user middleware, so a 500 generated
  there reaches the client without passing through any of them.
"""

import logging
from typing import Dict

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

# Content-Security-Policy directives. connect-src admits the API on the serving origin
# plus the Identity Platform, secure-token, Firestore and Firebase-installations
# endpoints the single-page application calls directly.
CONTENT_SECURITY_POLICY: str = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "style-src-elem 'self'",
        "style-src-attr 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        (
            "connect-src 'self' https://identitytoolkit.googleapis.com "
            "https://securetoken.googleapis.com https://firestore.googleapis.com "
            "https://firebaseinstallations.googleapis.com"
        ),
        "upgrade-insecure-requests",
    )
)

# Header name carrying an enforced policy, and the name carrying the same policy when
# violations are only to be reported.
CONTENT_SECURITY_POLICY_HEADER: str = "Content-Security-Policy"
CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER: str = "Content-Security-Policy-Report-Only"

# The headers whose name does not vary. X-Frame-Options is emitted alongside the CSP
# frame-ancestors directive for browsers that do not support it.
STATIC_SECURITY_HEADERS: Dict[str, str] = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
}

# Body of the response an unhandled exception is converted into. Deliberately fixed: no
# exception type, message or traceback reaches the caller.
_SERVER_ERROR_STATUS: int = 500
_SERVER_ERROR_BODY: bytes = b'{"detail":"Internal Server Error"}'
_SERVER_ERROR_HEADERS = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_SERVER_ERROR_BODY)).encode("latin-1")),
]


class SecurityHeadersMiddleware:
    """Sets the canonical security headers on every response leaving the application.

    Pure ASGI: the headers are written onto the ``http.response.start`` message rather
    than onto a materialised response object, so a streaming response, a response produced
    by an inner middleware and a response produced by an exception handler are all covered
    identically.

    ``csp_report_only`` selects which of the two Content-Security-Policy header names
    carries the policy; the policy value is the same either way. It is read once, when the
    middleware is constructed.

    Registration contract: this must be the OUTERMOST middleware, added to the application
    after every other one, so that no response can be produced outside it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._policy_header = (
            CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER
            if get_settings().csp_report_only
            else CONTENT_SECURITY_POLICY_HEADER
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            # SECURITY: instructs the browser on script sources, framing, MIME sniffing,
            # referrer leakage and feature access - no response carried any security header
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[self._policy_header] = CONTENT_SECURITY_POLICY
                for name, value in STATIC_SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class ServerErrorBoundaryMiddleware:
    """Converts an unhandled exception into a 500 response produced inside the stack.

    Pure ASGI, and registered as the innermost middleware, so the 500 it produces travels
    outward through every other wrapper and therefore carries the security headers, the
    CORS headers and the authentication bypass marker that those wrappers add. Starlette's
    own ``ServerErrorMiddleware`` sits outside all user middleware, so the 500 it would
    otherwise produce carries none of them.

    The exception is recorded with its full server-side traceback and is not re-raised: the
    response has already been sent from inside the stack, and re-raising past a
    ``BaseHTTPMiddleware`` after the response has started has no defined behaviour. Nothing
    about the exception reaches the caller.

    A request whose response has already started is left alone - the exception is recorded
    and propagates, because the status line cannot be rewritten once it is on the wire.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_tracking_start(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception:
            # SECURITY: an unhandled error is answered from inside the middleware stack, so
            # the response carries the security headers, the CORS headers and the bypass
            # marker - a server error produced outside the stack carried none of them
            logger.exception(
                "Unhandled exception serving %s %s; answering %s",
                scope.get("method", ""),
                scope.get("path", ""),
                _SERVER_ERROR_STATUS,
            )
            if response_started:
                raise
            await send(
                {
                    "type": "http.response.start",
                    "status": _SERVER_ERROR_STATUS,
                    "headers": list(_SERVER_ERROR_HEADERS),
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": _SERVER_ERROR_BODY,
                    "more_body": False,
                }
            )
