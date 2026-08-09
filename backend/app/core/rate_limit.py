"""Two-tier per-client request throttling for the Excel Clone API.

Tier one applies an application-wide ceiling to every request. Tier two applies a
tighter budget to mutating HTTP methods. Both tiers count in the same configured
window-store backend, each holding its own client of it. A client's quota is therefore
the configured quota across every worker and pod only while that backend is shared;
the ``memory://`` store - configured explicitly, or substituted when the intended
store is unusable - counts per process, so each worker then enforces its own copy of
the quota. Both tiers are installed as middleware by :func:`register_rate_limiting`,
so no route handler requires a ``request`` parameter or a decorator.

Alongside the two tiers, :func:`register_rate_limiting` installs two bounds on the work a
request may demand rather than on how many requests there are: a maximum request body size, and
a maximum number of requests IN FLIGHT. The second is not a throttling tier and is not
interchangeable with one - a burst of 120 requests is well inside a 600-per-minute ceiling, and
before the bound existed such a burst exhausted the database connection pool and 47 per cent of
it was answered HTTP 500 after a thirty-second wait. See
:class:`_ConcurrencyLimitMiddleware`.

Both tiers count against a FIXED bucket name - :data:`_ALL_REQUESTS_SCOPE` and
:data:`_WRITE_REQUESTS_SCOPE` - rather than against anything derived from the request.
The ceiling is therefore one budget per client across the whole application: every route,
every workbook path, and every URL that matches no route at all draws on the same
allowance. Being pure ASGI, the ceiling also runs before routing, so a request to an
unmatched path is counted rather than exempted.

"Every request" includes a CORS preflight, and that depends on a registration order
``backend/app/main.py`` owns: ``CORSMiddleware`` answers a preflight itself without ever
calling the application inside it, so the tiers see a preflight only while they wrap CORS
rather than the other way round. A browser sends one preflight per API call that carries an
``Authorization`` header, because that header is not CORS-safelisted, so a preflight surface
outside the tiers is roughly half of a browser client's traffic and is metered for that
reason. What a throttled preflight cannot do is report itself: a preflight answered with any
non-2xx status is a CORS failure by specification, so a browser surfaces an exhausted
preflight as a failed request whatever headers accompany it.

Because the tiers wrap CORS, a response one of them produces itself never passes through
``CORSMiddleware``, so :func:`_cross_origin_headers` re-states the small subset of the
cross-origin headers a caller needs in order to read the refusal at all. The allow-list it
matches against is read ONCE, when the middleware is registered, and never per request: a
rejection is the cheapest thing this module does and the configuration read is not, so
consulting settings there would let a client that is already over its quota drive three
Secret Manager reads per refused request.

Clients are identified by :func:`resolve_client_key`, which meters the address the server
reports as the connection peer, and only while that address is the transport peer. A
deployment whose server rewrites the peer from a forwarded header is metered as one client,
because an address taken from a header is chosen by whoever sent it.

A window store that stops answering does not remove throttling: the tier switches, once
and with a warning, to a process-local counter and keeps enforcing the same window. The
quota becomes per worker rather than shared, which is a weaker guarantee than the
configured one but is still a guarantee.

:func:`register_rate_limiting` also installs the request-body ceiling
(:class:`_RequestBodySizeLimitMiddleware`). That is a separate control and
``rate_limit_enabled`` does not govern it: the tiers bound how many requests a client may
make, the ceiling bounds how much work one request may demand, and disabling the former
does not withdraw the latter.
"""

import asyncio
import ipaddress
import json
import logging
import math
import threading
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from fastapi import FastAPI
from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage, storage_from_string
from limits.storage.base import Storage
from limits.strategies import FixedWindowRateLimiter
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.core.config import get_settings
from backend.app.db.database import DB_POOL_CAPACITY

logger = logging.getLogger(__name__)

# HTTP methods metered by the write tier. Every other method bypasses that tier.
_WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_TOO_MANY_REQUESTS_STATUS: int = 429

#: Header advising a refused client when the window it exhausted resets. Public, because
#: ``backend/app/main.py`` names it in the CORS ``expose_headers`` list: it is not a
#: CORS-safelisted response header, so a cross-origin caller cannot read it unless it is
#: exposed, and the client seam reads it to tell the user when to retry. One name, so the
#: header emitted and the header exposed cannot drift apart.
RETRY_AFTER_HEADER: str = "Retry-After"

