"""HTTP security response headers for the FastAPI application.

Defines the canonical security header set emitted on API responses and the
Starlette middleware that applies it. Header values are module-level constants.
"""

from typing import Dict, Tuple

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from backend.app.core.config import get_settings

# Header name used when the policy is enforced.
CONTENT_SECURITY_POLICY_HEADER: str = "Content-Security-Policy"

# Header name used when the policy is reported but not enforced.
CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER: str = "Content-Security-Policy-Report-Only"

# Content-Security-Policy directives, joined into the policy value below.
# connect-src admits the API origin, the Firebase Authentication sign-in and ID
# token refresh hosts, and the Cloud Firestore host that also serves its
# streaming subscriptions.
CSP_DIRECTIVES: Tuple[str, ...] = (
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    (
        "connect-src 'self'"
        " https://identitytoolkit.googleapis.com"
        " https://securetoken.googleapis.com"
        " https://firestore.googleapis.com"
        " https://firebaseinstallations.googleapis.com"
    ),
    "upgrade-insecure-requests",
)

# The policy value. Identical whether it is enforced or reported.
CONTENT_SECURITY_POLICY: str = "; ".join(CSP_DIRECTIVES)

STRICT_TRANSPORT_SECURITY: str = "max-age=63072000; includeSubDomains; preload"
X_FRAME_OPTIONS: str = "DENY"
X_CONTENT_TYPE_OPTIONS: str = "nosniff"
REFERRER_POLICY: str = "strict-origin-when-cross-origin"
PERMISSIONS_POLICY: str = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(),"
    " magnetometer=(), microphone=(), payment=(), usb=()"
)

# The five headers whose names and values do not depend on configuration.
STATIC_SECURITY_HEADERS: Dict[str, str] = {
    "Strict-Transport-Security": STRICT_TRANSPORT_SECURITY,
    "X-Frame-Options": X_FRAME_OPTIONS,
    "X-Content-Type-Options": X_CONTENT_TYPE_OPTIONS,
    "Referrer-Policy": REFERRER_POLICY,
    "Permissions-Policy": PERMISSIONS_POLICY,
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applies the canonical security header set to every response.

    The five headers in ``STATIC_SECURITY_HEADERS`` are always emitted. The
    Content-Security-Policy value is always ``CONTENT_SECURITY_POLICY``; the
    ``csp_report_only`` setting selects which of the two policy header names
    carries it, and only one of the two names is ever emitted.

    Headers are applied to whatever response the downstream application
    returns, without regard to its status code, the request path, the request
    method or the response content type.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        # Resolved once per application instance.
        self.csp_header_name: str = (
            CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER
            if get_settings().csp_report_only
            else CONTENT_SECURITY_POLICY_HEADER
        )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)

        # SECURITY: emit HTTP security headers — previously no response carried any
        for header_name, header_value in STATIC_SECURITY_HEADERS.items():
            response.headers[header_name] = header_value
        response.headers[self.csp_header_name] = CONTENT_SECURITY_POLICY

        return response
