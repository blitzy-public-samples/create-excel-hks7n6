"""HTTP security headers for every API response.

Emits the canonical header set on whatever response the application produces,
including error and CORS preflight responses. The header values here are the same
values infrastructure/docker/nginx.conf and the Terraform backend bucket emit.
"""

from typing import Dict

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from backend.app.core.config import get_settings

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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Sets the canonical security headers on every response.

    ``csp_report_only`` selects which of the two Content-Security-Policy header names
    carries the policy; the policy value is the same either way. It is read once, when
    the middleware is constructed.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._policy_header = (
            CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER
            if get_settings().csp_report_only
            else CONTENT_SECURITY_POLICY_HEADER
        )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # SECURITY: instructs the browser on script sources, framing, MIME sniffing,
        # referrer leakage and feature access - no response carried any security header
        response = await call_next(request)
        response.headers[self._policy_header] = CONTENT_SECURITY_POLICY
        for name, value in STATIC_SECURITY_HEADERS.items():
            response.headers[name] = value
        return response