# Retained under its private name so existing references keep working.
_RETRY_AFTER_HEADER: str = RETRY_AFTER_HEADER

# Floor, in seconds, for the Retry-After value of a rejection.
_MIN_RETRY_AFTER_SECONDS: int = 1

# Bucket identifier the ceiling tier counts every request against. Fixed, so the ceiling
# is one application-wide allowance per client rather than one allowance per URL.
_ALL_REQUESTS_SCOPE: str = "all-requests"

# Bucket identifier keeping the write tier's counters independent of the ceiling tier.
_WRITE_REQUESTS_SCOPE: str = "write-requests"

# Header declaring the request body size.
_CONTENT_LENGTH_HEADER: bytes = b"content-length"

_PAYLOAD_TOO_LARGE_STATUS: int = 413

_SERVICE_UNAVAILABLE_STATUS: int = 503

# Requests allowed to be in flight at once, per worker process. Taken from the database pool's
# capacity because that is the resource that runs out first: every request to a protected route
# needs a pooled connection, and one request in flight needs one connection at a time - the
# identity lookup closes its own Session before the handler's acquires anything. Equal values
# therefore mean connection demand can never exceed the pool, so the pool cannot be exhausted
# and no request can wait on it.
#
# The number is NOT derived from the request thread pool, and the difference is the whole point.
# get_db is a synchronous generator dependency, so its Session - and once it has queried, its
# pooled connection - is held across awaits at which the worker thread is released and later
# re-acquired. A request can therefore hold a connection while holding no thread, which is what
# runtime verification observed directly: at a concurrency of 120 against 40 worker threads,
# pg_stat_activity showed all 15 pooled connections idle INSIDE an open transaction with only
# one or two executing anything. Raising the pool to match the thread pool would have moved
# that cliff rather than removing it; bounding the requests removes it.
_MAX_CONCURRENT_REQUESTS: int = DB_POOL_CAPACITY

# Seconds a request waits for a concurrency slot before it is shed. Shorter than
# database.DB_POOL_TIMEOUT_SECONDS on purpose: a queue this bound has already absorbed is
# draining at the rate the database can serve, so a wait longer than this means the arrival
# rate exceeds the service rate and the honest answer is to say so rather than to hold the
# caller until something else times out.
_CONCURRENCY_WAIT_SECONDS: float = 5.0

# Retry-After for a shed request. The queue drains in the time the requests ahead of it take,
# which is far shorter than a throttling window, so advising a full window would be wrong.
_CONCURRENCY_RETRY_AFTER_SECONDS: int = 1

# Largest request body the API accepts. The cell-update and share routes accept list
# bodies that declare no maximum cardinality, so request-count throttling alone does not
# bound the memory and database work a single authenticated request can demand.
# At roughly 60 bytes of JSON per cell, 10 MiB admits on the order of 170,000 cells in one
# request and refuses anything larger.
_MAX_REQUEST_BODY_BYTES: int = 10 * 1024 * 1024

# Window store used when no shared store is configured or the configured one is
# unreachable. Counts in process memory, so quotas are per worker.
_IN_PROCESS_STORAGE_URI: str = "memory://"

# Client key used when no usable peer address is available, so such requests share one
# budget rather than escaping metering.
_UNKNOWN_CLIENT_KEY: str = "unknown"

# Request header naming the origin a cross-origin caller is on.
_ORIGIN_HEADER: bytes = b"origin"

# The cross-origin response headers a refusal produced inside these tiers re-states for
# itself. CORSMiddleware is registered inside the tiers, so it never sees such a response;
# without these a browser reports a 429, 413 or 503 as an opaque cross-origin failure and
# the caller cannot tell a throttle from an outage. Deliberately only these three: the
# preflight headers describe what a future request may do, which a refusal does not answer.
_CORS_ALLOW_ORIGIN_HEADER: str = "Access-Control-Allow-Origin"
_CORS_ALLOW_CREDENTIALS_HEADER: str = "Access-Control-Allow-Credentials"
_CORS_ALLOW_CREDENTIALS_VALUE: str = "true"
_CORS_EXPOSE_HEADERS_HEADER: str = "Access-Control-Expose-Headers"
_VARY_HEADER: str = "Vary"
_VARY_ORIGIN_VALUE: str = "Origin"

