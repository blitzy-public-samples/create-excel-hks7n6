"""Two-tier per-client request throttling for the Excel Clone API.

Tier one applies an application-wide ceiling to every request. Tier two applies a
tighter budget to mutating HTTP methods. Both tiers identify clients by remote
address and are installed as middleware by :func:`register_rate_limiting`, so no
route handler requires a ``request`` parameter or a decorator.
"""

import math
import time
from typing import FrozenSet, List

from fastapi import FastAPI
from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from backend.app.core.config import get_settings

# HTTP methods metered by the write tier. Every other method bypasses that tier.
_WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_TOO_MANY_REQUESTS_STATUS: int = 429

_RETRY_AFTER_HEADER: str = "Retry-After"

# Floor, in seconds, for the Retry-After value of a rejection.
_MIN_RETRY_AFTER_SECONDS: int = 1

# Bucket identifier keeping the write tier's counters independent of the ceiling tier.
_WRITE_REQUESTS_SCOPE: str = "write-requests"


def _parse_window_expression(expression: str, field_name: str) -> List[RateLimitItem]:
    """Return the windows ``expression`` declares.

    Raises:
        ValueError: if the expression declares no parseable window, because a tier
            built from it would admit every request while appearing installed.
    """
    windows = parse_many(expression)
    if not windows:
        raise ValueError(
            f"{field_name} must be a rate expression such as '600/minute'; "
            f"rejected: {expression!r}"
        )
    return windows


class _WriteMethodRateLimitMiddleware(BaseHTTPMiddleware):
    """Meters POST, PUT, PATCH and DELETE against a fixed window per client.

    Requests using any other method are passed through without being counted.
    """

    def __init__(self, app, write_limits: List[RateLimitItem]) -> None:
        super().__init__(app)
        self._write_limits = write_limits
        self._limiter = FixedWindowRateLimiter(MemoryStorage())

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # SECURITY: bounds mutating request volume per client - writes were unthrottled
        if request.method not in _WRITE_METHODS:
            return await call_next(request)

        client_key = get_remote_address(request)
        for window in self._write_limits:
            if not self._limiter.hit(window, _WRITE_REQUESTS_SCOPE, client_key):
                return self._rejection(window, client_key)
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
    here, so an unusable value fails at registration rather than at request time.
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return

    write_limits = _parse_window_expression(
        settings.rate_limit_write, "rate_limit_write"
    )
    _parse_window_expression(settings.rate_limit_default, "rate_limit_default")

    app.add_middleware(
        _WriteMethodRateLimitMiddleware, write_limits=write_limits
    )

    # SECURITY: bounds total request volume per client - brute force, credential
    # stuffing and scraping were unthrottled on every route
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[settings.rate_limit_default],
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
