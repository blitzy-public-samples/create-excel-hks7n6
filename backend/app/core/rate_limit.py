"""Two-tier per-client request throttling for the Excel Clone API.

Tier one applies an application-wide ceiling to every request. Tier two applies a
tighter budget to mutating HTTP methods. Both tiers count in the same configured
window-store backend, each holding its own client of it. A client's quota is therefore
the configured quota across every worker and pod only while that backend is shared;
the ``memory://`` store - configured explicitly, or substituted when the intended
store is unusable - counts per process, so each worker then enforces its own copy of
the quota. Both tiers are installed as middleware by :func:`register_rate_limiting`,
so no route handler requires a ``request`` parameter or a decorator.

Both tiers count against a FIXED bucket name - :data:`_ALL_REQUESTS_SCOPE` and
:data:`_WRITE_REQUESTS_SCOPE` - rather than against anything derived from the request.
The ceiling is therefore one budget per client across the whole application: every route,
every workbook path, and every URL that matches no route at all draws on the same
allowance. Being pure ASGI, the ceiling also runs before routing, so a request to an
unmatched path is counted rather than exempted.

Clients are identified by :func:`resolve_client_key`, which meters the socket peer
address and never a forwarded header.

A window store that stops answering does not remove throttling: the tier switches, once
and with a warning, to a process-local counter and keeps enforcing the same window. The
quota becomes per worker rather than shared, which is a weaker guarantee than the
configured one but is still a guarantee.
"""

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

logger = logging.getLogger(__name__)

# HTTP methods metered by the write tier. Every other method bypasses that tier.
_WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_TOO_MANY_REQUESTS_STATUS: int = 429

_RETRY_AFTER_HEADER: str = "Retry-After"

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

# Largest request body the API accepts. The cell-update and share routes accept list
# bodies that declare no maximum cardinality, so request-count throttling alone does not
# bound the memory and database work a single authenticated request can demand.
# At roughly 60 bytes of JSON per cell, 10 MiB admits on the order of 170,000 cells in one
# request and refuses anything larger.
_MAX_REQUEST_BODY_BYTES: int = 10 * 1024 * 1024

# Window store used when no shared store is configured or the configured one is
# unreachable. Counts in process memory, so quotas are per worker.
_IN_PROCESS_STORAGE_URI: str = "memory://"

# Client key used when neither a socket address nor a trusted forwarded address is
# available, so such requests share one budget rather than escaping metering.
_UNKNOWN_CLIENT_KEY: str = "unknown"


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


def resolve_client_key(scope: Scope) -> str:
    """Return the throttling identity for the request described by ``scope``.

    The identity is the socket peer address and nothing else. No forwarded header is
    consulted: a chain is trusted only on the strength of its own contents, so a caller
    reaching the API directly could name any address it liked and rotate through
    addresses to escape its quota. Metering the peer is unforgeable.

    Args:
        scope: The ASGI connection scope of the request being metered.

    Returns:
        str: The address the request's quota is counted against, or
            :data:`_UNKNOWN_CLIENT_KEY` when no address is available at all, so such
            requests share one budget rather than escaping metering.
    """
    client = scope.get("client")
    if client and client[0]:
        return client[0]
    return _UNKNOWN_CLIENT_KEY


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
    """

    def __init__(
        self,
        app: ASGIApp,
        ceiling: _FixedWindowTier,
        write: _FixedWindowTier,
    ) -> None:
        self.app = app
        self._ceiling = ceiling
        self._write = write

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
            await self._rejection(tier, exhausted, client_key)(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _rejection(
        tier: _FixedWindowTier, window: RateLimitItem, client_key: str
    ) -> "_JsonRejection":
        """Return the 429 for an exhausted ``window``, carrying Retry-After."""
        return _JsonRejection(
            status_code=_TOO_MANY_REQUESTS_STATUS,
            body={"error": "Rate limit exceeded: {0}".format(window)},
            headers={
                _RETRY_AFTER_HEADER: str(tier.retry_after_seconds(window, client_key))
            },
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

    Registration note: :func:`register_rate_limiting` installs this middleware INSIDE
    ``AuthEnforcementBypassMarkerMiddleware``, so a 413 from here carries the CORS and security
    headers but NOT the ``X-Auth-Enforcement-Bypassed`` marker. That is deliberate and not a gap
    in the marker's coverage: the marker records that an authentication decision was taken with
    verification relaxed, and a 413 is refused before any authentication decision is reached, so
    there is no bypass to report on it.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # SECURITY: bounds the memory and database work one request can demand - the list
        # bodies the cell-update and share routes accept declare no maximum cardinality
        declared = _declared_body_size(scope)
        if declared is not None and declared > self._max_body_bytes:
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
                if state["bytes"] > self._max_body_bytes:
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

    def _rejection(self) -> "_JsonRejection":
        """Return the 413 for a body over the maximum."""
        return _JsonRejection(
            status_code=_PAYLOAD_TOO_LARGE_STATUS,
            body={
                "error": (
                    "Request body exceeds the maximum of "
                    "{0} bytes".format(self._max_body_bytes)
                )
            },
            headers={},
        )


def register_rate_limiting(app: FastAPI) -> None:
    """Install both throttling tiers on ``app``.

    Records the resulting state on ``app.state.rate_limit_enabled`` either way, so an
    operator can tell an enforcing deployment from an unthrottled one, and publishes the
    two tiers on ``app.state.rate_limit_tiers`` so their degradation state is readable.

    When ``rate_limit_enabled`` is false a warning naming the consequence is logged and
    nothing else is registered: no middleware and no tiers. Both window expressions are
    parsed here, so an unusable value fails at registration rather than at request time,
    and the window store is resolved once here so both tiers count in the same place.
    """
    settings = get_settings()
    app.state.rate_limit_enabled = bool(settings.rate_limit_enabled)
    if not settings.rate_limit_enabled:
        # SECURITY: a deployment serving with no throttling at all announces itself at
        # warning level, so it is distinguishable from an enforcing one.
        logger.warning(
            "Request throttling is DISABLED by configuration: no per-client ceiling and "
            "no write budget are installed, so request volume on every route is "
            "unbounded. Set rate_limit_enabled true to restore throttling."
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

    # SECURITY: bounds the size of a single request body - request-count throttling does
    # not limit the work one request can demand.
    # CONTRACT: registered before the throttling tiers, so it sits inside them and a 413 still
    # carries the CORS and security headers.
    app.add_middleware(
        _RequestBodySizeLimitMiddleware, max_body_bytes=_MAX_REQUEST_BODY_BYTES
    )

    ceiling = _FixedWindowTier(_ALL_REQUESTS_SCOPE, ceiling_limits, storage)
    write = _FixedWindowTier(_WRITE_REQUESTS_SCOPE, write_limits, storage)
    app.state.rate_limit_tiers = {"ceiling": ceiling, "write": write}
    app.add_middleware(_RateLimitMiddleware, ceiling=ceiling, write=write)