# Request headers that name an address other than the transport peer. A server layer
# configured to trust one of these replaces the peer in the connection scope with the
# address the header names, before any application middleware runs.
_FORWARDED_ADDRESS_HEADERS: FrozenSet[bytes] = frozenset(
    {b"x-forwarded-for", b"x-real-ip", b"forwarded"}
)

# Header the address is taken from when a server layer rewrites the peer.
_FORWARDED_FOR_HEADER: bytes = b"x-forwarded-for"

# Port a rewritten peer carries: a forwarded chain records addresses without ports, so the
# rewriting layer has none to supply. An accepted TCP connection always has a non-zero peer
# port, which is what makes this distinguishable.
_REWRITTEN_CLIENT_PORT: int = 0

# Client key every request whose reported peer may have come from a header counts against.
# Fixed, so rotating that header moves a caller between no buckets at all.
_FORWARDED_CLIENT_KEY: str = "forwarded-peer"

# Records the forwarded-peer topology once per process rather than once per request.
_forwarded_peer_lock: threading.Lock = threading.Lock()
_forwarded_peer_reported: bool = False


def resolve_storage(redis_uri: str) -> Tuple[Storage, str]:
    """Return the window store both tiers count in, and the URI it was built from.

    The store is derived from ``REDIS_URL``, the broker the deployment already runs, so
    every worker and pod counts in one place without a second configuration value to keep
    in step. A store that cannot be constructed or does not answer is replaced by
    in-process counting and the substitution is logged, because a counter store that is
    unreachable must degrade the control rather than refuse every request and take the API
    down with it.

    Returns:
        The storage instance and the URI actually in use, which is ``memory://`` whenever
        the intended store is unusable. :func:`register_rate_limiting` passes that URI to
        the ceiling tier, so both tiers address the same backend.
    """
    intended = (redis_uri or "").strip() or _IN_PROCESS_STORAGE_URI
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


def _forwarded_addresses(scope: Scope) -> Optional[List[str]]:
    """Return the addresses the request's forwarded headers name, or ``None`` if it has none.

    An empty list means such a header was sent but named nothing, which still tells us the
    request travelled with one.
    """
    addresses: Optional[List[str]] = None
    for name, value in scope.get("headers") or ():
        lowered = name.lower()
        if lowered not in _FORWARDED_ADDRESS_HEADERS:
            continue
        if addresses is None:
            addresses = []
        if lowered != _FORWARDED_FOR_HEADER:
            continue
        try:
            decoded = value.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):
            continue
        addresses.extend(
            entry.strip() for entry in decoded.split(",") if entry.strip()
        )
    return addresses


def _peer_came_from_a_header(
    host: Optional[str], port: Optional[int], forwarded: List[str]
) -> bool:
    """Whether the reported peer looks like it was taken from a forwarded header.

    Two independent signals, either of which is enough. A reported port of
    :data:`_REWRITTEN_CLIENT_PORT` cannot belong to an accepted connection. A reported host
    that also appears in the forwarded chain is either that chain's work or a caller naming
    its own address, and both are treated the same way.
    """
    if port == _REWRITTEN_CLIENT_PORT:
        return True
    return bool(host) and host in forwarded


def _report_forwarded_peer_once() -> None:
    """Record the forwarded-peer topology the first time a request exhibits it."""
    global _forwarded_peer_reported
    if _forwarded_peer_reported:
        return
    with _forwarded_peer_lock:
        if _forwarded_peer_reported:
            return
        # SECURITY: an address a request can name is not an identity - rotating one escaped
        # the quota entirely, so every such request is metered as one client
        logger.warning(
            "The server is reporting request peers taken from a forwarded header, so an "
            "individual client cannot be identified: every such request is counted against "
            "one shared budget. Serve with the transport peer preserved - uvicorn's "
            "--no-proxy-headers, or trusting only the address of the proxy in front of it - "
            "to meter each client separately."
        )
        _forwarded_peer_reported = True


