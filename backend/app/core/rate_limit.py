"""Two-tier per-client request throttling for the Excel Clone API.

Tier one applies an application-wide ceiling to every request, matched to a route or
not. Tier two applies a tighter budget to mutating HTTP methods. Both tiers count in
the same configured window store and identify clients the same way. Both are installed
as middleware by :func:`register_rate_limiting` and require nothing of any route
handler.
"""

import logging
import math
import time
from typing import FrozenSet, List, Tuple

from fastapi import FastAPI
from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage
from limits.storage import storage_from_string
from limits.storage.base import Storage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

TOO_MANY_REQUESTS_STATUS: int = 429

MIN_RETRY_AFTER_SECONDS: int = 1

# Window-store URI that counts inside the current process only.
IN_PROCESS_STORAGE_URI: str = "memory://"

# Header carrying the client address chain when a proxy sits in front of the API.
FORWARDED_FOR_HEADER: str = "x-forwarded-for"

# Bucket identifiers keeping the two tiers' counters independent of each other and of
# slowapi's own application-scoped bucket.
ALL_REQUESTS_SCOPE: str = "all-requests"
WRITE_REQUESTS_SCOPE: str = "write-requests"

# Smallest request allowance a configured window may declare. A window allowing zero
# requests would reject every request rather than throttle bursts.
MIN_LIMIT_AMOUNT: int = 1

# Smallest window length, as a multiple of the window's granularity, a configured window
# may declare.
MIN_LIMIT_MULTIPLES: int = 1


def resolve_client_key(scope: Scope, trusted_proxy_hops: int) -> str:
    """Return the throttling identity of the caller in ``scope``.

    With ``trusted_proxy_hops`` at 0 the socket peer address is used and any
    ``X-Forwarded-For`` header is ignored, so a caller cannot choose its own identity.
    With a positive value, that many entries are discarded from the right of
    ``X-Forwarded-For`` — those being the addresses the trusted proxies themselves
    appended — and the next entry to the left is used. A header too short to satisfy
    the configured hop count is not trusted either, and the socket address is used.

    The peer address comes from ``slowapi.util.get_remote_address``, which returns a
    fixed loopback address when the scope carries no client. Such requests therefore
    share one budget rather than escaping the tiers.
    """
    request = Request(scope)
    peer = get_remote_address(request)
    if trusted_proxy_hops <= 0:
        return peer

    forwarded = request.headers.get(FORWARDED_FOR_HEADER, "")
    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(chain) < trusted_proxy_hops + 1:
        return peer
    return chain[-(trusted_proxy_hops + 1)]


class ClientRateLimitMiddleware:
    """Meters every request per client, and mutating requests a second time.

    Runs outside route matching, so an unmatched path consumes budget exactly as a
    matched one does. Non-HTTP scopes are passed through untouched.

    Two independent budgets are enforced. Every HTTP request is charged against
    ``all_requests_limits``. A request whose method appears in :data:`WRITE_METHODS` is
    additionally charged against ``write_limit``. A client over either budget receives
    an HTTP 429 carrying ``Retry-After`` and the request does not reach the
    application.

    The instance holds no per-client state; counting is delegated to the ``limiter``
    supplied at construction, which owns the window store.
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: FixedWindowRateLimiter,
        all_requests_limits: Tuple[RateLimitItem, ...],
        write_limit: RateLimitItem,
        trusted_proxy_hops: int,
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._all_requests_limits = all_requests_limits
        self._write_limit = write_limit
        self._trusted_proxy_hops = trusted_proxy_hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_key = resolve_client_key(scope, self._trusted_proxy_hops)
        charges = [(limit, ALL_REQUESTS_SCOPE) for limit in self._all_requests_limits]
        if scope.get("method", "").upper() in WRITE_METHODS:
            charges.append((self._write_limit, WRITE_REQUESTS_SCOPE))

        for limit, bucket in charges:
            if not self._hit(limit, client_key, bucket):
                await self._build_rejection(limit, client_key, bucket)(scope, receive, send)
                return

        await self.app(scope, receive, send)

    def _hit(self, limit: RateLimitItem, client_key: str, bucket: str) -> bool:
        """Charge one request, treating a store failure as a pass.

        A window store that has become unreachable must not take the API down with it,
        so the request is admitted and the failure is logged.
        """
        try:
            return self._limiter.hit(limit, client_key, bucket)
        except Exception:
            logger.exception(
                "Rate limit store unavailable; admitting request without metering %s", bucket
            )
            return True

    def _retry_after_seconds(self, limit: RateLimitItem, client_key: str, bucket: str) -> int:
        """Whole seconds until this client's window resets, never below one."""
        try:
            reset_time = self._limiter.get_window_stats(limit, client_key, bucket).reset_time
        except Exception:
            logger.exception("Rate limit store unavailable reading window stats for %s", bucket)
            return MIN_RETRY_AFTER_SECONDS
        return max(MIN_RETRY_AFTER_SECONDS, math.ceil(reset_time - time.time()))

    def _build_rejection(
        self, limit: RateLimitItem, client_key: str, bucket: str
    ) -> JSONResponse:
        return JSONResponse(
            {"error": f"Rate limit exceeded: {limit}"},
            status_code=TOO_MANY_REQUESTS_STATUS,
            headers={"Retry-After": str(self._retry_after_seconds(limit, client_key, bucket))},
        )


