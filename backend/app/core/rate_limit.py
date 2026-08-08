"""Two-tier per-client request throttling for the Excel Clone API.

Tier one applies an application-wide ceiling to every request. Tier two applies a
tighter budget to mutating HTTP methods. Both tiers count in the same shared window
store, so a client's quota is the configured quota rather than that quota multiplied
by the number of workers and pods serving the API. Both are installed as middleware
by :func:`register_rate_limiting`, so no route handler requires a ``request``
parameter or a decorator.

Clients are identified by :func:`resolve_client_key`, which consults a forwarded
address only when the request arrived from a configured trusted proxy.
"""

import logging
import math
import time
from ipaddress import ip_address, ip_network
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

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
from starlette.types import ASGIApp, Receive, Scope, Send

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

# Header declaring the request body size.
_CONTENT_LENGTH_HEADER: bytes = b"content-length"

_PAYLOAD_TOO_LARGE_STATUS: int = 413

# Largest request body the API accepts. The cell-update and share routes accept list
# bodies that declare no maximum cardinality, so request-count throttling alone does not
# bound the memory and database work a single authenticated request can demand.
# 10 MiB is far above any workbook the client sends and far below a body that could
# exhaust a worker: at roughly 60 bytes of JSON per cell it admits on the order of 170,000
# cells in one request and refuses anything larger.
_MAX_REQUEST_BODY_BYTES: int = 10 * 1024 * 1024

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


def resolve_client_key(scope: Scope, trusted_proxies: Sequence[str]) -> str:
    """Return the throttling identity for the request described by ``scope``.

    The socket peer address is the identity unless the peer is itself a trusted proxy.
    Verifying the peer is what makes a forwarded address usable: a chain is otherwise
    trusted on the strength of its own contents, so a caller reaching the API directly
    could name any address it liked and rotate through addresses to escape its quota.

    With ``trusted_proxies`` empty no forwarded header is read at all.

    When the peer matches one of ``trusted_proxies``, ``X-Forwarded-For`` is walked from
    the RIGHT - the rightmost entry was appended by the nearest proxy - and the first
    entry that is not itself a trusted proxy is the client. Entries a client prepended
    sit further left and are never reached, so lengthening the chain cannot move the
    position that is read. Behind the Google global load balancer this resolves to the
    address the balancer recorded for the client.

    A chain of nothing but trusted proxies, an unparseable entry in the position that
    would be read, and a request with no forwarded header at all each fall back to the
    socket peer.

    Args:
        scope: The ASGI connection scope of the request being metered.
        trusted_proxies: Addresses or CIDR networks whose forwarded chain is honoured,
            as validated by ``Settings.rate_limit_trusted_proxies``.

    Returns:
        str: The address the request's quota is counted against, or
            :data:`_UNKNOWN_CLIENT_KEY` when no address is available at all, so such
            requests share one budget rather than escaping metering.
    """
    socket_address = ""
    client = scope.get("client")
    if client:
        socket_address = client[0] or ""

    if trusted_proxies and _matches_any(socket_address, trusted_proxies):
        forwarded = _forwarded_for(scope)
        if forwarded:
            for entry in reversed(
                [part.strip() for part in forwarded.split(",") if part.strip()]
            ):
                if not _matches_any(entry, trusted_proxies):
                    return entry

    return socket_address or _UNKNOWN_CLIENT_KEY


def _matches_any(candidate: str, networks: Sequence[str]) -> bool:
    """Whether ``candidate`` is an address inside any of ``networks``.

    An unparseable candidate matches nothing, so a malformed forwarded entry is treated
    as untrusted rather than as a match.
    """
    try:
        address = ip_address(candidate)
    except ValueError:
        return False
    for network in networks:
        try:
            if address in ip_network(network, strict=False):
                return True
        except ValueError:
            # Settings validation rejects an unparseable network, so this guards only a
            # caller that assembled the list itself.
            continue
    return False


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
        trusted_proxies: Sequence[str],
    ) -> None:
        super().__init__(app)
        self._write_limits = write_limits
        self._limiter = FixedWindowRateLimiter(storage)
        self._trusted_proxies = tuple(trusted_proxies)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # SECURITY: bounds mutating request volume per client - writes were unthrottled
        if request.method not in _WRITE_METHODS:
            return await call_next(request)

        client_key = resolve_client_key(request.scope, self._trusted_proxies)
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


def _declared_body_size(scope: Scope) -> Optional[int]:
    """Return the request's declared body size, or None if it declared none.

    A ``Content-Length`` that is not a non-negative integer is reported as None rather than
    trusted, so the streamed byte count is what bounds such a request.
    """
    for name, value in scope.get("headers") or ():
        if name.lower() == _CONTENT_LENGTH_HEADER:
            try:
                declared = int(value)
            except (TypeError, ValueError):
                return None
            return declared if declared >= 0 else None
    return None


