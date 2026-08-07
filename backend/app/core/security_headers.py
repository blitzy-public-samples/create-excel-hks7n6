"""HTTP security response headers for the FastAPI application.

Defines the canonical security header set emitted on API responses, the Starlette
middleware that applies it, and the unhandled-exception handler that applies it to
the 500 response Starlette synthesises outside the middleware stack. Header values
are module-level constants.
"""

import logging
from typing import Dict, Mapping, MutableMapping, Optional, Tuple

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

CONTENT_SECURITY_POLICY_HEADER: str = "Content-Security-Policy"

CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER: str = "Content-Security-Policy-Report-Only"

# Header that names the destinations `report-to` may refer to.
REPORTING_ENDPOINTS_HEADER: str = "Reporting-Endpoints"

# Reporting group name shared by the Reporting-Endpoints header and `report-to`.
CSP_REPORT_GROUP: str = "csp-endpoint"

# Content-Security-Policy directives that precede connect-src in the policy value.
# style-src-attr carries the only inline style sink the application uses, the
# React `style` attribute on a spreadsheet cell; stylesheets are served as files
# and are covered by style-src 'self'.
CSP_LEADING_DIRECTIVES: Tuple[str, ...] = (
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "style-src-elem 'self'",
    # SECURITY: inline styles are confined to the style attribute — the policy
    # previously allowed inline <style> elements and stylesheets as well
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
)

# connect-src sources admitted unconditionally: the document's own origin and the listed
# Firebase Authentication, installations and Cloud Firestore endpoints, including the
# Firestore host that serves its streaming subscriptions. 'self' covers the API only when
# one origin serves both the SPA document and the API; a cross-origin API is admitted
# through Settings.api_origin.
CSP_CONNECT_SRC_SOURCES: Tuple[str, ...] = (
    "'self'",
    "https://identitytoolkit.googleapis.com",
    "https://securetoken.googleapis.com",
    "https://firestore.googleapis.com",
    "https://firebaseinstallations.googleapis.com",
)

# Content-Security-Policy directives that follow connect-src in the policy value.
CSP_TRAILING_DIRECTIVES: Tuple[str, ...] = ("upgrade-insecure-requests",)

# Body of the response returned when an unhandled exception escapes the application.
# Deliberately carries no exception detail.
INTERNAL_SERVER_ERROR_BODY: Dict[str, str] = {"detail": "Internal Server Error"}

INTERNAL_SERVER_ERROR_STATUS: int = 500

STRICT_TRANSPORT_SECURITY: str = "max-age=63072000; includeSubDomains; preload"
X_FRAME_OPTIONS: str = "DENY"
X_CONTENT_TYPE_OPTIONS: str = "nosniff"
REFERRER_POLICY: str = "strict-origin-when-cross-origin"
PERMISSIONS_POLICY: str = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(),"
    " magnetometer=(), microphone=(), payment=(), usb=()"
)

STATIC_SECURITY_HEADERS: Dict[str, str] = {
    "Strict-Transport-Security": STRICT_TRANSPORT_SECURITY,
    "X-Frame-Options": X_FRAME_OPTIONS,
    "X-Content-Type-Options": X_CONTENT_TYPE_OPTIONS,
    "Referrer-Policy": REFERRER_POLICY,
    "Permissions-Policy": PERMISSIONS_POLICY,
}


def build_content_security_policy(api_origin: str = "", report_uri: str = "") -> str:
    """Return the policy value for ``api_origin`` and ``report_uri``.

    ``api_origin`` is appended to ``connect-src`` when supplied, which is what admits a
    cross-origin API; it must already be an exact browser origin, and ``Settings``
    validates it. An empty value, or one already present, adds no source.

    ``report_uri`` supplied adds ``report-to``, naming the group the
    ``Reporting-Endpoints`` header declares, and ``report-uri`` for user agents that do
    not implement Reporting API v1. It must already be an absolute https URL made of
    printable ASCII carrying no ``;``, ``,`` or ``"``, and ``Settings`` validates it: the
    value is joined into this policy with ``"; "``, so a directive separator or a line
    break inside it would become part of the policy rather than part of the URL.
    """
    connect_src_sources = CSP_CONNECT_SRC_SOURCES
    if api_origin and api_origin not in connect_src_sources:
        connect_src_sources = connect_src_sources + (api_origin,)

    directives = (
        CSP_LEADING_DIRECTIVES
        + ("connect-src " + " ".join(connect_src_sources),)
        + CSP_TRAILING_DIRECTIVES
    )
    if report_uri:
        directives = directives + (
            f"report-to {CSP_REPORT_GROUP}",
            f"report-uri {report_uri}",
        )
    return "; ".join(directives)