def resolve_client_key(scope: Scope) -> str:
    """Return the throttling identity for the request described by ``scope``.

    The identity is the transport peer of the connection. A peer the server has replaced
    with an address from a forwarded header is not one: the value is chosen by whoever sent
    the header, so it is metered against the single fixed :data:`_FORWARDED_CLIENT_KEY`
    bucket and rotating the header moves a caller between no buckets at all. This is the
    documented behaviour of a deployment behind a proxy - every client behind it draws on
    one budget - and it is applied whether or not the operator intended the rewrite.

    A peer that is the transport peer must still be an IP address to become a bucket name.
    A rewriting layer copies the header's text through unexamined, so anything else -
    arbitrary text, an over-long value, markup, a path - is metered against
    :data:`_UNKNOWN_CLIENT_KEY` rather than becoming a bucket of its own.

    Args:
        scope: The ASGI connection scope of the request being metered.

    Returns:
        str: The bucket the request's quota is counted against. Always one of a normalised
            IP address, :data:`_FORWARDED_CLIENT_KEY` or :data:`_UNKNOWN_CLIENT_KEY`, so
            the set of buckets a caller can create is bounded and no request escapes
            metering.
    """
    client = scope.get("client")
    host = client[0] if client else None
    port = client[1] if client and len(client) > 1 else None

    forwarded = _forwarded_addresses(scope)
    if forwarded is not None and _peer_came_from_a_header(host, port, forwarded):
        _report_forwarded_peer_once()
        return _FORWARDED_CLIENT_KEY

    if not host:
        return _UNKNOWN_CLIENT_KEY
    try:
        # SECURITY: bounds the buckets one caller can create - an unvalidated address became
        # a bucket name, so arbitrary header text filled the window store
        return str(ipaddress.ip_address(host))
    except ValueError:
        return _UNKNOWN_CLIENT_KEY


def _request_origin(scope: Scope) -> str:
    """Return the request's ``Origin`` header value, or an empty string if it carries none.

    A value that is not decodable as a header is reported as absent rather than guessed at,
    because the only use of it is an exact comparison against the configured allow-list.

    Args:
        scope: The ASGI connection scope of the request being answered.

    Returns:
        str: The origin the caller declared, or ``""``.
    """
    for name, value in scope.get("headers") or ():
        if name.lower() == _ORIGIN_HEADER:
            try:
                return value.decode("latin-1")
            except (AttributeError, UnicodeDecodeError):
                return ""
    return ""


def _cross_origin_headers(scope: Scope, allowed_origins: FrozenSet[str]) -> Dict[str, str]:
    """Return the cross-origin headers a refusal produced inside these tiers must carry.

    ``CORSMiddleware`` is registered INSIDE the throttling tiers, which is what lets the
    ceiling meter a CORS preflight, and the cost of that order is that a response a tier
    produces itself never travels back out through CORS. This restates the subset a browser
    needs to hand the refusal to the page's own code.

    The origin is echoed only when it appears in ``allowed_origins`` EXACTLY as configured,
    which is the same comparison ``CORSMiddleware`` makes against the same
    ``ALLOWED_ORIGINS`` list. ``Vary: Origin`` is set whenever the request carried an origin
    at all, including a refused one, so a shared cache cannot serve one origin's answer to
    another. :data:`RETRY_AFTER_HEADER` is exposed for an admitted origin, matching the
    ``expose_headers`` list ``configure_cors`` declares, because a cross-origin caller cannot
    otherwise read the one header that tells it when to try again.

    Args:
        scope: The ASGI connection scope of the request being refused.
        allowed_origins: The configured origins, captured when the middleware was
            registered rather than read per request.

    Returns:
        Dict[str, str]: The headers to add to the refusal, empty for a same-origin request.
    """
    origin = _request_origin(scope)
    if not origin:
        return {}
    headers = {_VARY_HEADER: _VARY_ORIGIN_VALUE}
    if origin in allowed_origins:
        # SECURITY: the origin is echoed only on an exact match against the configured
        # allow-list, never reflected unconditionally, so a refusal cannot become the one
        # response that admits an origin the CORS policy refuses.
        headers[_CORS_ALLOW_ORIGIN_HEADER] = origin
        headers[_CORS_ALLOW_CREDENTIALS_HEADER] = _CORS_ALLOW_CREDENTIALS_VALUE
        headers[_CORS_EXPOSE_HEADERS_HEADER] = RETRY_AFTER_HEADER
    return headers


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