class _RequestBodySizeLimitMiddleware:
    """Refuses a request body larger than :data:`_MAX_REQUEST_BODY_BYTES`.

    A declared ``Content-Length`` over the maximum is refused before the body is read at
    all. A body that declares no length, or understates it, is bounded while it streams:
    the byte count is accumulated as the application reads, and the connection is refused
    the moment the maximum is passed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # SECURITY: bounds the memory and database work one request can demand - the list
        # bodies the cell-update and share routes accept declare no maximum cardinality
        declared = _declared_body_size(scope)
        if declared is not None and declared > _MAX_REQUEST_BODY_BYTES:
            await self._rejection()(scope, receive, send)
            return

        # ``over`` records that the body passed the maximum, ``started`` that a response has
        # already gone to the client, and ``refused`` that the 413 has been sent.
        state: Dict[str, Any] = {
            "bytes": 0,
            "over": False,
            "started": False,
            "refused": False,
        }

        async def counted_receive() -> Dict[str, Any]:
            message = await receive()
            if message["type"] == "http.request":
                state["bytes"] += len(message.get("body", b"") or b"")
                if state["bytes"] > _MAX_REQUEST_BODY_BYTES:
                    state["over"] = True
                    # Reported to the application as a disconnect, so it stops reading
                    # instead of assembling a body already known to be over the maximum.
                    return {"type": "http.disconnect"}
            return message

        async def refuse() -> None:
            state["refused"] = True
            await self._rejection()(scope, receive, send)

        async def guarded_send(message: Dict[str, Any]) -> None:
            if state["over"] and not state["started"]:
                # The application answered a body it never fully received - a parse error,
                # for instance. That answer would hide the reason, so the refusal replaces
                # it and everything the application still sends is dropped.
                if not state["refused"]:
                    await refuse()
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            # The reported disconnect surfaces inside the application as a client
            # disconnection, which it is entitled to raise. When that is what happened, the
            # refusal is the honest answer rather than a propagated error.
            if state["over"] and not state["started"] and not state["refused"]:
                await refuse()
                return
            raise
        if state["over"] and not state["started"] and not state["refused"]:
            await refuse()

    def _rejection(self) -> Response:
        """Return the 413 for a body over the maximum."""
        return JSONResponse(
            status_code=_PAYLOAD_TOO_LARGE_STATUS,
            content={
                "error": (
                    "Request body exceeds the maximum of "
                    "{0} bytes".format(_MAX_REQUEST_BODY_BYTES)
                )
            },
        )


def register_rate_limiting(app: FastAPI) -> None:
    """Install both throttling tiers on ``app``.

    Records the resulting state on ``app.state.rate_limit_enabled`` either way, so an
    operator can tell an enforcing deployment from an unthrottled one.

    When ``rate_limit_enabled`` is false a warning naming the consequence is logged and
    nothing else is registered: no middleware, no exception handler and no
    ``app.state.limiter``. Both window expressions are parsed here, so an unusable value
    fails at registration rather than at request time, and the window store is resolved
    once here so both tiers count in the same place.
    """
    settings = get_settings()
    app.state.rate_limit_enabled = bool(settings.rate_limit_enabled)
    if not settings.rate_limit_enabled:
        # SECURITY: a deployment serving with no throttling at all announces itself - it
        # was previously indistinguishable from an enforcing one
        logger.warning(
            "Request throttling is DISABLED by configuration: no per-client ceiling and "
            "no write budget are installed, so request volume on every route is "
            "unbounded. Set rate_limit_enabled true to restore throttling."
        )
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
    trusted_proxies = tuple(settings.rate_limit_trusted_proxies)

    # SECURITY: bounds the size of a single request body - request-count throttling does
    # not limit the work one request can demand. Registered before the throttling tiers, so
    # it sits inside them and a rejection still carries the CORS and security headers.
    app.add_middleware(_RequestBodySizeLimitMiddleware)

    app.add_middleware(
        _WriteMethodRateLimitMiddleware,
        write_limits=write_limits,
        storage=storage,
        trusted_proxies=trusted_proxies,
    )

    # SECURITY: bounds total request volume per client - brute force, credential
    # stuffing and scraping were unthrottled on every route
    # SECURITY: a forwarded client address is honoured only when the request arrived from
    # a configured trusted proxy - the chain was otherwise trusted on its own contents, so
    # a caller reaching the API directly could forge and rotate its quota identity
    def _client_key(request: Request) -> str:
        return resolve_client_key(request.scope, trusted_proxies)

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