# The policy value for same-origin delivery, where 'self' already covers the API. This is
# the baseline the SPA meta tag, the Nginx configuration and the load-balancer custom
# response headers carry.
CONTENT_SECURITY_POLICY: str = build_content_security_policy()

# Attribute on the application state carrying the delivery values resolved for that
# application.
POLICY_DELIVERY_STATE_ATTRIBUTE: str = "security_header_policy_delivery"

# Delivery values applied when configuration cannot be read: the same-origin policy,
# enforced, with no reporting endpoint.
FALLBACK_POLICY_DELIVERY: Tuple[str, str, str] = (
    CONTENT_SECURITY_POLICY_HEADER,
    CONTENT_SECURITY_POLICY,
    "",
)


def build_reporting_endpoints(report_uri: str) -> str:
    """Return the ``Reporting-Endpoints`` value for ``report_uri``, or ``''``.

    ``report_uri`` is emitted as a quoted structured-field value, so it must already
    carry no ``"`` to close that quoting and no ``,`` to start a further member;
    ``Settings`` validates it.
    """
    if not report_uri:
        return ""
    return f'{CSP_REPORT_GROUP}="{report_uri}"'


def apply_security_header_values(
    headers: MutableMapping[str, str],
    csp_header_name: str,
    policy: str,
    reporting_endpoints: str,
) -> None:
    """Set the canonical header set on any mutable header mapping."""
    # SECURITY: emit security headers; responses previously carried none.
    for header_name, header_value in STATIC_SECURITY_HEADERS.items():
        headers[header_name] = header_value
    headers[csp_header_name] = policy
    if reporting_endpoints:
        headers[REPORTING_ENDPOINTS_HEADER] = reporting_endpoints


def apply_security_headers(
    response: Response, csp_header_name: str, policy: str, reporting_endpoints: str
) -> Response:
    """Set the canonical header set on ``response`` and return it."""
    apply_security_header_values(
        response.headers, csp_header_name, policy, reporting_endpoints
    )
    return response


def resolve_policy_delivery() -> Tuple[str, str, str]:
    """Return the CSP header name, the policy value and the reporting endpoints value.

    Reads ``csp_report_only``, ``csp_report_uri`` and ``api_origin`` once.
    ``csp_report_only`` selects the header name, ``api_origin`` extends connect-src, and
    ``csp_report_uri`` adds the reporting directives. Both values reach this function
    already validated by ``Settings``, which is what keeps the policy and the reporting
    header this function returns free of any character that could alter them. Emits a
    warning when the policy is report-only with no collector configured, because that
    combination neither blocks a violation nor records one.
    """
    settings = get_settings()
    report_uri = settings.csp_report_uri
    if settings.csp_report_only and not report_uri:
        logger.warning(
            "Content-Security-Policy is report-only and csp_report_uri is unset: "
            "violations are neither blocked nor reported."
        )
    csp_header_name = (
        CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER
        if settings.csp_report_only
        else CONTENT_SECURITY_POLICY_HEADER
    )
    return (
        csp_header_name,
        build_content_security_policy(settings.api_origin, report_uri),
        build_reporting_endpoints(report_uri),
    )