class _FixedWindowTier:
    """One throttling tier: a set of windows counted against one fixed bucket name.

    The bucket name is a constant, never anything read from the request, so every request
    a tier meters draws on the same allowance per client. That is what makes the ceiling
    application-wide rather than per URL.

    Owns the degradation policy for the tier. If the shared store raises - it became
    unreachable, or the broker refused the command - the tier substitutes a process-local
    counter ONCE, records the substitution with its exception context, and keeps
    enforcing the same windows against that counter. Enforcement is never dropped: a
    request is refused or admitted on a count, never admitted because counting failed.
    """

    def __init__(self, scope_name: str, windows: List[RateLimitItem], storage: Storage) -> None:
        self._scope_name = scope_name
        self._windows = windows
        self._limiter = FixedWindowRateLimiter(storage)
        self._degraded = False
        # Serialises the one-time substitution below. Requests are served from a thread pool,
        # so without it two threads meeting the same store failure each built their own
        # replacement counter and the last assignment won, discarding whatever the other had
        # already counted into its copy.
        self._degrade_lock = threading.Lock()

    @property
    def degraded(self) -> bool:
        """Whether this tier has fallen back to counting in process memory."""
        return self._degraded

    def exhausted_window(self, client_key: str) -> Optional[RateLimitItem]:
        """Count one request and return the window it exhausted, or ``None``.

        Args:
            client_key: The identity the request is metered against.

        Returns:
            The first window ``client_key`` has now exceeded, or ``None`` when the
            request is within every window.
        """
        for window in self._windows:
            if not self._hit(window, client_key):
                return window
        return None

    def retry_after_seconds(self, window: RateLimitItem, client_key: str) -> int:
        """Return whole seconds until ``window`` resets for ``client_key``, at least one.

        A store that cannot report its window falls back to the window's own length,
        which is never shorter than the time actually remaining.
        """
        try:
            reset_time = self._limiter.get_window_stats(
                window, self._scope_name, client_key
            ).reset_time
            remaining = int(math.ceil(reset_time - time.time()))
        except Exception:
            logger.warning(
                "Throttling tier %s could not read its window; advising the client to "
                "retry after the whole window instead",
                self._scope_name,
                exc_info=True,
            )
            remaining = window.GRANULARITY.seconds * window.multiples
        return max(_MIN_RETRY_AFTER_SECONDS, remaining)

    def _hit(self, window: RateLimitItem, client_key: str) -> bool:
        """Record one request against ``window``, degrading the store if it fails.

        The substitution happens at most once per tier: the flag is re-tested under the lock,
        so concurrent requests that all meet the same store failure share one replacement
        counter rather than each installing its own.
        """
        try:
            return self._limiter.hit(window, self._scope_name, client_key)
        except Exception:
            if self._degraded:
                # Already counting in process memory, so a failure here is not a store
                # outage and there is nothing further to fall back to.
                raise
            with self._degrade_lock:
                if not self._degraded:
                    # SECURITY: a window store that stops answering degrades the guarantee, it
                    # does not remove the control - the request was previously admitted unmetered
                    logger.warning(
                        "Throttling tier %s could not consult its shared window store; counting "
                        "in process memory from now on, so each worker enforces its own copy of "
                        "the quota until the store answers again and the process is restarted",
                        self._scope_name,
                        exc_info=True,
                    )
                    self._limiter = FixedWindowRateLimiter(MemoryStorage())
                    self._degraded = True
            return self._limiter.hit(window, self._scope_name, client_key)


