"""Two-tier per-client request throttling for the Excel Clone API.

Tier one applies a global ceiling to every matched route. Tier two applies a tighter
budget to mutating HTTP methods. Both tiers are installed as middleware by
:func:`register_rate_limiting` and require nothing of any route handler.
"""

import math
import time
from typing import FrozenSet

from fastapi import FastAPI
from limits import RateLimitItem, parse, parse_many
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.core.config import get_settings

# HTTP methods metered by the write tier. Every other method bypasses that tier.
WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Status code returned to a client that has exhausted either budget.
TOO_MANY_REQUESTS_STATUS: int = 429

# Floor, in seconds, for the Retry-After value of a write-tier rejection.
MIN_RETRY_AFTER_SECONDS: int = 1


class WriteMethodRateLimitMiddleware:
    """Meters mutating requests per client address using a fixed window.

    Non-HTTP scopes and requests whose method is absent from :data:`WRITE_METHODS`
    are passed through untouched and consume none of the budget. A client that has
    exhausted the budget receives an HTTP 429 carrying a ``Retry-After`` header and
    the request does not reach the application.

    The instance holds no per-client state of its own; counting is delegated to the
    ``limiter`` supplied at construction, which owns the window storage.
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: FixedWindowRateLimiter,
        limit: RateLimitItem,
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_metered = (
            scope["type"] == "http"
            and scope.get("method", "").upper() in WRITE_METHODS
        )
        if not is_metered:
            await self.app(scope, receive, send)
            return

        # Same key function as the global tier: one client resolves to one identity
        # in both tiers.
        client_key = get_remote_address(Request(scope))
        if self._limiter.hit(self._limit, client_key):
            await self.app(scope, receive, send)
            return

        await self._build_rejection(client_key)(scope, receive, send)

    def _retry_after_seconds(self, client_key: str) -> int:
        """Whole seconds until this client's window resets, never below one."""
        reset_time = self._limiter.get_window_stats(self._limit, client_key).reset_time
        return max(MIN_RETRY_AFTER_SECONDS, math.ceil(reset_time - time.time()))

    def _build_rejection(self, client_key: str) -> JSONResponse:
        """Build the 429 response for a client that is over budget."""
        return JSONResponse(
            {"error": f"Rate limit exceeded: {self._limit}"},
            status_code=TOO_MANY_REQUESTS_STATUS,
            headers={"Retry-After": str(self._retry_after_seconds(client_key))},
        )


def _validate_default_limit(expression: str) -> str:
    """Return ``expression`` unchanged once it parses as global-tier grammar.

    Accepts every form the global tier itself accepts, including several windows
    separated by semicolons. Raises :class:`ValueError` if it does not parse.
    """
    try:
        parse_many(expression)
    except ValueError as exc:
        raise ValueError(
            "Settings.rate_limit_default is not a valid rate limit expression: "
            f"{expression!r}"
        ) from exc
    return expression


def _parse_write_limit(expression: str) -> RateLimitItem:
    """Return ``expression`` parsed into a single window for the write tier.

    Raises :class:`ValueError` if it does not parse.
    """
    try:
        return parse(expression)
    except ValueError as exc:
        raise ValueError(
            "Settings.rate_limit_write is not a valid rate limit expression: "
            f"{expression!r}"
        ) from exc


def register_rate_limiting(app: FastAPI) -> None:
    """Install both throttling tiers on ``app``.

    Registers nothing whatsoever — no middleware, no exception handler and no
    ``app.state.limiter`` — when ``Settings.rate_limit_enabled`` is false.

    Raises :class:`ValueError`, before either tier is installed, if either configured
    threshold is not valid rate limit grammar.
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return

    default_limit = _validate_default_limit(settings.rate_limit_default)
    write_limit = _parse_write_limit(settings.rate_limit_write)

    # SECURITY: global per-client request ceiling — request volume was previously unbounded
    limiter = Limiter(key_func=get_remote_address, default_limits=[default_limit])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # SECURITY: tighter per-client ceiling on mutating methods — writes were previously unthrottled
    # Installed after the global tier, which places this one outermost: it is evaluated first.
    app.add_middleware(
        WriteMethodRateLimitMiddleware,
        limiter=FixedWindowRateLimiter(MemoryStorage()),
        limit=write_limit,
    )