def resolve_policy_delivery_for_application(
    application: Optional[Starlette],
) -> Tuple[str, str, str]:
    """Return the delivery values for ``application``, resolving them at most once.

    The values :func:`resolve_policy_delivery` returns are kept on the application's
    state under :data:`POLICY_DELIVERY_STATE_ATTRIBUTE` and reused, so a response built
    outside the middleware does not read configuration again for every such response.

    When configuration cannot be read, :data:`FALLBACK_POLICY_DELIVERY` is returned and
    the failure is logged. Nothing is stored in that case, so a later call resolves
    again once configuration is readable.
    """
    state = getattr(application, "state", None)
    resolved = getattr(state, POLICY_DELIVERY_STATE_ATTRIBUTE, None)
    if resolved is not None:
        return resolved

    try:
        resolved = resolve_policy_delivery()
    except Exception:
        logger.exception(
            "Security header policy could not be resolved from configuration; "
            "applying the enforced same-origin policy"
        )
        return FALLBACK_POLICY_DELIVERY

    if state is not None:
        setattr(state, POLICY_DELIVERY_STATE_ATTRIBUTE, resolved)
    return resolved


async def security_headers_server_error_handler(
    request: Request, exc: Exception
) -> Response:
    """Return a 500 carrying the canonical header set and no exception detail.

    Registered as the handler for :class:`Exception` so that the response Starlette
    synthesises for an unhandled exception is covered too. That response is produced
    by ``ServerErrorMiddleware``, which Starlette places outside every user
    middleware.

    The header set is applied whether or not configuration can be read, so an incident
    that makes configuration unreadable does not also strip the headers from the
    responses it produces.
    """
    # SECURITY: the unhandled-exception response carries the header set even when
    # configuration cannot be read — it then carried none of them and no body
    csp_header_name, policy, reporting_endpoints = resolve_policy_delivery_for_application(
        request.scope.get("app")
    )
    return apply_security_headers(
        JSONResponse(INTERNAL_SERVER_ERROR_BODY, status_code=INTERNAL_SERVER_ERROR_STATUS),
        csp_header_name,
        policy,
        reporting_endpoints,
    )


class SecurityHeadersMiddleware:
    """Applies the canonical security header set to every response.

    The five headers in ``STATIC_SECURITY_HEADERS`` are always emitted. The
    Content-Security-Policy value is always the one ``resolve_policy_delivery``
    builds; the ``csp_report_only`` setting selects which of the two policy header
    names carries it, and only one of the two names is ever emitted.

    Headers are written into the ``http.response.start`` message of whatever response
    passes through, without regard to its status code, the request path, the request
    method or the response content type. Response status, body and content are left
    untouched, and no content is buffered.

    Coverage extends to an exception that reaches this middleware with no response yet
    started. ``ServerErrorMiddleware`` builds its own ``500`` for such exceptions and
    sits outside every application middleware. When the application registers a handler
    for :class:`Exception` — ``security_headers_server_error_handler`` is provided for
    exactly that — the exception is re-raised so that handler produces the headered
    response. When it registers none, a ``500`` carrying the same headers is emitted
    here and the exception is then re-raised, so the layer outside this one still
    records it. The outer layer is left to render its own response when it has already
    begun sending one, and when the application runs in debug mode, where it renders a
    traceback instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Resolved once per application instance.
        (
            self.csp_header_name,
            self.content_security_policy,
            self.reporting_endpoints,
        ) = resolve_policy_delivery()

    def _apply(self, headers: MutableMapping[str, str]) -> None:
        apply_security_header_values(
            headers,
            self.csp_header_name,
            self.content_security_policy,
            self.reporting_endpoints,
        )

    @staticmethod
    def _server_error_handler_registered(scope: Scope) -> bool:
        """Whether the application has a handler for an unhandled exception."""
        application = scope.get("app")
        handlers: Mapping = getattr(application, "exception_handlers", {}) or {}
        return Exception in handlers or INTERNAL_SERVER_ERROR_STATUS in handlers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                self._apply(MutableHeaders(raw=message["headers"]))
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            application = scope.get("app")
            if (
                not response_started
                and not getattr(application, "debug", False)
                and not self._server_error_handler_registered(scope)
            ):
                # SECURITY: security headers cover the unhandled-exception response —
                # it is synthesised outside this middleware and carried none
                logger.exception(
                    "Unhandled exception serving %s %s",
                    scope.get("method"),
                    scope.get("path"),
                )
                response = JSONResponse(
                    INTERNAL_SERVER_ERROR_BODY,
                    status_code=INTERNAL_SERVER_ERROR_STATUS,
                )
                self._apply(response.headers)
                await response(scope, receive, send)
            raise
