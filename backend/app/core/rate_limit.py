"""Two-tier per-client request throttling for the Excel Clone API.

Tier one applies an application-wide ceiling to every request. Tier two applies a
tighter budget to mutating HTTP methods. Both tiers count in the same shared window
store, so a client's quota is the configured quota rather than that quota multiplied
by the number of workers and pods serving the API. Both are installed as middleware
by :func:`register_rate_limiting`, so no route handler requires a ``request``
parameter or a decorator.

Clients are identified by :func:`resolve_client_key`, which consults a forwarded
address only as far as the configured number of trusted proxy hops allows.
"""

import logging
import math
import time
from typing import FrozenSet, List, Optional, Tuple

from fastapi import FastAPI
from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage, storage_from_string
from limits.storage.base import Storage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

# HTTP methods metered by the write tier. Every other method bypasses that tier.
_WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_TOO_MANY_REQUESTS_STATUS: int = 429

_RETRY_AFTER_HEADER: str = "Retry-After"

# Floor, in seconds, for the Retry-After value of a rejection.
_MIN_RETRY_AFTER_SECONDS: int = 1

# Bucket identifier keeping the write tier's counters independent of the ceiling tier.
_WRITE_REQUESTS_SCOPE: str = "write-requests"

# Header carrying the proxy-supplied client chain.
_FORWARDED_FOR_HEADER: bytes = b"x-forwarded-for"

# Window store used when no shared store is configured or the configured one is
# unreachable. Counts in process memory, so quotas are per worker.
_IN_PROCESS_STORAGE_URI: str = "memory://"

# Client key used when neither a socket address nor a trusted forwarded address is
# available, so such requests share one budget rather than escaping metering.
_UNKNOWN_CLIENT_KEY: str = "unknown"


def resolve_storage(
    configured_uri: str, fallback_uri: str
) -> Tuple[Storage, str]:
    """Return the window store both tiers count in, and the URI it was built from.

    ``configured_uri`` wins when set; otherwise ``fallback_uri`` is used, which is how
    an unset ``rate_limit_storage_uri`` derives the store from ``REDIS_URL``. A store
    that cannot be constructed or does not answer is replaced by in-process counting
    and the substitution is logged, because a counter store that is unreachable must
    degrade the control rather than refuse every request and take the API down with it.

    Returns:
        The storage instance and the URI actually in use, which is
        ``memory://`` whenever the intended store was unusable.
    """
    intended = (configured_uri or fallback_uri or "").strip() or _IN_PROCESS_STORAGE_URI
    if intended == _IN_PROCESS_STORAGE_URI:
        return MemoryStorage(), _IN_PROCESS_STORAGE_URI

    try:
        storage = storage_from_string(intended)
        reachable = storage.check()
    except Exception:
        logger.warning(
            "Rate-limit window store could not be created; counting per process "
            "instead, so each worker enforces its own copy of the quota",
            exc_info=True,
        )
        return MemoryStorage(), _IN_PROCESS_STORAGE_URI

    if not reachable:
        logger.warning(
            "Rate-limit window store did not answer; counting per process instead, "
            "so each worker enforces its own copy of the quota"
        )
        return MemoryStorage(), _IN_PROCESS_STORAGE_URI

    return storage, intended


def resolve_client_key(scope: Scope, trusted_proxy_hops: int) -> str:
    """Return the throttling identity for the request described by ``scope``.

    With ``trusted_proxy_hops`` at 0 only the socket peer address is used, so a
    client-supplied ``X-Forwarded-For`` cannot influence which budget it spends.

    Above 0, the address is read from ``X-Forwarded-For`` counting from the RIGHT:
    the rightmost entry was written by the nearest proxy, so entry
    ``-(trusted_proxy_hops + 1)`` is the one the outermost trusted proxy recorded.
    Counting from the right is what makes the value unspoofable - a client that
    prepends extra entries lengthens the chain but cannot move the position the
    trusted proxy writes to. Behind the Google global load balancer one hop is
    correct, because it appends its own address after the client's.

    A chain shorter than the configured hop count means the expected proxies did not
    append to it, so the header is not trusted and the socket address is used.
    """
    socket_address = ""
    client = scope.get("client")
    if client:
        socket_address = client[0] or ""

    if trusted_proxy_hops > 0:
        forwarded = _forwarded_for(scope)
        if forwarded:
            entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
            index = len(entries) - 1 - trusted_proxy_hops
            if index >= 0:
                return entries[index]

    return socket_address or _UNKNOWN_CLIENT_KEY