class _RateLimitMiddleware:
    """Refuses a request whose client has exhausted the ceiling or the write budget.

    Pure ASGI, so it runs before routing: a request to a path that matches no route is
    counted rather than exempted, which is what stops path scanning from being free.

    The ceiling tier meters every request and is consulted first, so a client over the
    ceiling is refused without also spending its write budget. The write tier meters
    ``POST``, ``PUT``, ``PATCH`` and ``DELETE`` only, and is consulted just for those
    methods once the ceiling has admitted the request. A single request therefore
    consumes at most one unit of each budget.

    Registration note: ``register_rate_limiting`` installs this OUTSIDE ``CORSMiddleware``,
    which is what lets the ceiling meter a CORS preflight - CORS answers a preflight itself
    and never calls the application inside it. The 429 therefore does not travel out through
    CORS, so it carries the cross-origin headers :func:`_cross_origin_headers` supplies.
    """

    def __init__(
        self,
        app: ASGIApp,
        ceiling: _FixedWindowTier,
        write: _FixedWindowTier,
        allowed_origins: FrozenSet[str] = frozenset(),
    ) -> None:
        self.app = app
        self._ceiling = ceiling
        self._write = write
        self._allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_key = resolve_client_key(scope)

        # SECURITY: bounds total request volume per client - brute force, credential
        # stuffing and scraping were unthrottled on every route, and the ceiling is
        # counted against one fixed bucket so it cannot be spread across URLs
        exhausted = self._ceiling.exhausted_window(client_key)
        tier = self._ceiling
        if exhausted is None and scope.get("method", "").upper() in _WRITE_METHODS:
            # SECURITY: bounds mutating request volume per client - writes were unthrottled
            exhausted = self._write.exhausted_window(client_key)
            tier = self._write

        if exhausted is not None:
            await self._rejection(scope, tier, exhausted, client_key)(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _rejection(
        self,
        scope: Scope,
        tier: _FixedWindowTier,
        window: RateLimitItem,
        client_key: str,
    ) -> "_JsonRejection":
        """Return the 429 for an exhausted ``window``, carrying Retry-After."""
        headers = {
            _RETRY_AFTER_HEADER: str(tier.retry_after_seconds(window, client_key))
        }
        headers.update(_cross_origin_headers(scope, self._allowed_origins))
        return _JsonRejection(
            status_code=_TOO_MANY_REQUESTS_STATUS,
            body={"error": "Rate limit exceeded: {0}".format(window)},
            headers=headers,
        )


class _JsonRejection:
    """A minimal JSON ASGI response, so no Starlette response object is needed here."""

    def __init__(self, status_code: int, body: Dict[str, Any], headers: Dict[str, str]) -> None:
        self._status_code = status_code
        self._body = json.dumps(body).encode("utf-8")
        self._headers = [(b"content-type", b"application/json")]
        self._headers.append((b"content-length", str(len(self._body)).encode("latin-1")))
        for name, value in headers.items():
            self._headers.append(
                (name.lower().encode("latin-1"), value.encode("latin-1"))
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        start: Message = {
            "type": "http.response.start",
            "status": self._status_code,
            "headers": self._headers,
        }
        await send(start)
        await send({"type": "http.response.body", "body": self._body})


class _ConcurrencyLimitMiddleware:
    """Bounds the number of requests in flight, and sheds the excess with a 503.

    Throttling bounds how MANY requests a client may send in a window. It does not bound how
    many are being served at the same instant, and those are different limits: a burst of 120
    requests is comfortably inside a 600-per-minute ceiling and passes both tiers untouched,
    and then every one of them competes for a pooled database connection at once.

    What that produced before this existed, measured rather than reasoned about: 600 requests at
    a concurrency of 120 returned 284 HTTP 500s - 47 per cent - each after waiting the pool's
    full 30-second timeout, with 322 ``QueuePool limit of size 5 overflow 10 reached`` records in
    the log. The caller learned nothing from that: a 500 after 30 seconds is indistinguishable
    from a broken server.

    With in-flight requests bounded to :data:`_MAX_CONCURRENT_REQUESTS`, which equals the pool's
    capacity, connection demand cannot exceed the pool at all. A request that cannot get a slot
    within :data:`_CONCURRENCY_WAIT_SECONDS` is answered 503 with ``Retry-After`` - a shed
    request, reported as one, quickly.

    Registered INSIDE both throttling tiers, so a client already over its quota is refused
    without occupying a slot, and outside everything that touches the database.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_concurrent_requests: int = _MAX_CONCURRENT_REQUESTS,
        wait_seconds: float = _CONCURRENCY_WAIT_SECONDS,
        allowed_origins: FrozenSet[str] = frozenset(),
    ) -> None:
        self.app = app
        self._max_concurrent_requests = max_concurrent_requests
        self._wait_seconds = wait_seconds
        self._allowed_origins = allowed_origins
        # Created on first use rather than here. A Semaphore binds to the running event loop,
        # and this middleware is constructed while the application is being built - before any
        # loop exists, and in a process that may later serve on a different one.
        self._slots: Optional[asyncio.Semaphore] = None
        self._slots_lock = threading.Lock()

    @property
    def max_concurrent_requests(self) -> int:
        """The number of requests this middleware allows in flight at once."""
        return self._max_concurrent_requests

    def _semaphore(self) -> asyncio.Semaphore:
        """Return the slot semaphore, creating it on first use under a lock."""
        if self._slots is None:
            with self._slots_lock:
                if self._slots is None:
                    self._slots = asyncio.Semaphore(self._max_concurrent_requests)
        return self._slots

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        slots = self._semaphore()
        try:
            # SECURITY: bounds the number of requests competing for a pooled database
            # connection - request-count throttling bounds arrival rate, not concurrency, so a
            # burst inside the ceiling previously exhausted the pool and was answered 500
            await asyncio.wait_for(slots.acquire(), timeout=self._wait_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "Shed a request after waiting %.1fs for one of %d concurrency slots; the "
                "arrival rate is above what the database pool can serve",
                self._wait_seconds,
                self._max_concurrent_requests,
            )
            await self._rejection(scope)(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            slots.release()

    def _rejection(self, scope: Scope) -> "_JsonRejection":
        """Return the 503 for a shed request, carrying Retry-After."""
        headers = {_RETRY_AFTER_HEADER: str(_CONCURRENCY_RETRY_AFTER_SECONDS)}
        headers.update(_cross_origin_headers(scope, self._allowed_origins))
        return _JsonRejection(
            status_code=_SERVICE_UNAVAILABLE_STATUS,
            body={
                "error": (
                    "Server is at capacity: {0} requests may be served at once".format(
                        self._max_concurrent_requests
                    )
                )
            },
            headers=headers,
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
    """Refuses a request body larger than the configured maximum.

    The maximum is supplied when the middleware is constructed and defaults to
    :data:`_MAX_REQUEST_BODY_BYTES`, so there is one named value to change and a caller
    composing its own stack can narrow it.

    A declared ``Content-Length`` over the maximum is refused before the body is read at
    all. A body that declares no length, or understates it, is bounded while it streams:
    the byte count is accumulated as the application reads, and the connection is refused
    the moment the maximum is passed.

    Registration note: :func:`register_rate_limiting` installs this middleware OUTSIDE
    ``CORSMiddleware`` and ``AuthEnforcementBypassMarkerMiddleware``, so a 413 from here carries
    the security headers and the cross-origin headers :func:`_cross_origin_headers` supplies,
    but NOT the ``X-Auth-Enforcement-Bypassed`` marker. That is deliberate and not a gap in the
    marker's coverage: the marker records that an authentication decision was taken with
    verification relaxed, and a 413 is refused before any authentication decision is reached, so
    there is no bypass to report on it.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int,
        allowed_origins: FrozenSet[str] = frozenset(),
    ) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes
        self._allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # SECURITY: bounds the memory and database work one request can demand - the list
        # bodies the cell-update and share routes accept declare no maximum cardinality
        declared = _declared_body_size(scope)
        if declared is not None and declared > self._max_body_bytes:
            await self._rejection(scope)(scope, receive, send)
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
                if state["bytes"] > self._max_body_bytes:
                    state["over"] = True
                    # Reported to the application as a disconnect, so it stops reading
                    # instead of assembling a body already known to be over the maximum.
                    return {"type": "http.disconnect"}
            return message

        async def refuse() -> None:
            state["refused"] = True
            await self._rejection(scope)(scope, receive, send)

        async def guarded_send(message: Dict[str, Any]) -> None:
            if state["over"] and not state["started"]:
                # The application answered a body it never fully received - a parse error,
                # for instance. The refusal replaces that answer, and everything the
                # application still sends is dropped, so the reported status names the cause.
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
            # disconnection, which it may raise. When the body was over the maximum, the
            # refusal is sent instead of propagating that error.
            if state["over"] and not state["started"] and not state["refused"]:
                await refuse()
                return
            raise
        if state["over"] and not state["started"] and not state["refused"]:
            await refuse()

    def _rejection(self, scope: Scope) -> "_JsonRejection":
        """Return the 413 for a body over the maximum."""
        return _JsonRejection(
            status_code=_PAYLOAD_TOO_LARGE_STATUS,
            body={
                "error": (
                    "Request body exceeds the maximum of "
                    "{0} bytes".format(self._max_body_bytes)
                )
            },
            headers=_cross_origin_headers(scope, self._allowed_origins),
        )


def register_rate_limiting(app: FastAPI) -> None:
    """Install the request-body ceiling and, unless disabled, both throttling tiers.

    Records the resulting throttling state on ``app.state.rate_limit_enabled`` either
    way, so an operator can tell an enforcing deployment from an unthrottled one, and
    publishes the two tiers on ``app.state.rate_limit_tiers`` so their degradation state
    is readable.

    The two RESOURCE bounds - the request-concurrency bound and the request-body ceiling -
    are registered FIRST and unconditionally. They are distinct controls from throttling:
    the tiers bound how many requests a client may make, where these bound how much work one
    request may demand and how many may be in flight at once. ``rate_limit_enabled`` names
    the tiers, so it does not govern either bound, and withdrawing them with the tiers would
    be exactly backwards - a deployment that has turned throttling off is *more* exposed to
    the burst the pool bound exists to survive, not less. Registering both before the tiers
    also keeps them inside them, so a 413 or a 503 still travels back out through the CORS
    and security-header wrappers.

    When ``rate_limit_enabled`` is false a warning naming the consequence is logged and
    no tier is registered. Both window expressions are parsed only on that path, so an
    unusable value fails at registration rather than at request time, and the window
    store is resolved once so both tiers count in the same place.
    """
    settings = get_settings()

    # Captured once here, never read per request: a refusal is the cheapest response this
    # module produces, and get_settings() is not - it performs three Secret Manager reads on
    # every call - so reading the allow-list inside a rejection would let a client already
    # over its quota drive that cost with each further refused request. Read before the
    # throttling switch below, because both resource bounds are installed ahead of it.
    allowed_origins = frozenset(settings.ALLOWED_ORIGINS)

    # SECURITY: bounds the number of requests in flight to the database pool's capacity -
    # request-count throttling bounds arrival rate and not concurrency, so a burst inside the
    # ceiling exhausted the pool and was answered 500 after the pool's full timeout.
    # CONTRACT: registered first, so it sits INSIDE both throttling tiers and a client already
    # over its quota is refused without occupying a slot. Registered whatever
    # rate_limit_enabled is set to: that switch names the request-count tiers, and a
    # deployment with throttling off is more exposed to the burst this bound exists to
    # survive, not less.
    app.add_middleware(
        _ConcurrencyLimitMiddleware,
        max_concurrent_requests=_MAX_CONCURRENT_REQUESTS,
        wait_seconds=_CONCURRENCY_WAIT_SECONDS,
        allowed_origins=allowed_origins,
    )

    # SECURITY: bounds the size of a single request body - request-count throttling does
    # not limit the work one request can demand.
    # CONTRACT: registered before the throttling tiers, so it sits inside them; it sits
    # outside CORSMiddleware, so its 413 carries the security headers and the cross-origin
    # headers _cross_origin_headers supplies rather than CORS's own. Registered whatever
    # rate_limit_enabled is set to: the throttling switch governs the two request-count tiers
    # only, and a switch that also removed this ceiling would silently withdraw a control it
    # does not name.
    app.add_middleware(
        _RequestBodySizeLimitMiddleware,
        max_body_bytes=_MAX_REQUEST_BODY_BYTES,
        allowed_origins=allowed_origins,
    )

    app.state.rate_limit_enabled = bool(settings.rate_limit_enabled)
    if not settings.rate_limit_enabled:
        # SECURITY: a deployment serving with no throttling at all announces itself at
        # warning level, so it is distinguishable from an enforcing one.
        logger.warning(
            "Request throttling is DISABLED by configuration: no per-client ceiling and "
            "no write budget are installed, so request volume on every route is "
            "unbounded. Set rate_limit_enabled true to restore throttling. The %d-byte "
            "request-body ceiling and the %d-request concurrency bound are unaffected by "
            "this setting and remain in force.",
            _MAX_REQUEST_BODY_BYTES,
            _MAX_CONCURRENT_REQUESTS,
        )
        return

    write_limits = _parse_window_expression(
        settings.rate_limit_write, "rate_limit_write"
    )
    ceiling_limits = _parse_window_expression(
        settings.rate_limit_default, "rate_limit_default"
    )

    # SECURITY: both tiers count in one shared store - per-process counters let a
    # client's effective quota multiply by the number of workers and pods
    storage, storage_uri = resolve_storage(settings.REDIS_URL)
    logger.info(
        "Request throttling enabled: ceiling %s and write budget %s per client, counted "
        "in %s",
        settings.rate_limit_default,
        settings.rate_limit_write,
        storage_uri,
    )

    ceiling = _FixedWindowTier(_ALL_REQUESTS_SCOPE, ceiling_limits, storage)
    write = _FixedWindowTier(_WRITE_REQUESTS_SCOPE, write_limits, storage)
    app.state.rate_limit_tiers = {"ceiling": ceiling, "write": write}
    app.add_middleware(
        _RateLimitMiddleware,
        ceiling=ceiling,
        write=write,
        allowed_origins=allowed_origins,
    )