def _parse_limits(expression: str, setting_name: str) -> List[RateLimitItem]:
    """Return every window declared in ``expression``.

    Args:
        expression: One or more windows separated by semicolons.
        setting_name: The ``Settings`` field being validated, named in any error raised.

    Returns:
        Every parsed window, in the order written.

    Raises:
        ValueError: ``expression`` is not valid rate limit grammar, declares no window at
            all, or declares a window that permits no requests. A zero request allowance
            rejects every request; a zero-length window meters nothing.
    """
    try:
        limits = parse_many(expression)
    except ValueError as exc:
        raise ValueError(
            f"Settings.{setting_name} is not a valid rate limit expression: {expression!r}"
        ) from exc

    if not limits:
        raise ValueError(
            f"Settings.{setting_name} declares no rate limit window: {expression!r}"
        )

    for limit in limits:
        if limit.amount < MIN_LIMIT_AMOUNT or limit.multiples < MIN_LIMIT_MULTIPLES:
            raise ValueError(
                f"Settings.{setting_name} declares a window that permits no requests: "
                f"{limit} in {expression!r}"
            )
    return limits


def _validate_default_limit(expression: str) -> List[RateLimitItem]:
    """Return ``expression`` parsed into its windows, once every window is valid.

    Accepts several windows separated by semicolons; the global tier applies all of them.

    Raises:
        ValueError: as described on :func:`_parse_limits`.
    """
    return _parse_limits(expression, "rate_limit_default")


def _parse_write_limit(expression: str) -> RateLimitItem:
    """Return the single window the write tier meters against.

    Raises:
        ValueError: as described on :func:`_parse_limits`, or when ``expression`` declares
            more than one window. The write tier meters against one window, so any further
            window would be discarded rather than enforced.
    """
    limits = _parse_limits(expression, "rate_limit_write")
    if len(limits) > 1:
        raise ValueError(
            "Settings.rate_limit_write must declare exactly one rate limit window, but "
            f"{len(limits)} were given: {expression!r}"
        )
    return limits[0]


def resolve_storage(storage_uri: str) -> Tuple[Storage, str]:
    """Return the window store for ``storage_uri`` and the URI actually in use.

    A store that cannot be constructed or does not answer a liveness check is
    replaced by process-local memory and the substitution is logged as a warning,
    because per-process counting multiplies every client's effective quota by the
    number of workers and pods. Refusing to start would trade that for an outage,
    so the API starts and the degradation is reported.
    """
    if storage_uri == IN_PROCESS_STORAGE_URI:
        return MemoryStorage(), IN_PROCESS_STORAGE_URI
    try:
        storage = storage_from_string(storage_uri)
        if not storage.check():
            raise RuntimeError("liveness check failed")
    except Exception as exc:
        logger.warning(
            "Rate limit store %r is unavailable (%s); counting per process instead, "
            "so each client's effective quota is multiplied by the number of workers.",
            storage_uri,
            exc,
        )
        return MemoryStorage(), IN_PROCESS_STORAGE_URI
    return storage, storage_uri


def register_rate_limiting(app: FastAPI) -> None:
    """Install both throttling tiers on ``app``.

    Registers nothing whatsoever — no middleware, no exception handler and no
    ``app.state.limiter`` — when ``Settings.rate_limit_enabled`` is false.

    Raises:
        ValueError: before either tier is installed, when either configured threshold is
            not valid rate limit grammar, declares a window that permits no requests, or —
            for the write tier — declares more than one window.
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return

    all_requests_limits = tuple(_validate_default_limit(settings.rate_limit_default))
    write_limit = _parse_write_limit(settings.rate_limit_write)
    storage, storage_uri = resolve_storage(settings.rate_limit_storage_uri)
    trusted_proxy_hops = settings.rate_limit_trusted_proxy_hops

    def client_key_func(request: Request) -> str:
        return resolve_client_key(request.scope, trusted_proxy_hops)

    # SECURITY: application-wide per-client request ceiling — request volume was
    # previously unbounded, and a per-endpoint ceiling would grant a fresh budget per route
    limiter = Limiter(
        key_func=client_key_func,
        application_limits=[settings.rate_limit_default],
        storage_uri=storage_uri,
        headers_enabled=True,
        swallow_errors=True,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # SECURITY: ceiling applied outside route matching, plus a tighter one on mutating
    # methods — unmatched paths previously consumed no budget and writes were unthrottled
    # Installed after the tier above, which places this one outermost: it is evaluated first.
    app.add_middleware(
        ClientRateLimitMiddleware,
        limiter=FixedWindowRateLimiter(storage),
        all_requests_limits=all_requests_limits,
        write_limit=write_limit,
        trusted_proxy_hops=trusted_proxy_hops,
    )
