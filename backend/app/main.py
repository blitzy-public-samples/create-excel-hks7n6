from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from backend.app.api import workbooks, worksheets, cells, collaboration
from backend.app.core.config import get_settings
from backend.app.core.logging_config import configure_logging
from backend.app.core.rate_limit import RETRY_AFTER_HEADER, register_rate_limiting
from backend.app.core.security import AuthEnforcementBypassMarkerMiddleware
from backend.app.core.security_headers import (
    SecurityHeadersMiddleware,
    ServerErrorBoundaryMiddleware,
)
from backend.app.db.database import init_db

# SECURITY: gives every security record a level, a timestamp and a logger name, and escapes
# control characters in it - records carried none of the three, so a throttling degradation
# could not be told from a provider outage and informational records were discarded entirely.
# Discarding them mattered most for one record: the one register_rate_limiting emits names the
# window store the throttling tiers count in, and that is what distinguishes a deployment
# enforcing the configured quota from one enforcing a multiple of it.
# CONTRACT: called once, and before everything below, so the registration calls can report
# themselves and no record predates the configuration. Nothing under backend/ emits a record
# before this runs.
configure_logging()

app = FastAPI()

#: Smallest response worth compressing. Below this, gzip framing costs more bytes than it
#: saves, and every short body this application produces - the fixed error details, the
#: throttling refusals, an empty page - sits well under it.
COMPRESSION_MINIMUM_SIZE = 500

#: Deflate level. Measured on the 2,742,333-byte default workbook page: level 1 gave 31.8x in
#: 7.3 ms, level 6 gave 74.8x in 16.9 ms, level 9 gave 110.9x in 20.1 ms. Compression runs on
#: the event loop, so the millisecond figure is time no other request can use - the same
#: resource the concurrency bound exists to protect - and the extra ratio above level 6 is
#: highly dependent on how repetitive the data happens to be while the extra cost is not.
COMPRESSION_LEVEL = 6

@app.on_event('startup')
async def startup_event():
    await init_db()

def configure_cors():
    settings = get_settings()
    # SECURITY: explicit CORS origin, method and header allow-lists, with credentials
    # enabled. No wildcard is accepted in any of the three.
    # Retry-After is exposed because a cross-origin caller cannot read a response header that
    # is not on the CORS safelist, and Retry-After is not on it. The client seam reads that
    # header to tell the user when to try again; without this the value it computes is always
    # undefined for a browser and the throttling refusal cannot be honoured. Exposing it
    # discloses nothing: the value is the throttling window this application publishes.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=[RETRY_AFTER_HEADER],
    )

def include_routers():
    app.include_router(workbooks.router)
    app.include_router(worksheets.router)
    app.include_router(cells.router)
    app.include_router(collaboration.router)

# Middleware registration. Starlette wraps in reverse order of registration, so the calls
# below read innermost first and the effective request path is the reverse:
#   SecurityHeaders -> throttling tiers -> CORS -> compression -> bypass marker
#     -> error boundary -> router
# Each position is a contract the middleware it belongs to documents.

# SECURITY: an unhandled error is answered from inside this stack, so its response carries
# the security headers, the CORS headers and the bypass marker. Innermost, per its
# registration contract.
app.add_middleware(ServerErrorBoundaryMiddleware)
# SECURITY: marks every response served while authentication enforcement is disabled, so a
# bypassed request is distinguishable from an enforced one. Registered inside every
# BaseHTTPMiddleware, per its registration contract.
app.add_middleware(AuthEnforcementBypassMarkerMiddleware)
# SECURITY: bounds the bytes a single request can make the server put on the wire. The default
# workbook page was served at 2,742,333 bytes with no content encoding whatever the caller
# offered, so one request cost megabytes of egress and the connection time to push them -
# amplification a client cannot opt out of. Registered INSIDE the cross-origin policy and
# inside the throttling tiers, so a response either of those produces itself is never
# rewritten, and OUTSIDE the bypass marker and the error boundary, so a marked or
# error-boundary response is compressed like any other. It cannot double-encode: the responder
# leaves any response that already declares a Content-Encoding alone. Both this and CORS add
# to Vary rather than replacing it, so neither loses the other's entry.
app.add_middleware(
    GZipMiddleware,
    minimum_size=COMPRESSION_MINIMUM_SIZE,
    compresslevel=COMPRESSION_LEVEL,
)
# SECURITY: the cross-origin policy is registered INSIDE the throttling tiers. CORSMiddleware
# answers a CORS preflight itself and never calls the application inside it, so a preflight is
# counted only while the tiers wrap CORS - and a browser sends one preflight per API call
# carrying an Authorization header, because that header is not CORS-safelisted. Registered
# outside the tiers this was an unmetered surface amounting to roughly half a browser client's
# requests. The cost of this order is that a response a tier produces itself does not travel
# back out through CORS, which is why those responses carry the cross-origin headers
# rate_limit._cross_origin_headers supplies. A preflight answered with any non-2xx status is a
# CORS failure by specification, so an exhausted preflight is reported to the page as a failed
# request whatever headers accompany it; being counted is what bounds the surface.
configure_cors()
# SECURITY: per-client request throttling and a request-body size bound on every request,
# preflights included.
register_rate_limiting(app)
# SECURITY: security response headers on every response, including error, throttling and
# CORS preflight responses. Outermost, per its registration contract.
app.add_middleware(SecurityHeadersMiddleware)
include_routers()