def _forwarded_for(scope: Scope) -> Optional[str]:
    """Return the raw ``X-Forwarded-For`` value from ``scope``, or None."""
    for name, value in scope.get("headers") or ():
        if name.lower() == _FORWARDED_FOR_HEADER:
            return value.decode("latin-1")
    return None


def _parse_window_expression(expression: str, field_name: str) -> List[RateLimitItem]:
    """Return the windows ``expression`` declares.

    Raises:
        ValueError: if the expression declares no parseable window, because a tier
            built from it would admit every request while appearing installed. The
            message names ``field_name``, since the underlying parser reports only the
            offending string and both tiers are configured the same way.
    """
    try:
        windows = parse_many(expression)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a rate expression such as '600/minute'; "
            f"rejected: {expression!r} ({exc})"
        ) from exc
    if not windows:
        raise ValueError(
            f"{field_name} must be a rate expression such as '600/minute'; "
            f"rejected: {expression!r}"
        )
    return windows


class _WriteMethodRateLimitMiddleware(BaseHTTPMiddleware):
    """Meters POST, PUT, PATCH and DELETE against a fixed window per client.

    Requests using any other method are passed through without being counted. Counting
    happens in the shared store supplied at construction, so the budget is shared by
    every worker and pod rather than held per process.
    """

    def __init__(
        self,
        app,
        write_limits: List[RateLimitItem],
        storage: Storage,
        trusted_proxy_hops: int,
    ) -> None:
        super().__init__(app)
        self._write_limits = write_limits
        self._limiter = FixedWindowRateLimiter(storage)
        self._trusted_proxy_hops = trusted_proxy_hops

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # SECURITY: bounds mutating request volume per client - writes were unthrottled
        if request.method not in _WRITE_METHODS:
            return await call_next(request)

        client_key = resolve_client_key(request.scope, self._trusted_proxy_hops)
        try:
            for window in self._write_limits:
                if not self._limiter.hit(window, _WRITE_REQUESTS_SCOPE, client_key):
                    return self._rejection(window, client_key)
        except Exception:
            # A window store that stops answering must not deny every write. The
            # request proceeds unmetered and the outage is recorded, matching the
            # ceiling tier, which slowapi is configured to let through on error.
            logger.warning(
                "Write-tier throttling could not consult its window store; the "
                "request proceeds unmetered",
                exc_info=True,
            )
        return await call_next(request)

    def _rejection(self, window: RateLimitItem, client_key: str) -> Response:
        """Return the 429 for an exhausted ``window``, carrying Retry-After."""
        stats = self._limiter.get_window_stats(
            window, _WRITE_REQUESTS_SCOPE, client_key
        )
        retry_after = max(
            _MIN_RETRY_AFTER_SECONDS,
            int(math.ceil(stats.reset_time - time.time())),
        )
        return JSONResponse(
            status_code=_TOO_MANY_REQUESTS_STATUS,
            content={"error": f"Rate limit exceeded: {window}"},
            headers={_RETRY_AFTER_HEADER: str(retry_after)},
        )


def register_rate_limiting(app: FastAPI) -> None:
    """Install both throttling tiers on ``app``.

    Registers nothing at all when ``rate_limit_enabled`` is false: no middleware, no
    exception handler and no ``app.state.limiter``. Both window expressions are parsed
    here, so an unusable value fails at registration rather than at request time, and
    the window store is resolved once here so both tiers count in the same place.
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return

    write_limits = _parse_window_expression(
        settings.rate_limit_write, "rate_limit_write"
    )
    _parse_window_expression(settings.rate_limit_default, "rate_limit_default")

    # SECURITY: both tiers count in one shared store - per-process counters let a
    # client's effective quota multiply by the number of workers and pods
    storage, storage_uri = resolve_storage(
        settings.rate_limit_storage_uri, settings.REDIS_URL
    )
    trusted_proxy_hops = settings.rate_limit_trusted_proxy_hops

    app.add_middleware(
        _WriteMethodRateLimitMiddleware,
        write_limits=write_limits,
        storage=storage,
        trusted_proxy_hops=trusted_proxy_hops,
    )

    # SECURITY: bounds total request volume per client - brute force, credential
    # stuffing and scraping were unthrottled on every route
    # SECURITY: the client identity honours only as many forwarded hops as are
    # configured - the socket peer address alone made every client behind the load
    # balancer share one budget
    def _client_key(request: Request) -> str:
        return resolve_client_key(request.scope, trusted_proxy_hops)

    limiter = Limiter(
        key_func=_client_key,
        default_limits=[settings.rate_limit_default],
        storage_uri=storage_uri,
        headers_enabled=True,
        swallow_errors=True,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
