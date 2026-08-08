from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.api import workbooks, worksheets, cells, collaboration
from backend.app.core.config import get_settings
from backend.app.core.rate_limit import register_rate_limiting
from backend.app.core.security import AuthEnforcementBypassMarkerMiddleware
from backend.app.core.security_headers import (
    SecurityHeadersMiddleware,
    ServerErrorBoundaryMiddleware,
)
from backend.app.db.database import init_db

app = FastAPI()

@app.on_event('startup')
async def startup_event():
    await init_db()

def configure_cors():
    settings = get_settings()
    # SECURITY: explicit CORS method and header allow-list - both were
    # previously wildcards with credentials enabled
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

# HUMAN ASSISTANCE NEEDED
# Please review the CORS configuration to ensure it meets security requirements
# and aligns with the specific needs of the application.

def include_routers():
    app.include_router(workbooks.router)
    app.include_router(worksheets.router)
    app.include_router(cells.router)
    app.include_router(collaboration.router)

# Middleware registration. Starlette wraps in reverse order of registration, so the calls
# below read innermost first and the effective request path is the reverse:
#   SecurityHeaders -> CORS -> throttling tiers -> bypass marker -> error boundary -> router
# Each position is a contract the middleware it belongs to documents.

# SECURITY: an unhandled error is answered from inside this stack, so its response carries
# the security headers, the CORS headers and the bypass marker - a server error produced
# outside the stack carried none of them. Innermost, per its registration contract.
app.add_middleware(ServerErrorBoundaryMiddleware)
# SECURITY: marks every response served while authentication enforcement is disabled - a
# bypassed request was otherwise indistinguishable from an enforced one. Registered inside
# every BaseHTTPMiddleware, per its registration contract.
app.add_middleware(AuthEnforcementBypassMarkerMiddleware)
# SECURITY: per-client request throttling - no rate limit existed on any route
register_rate_limiting(app)
# SECURITY: cross-origin policy wraps the throttling tiers, so a 429 and an exhausted
# preflight reach the browser with their CORS headers - a rejection produced inside CORS
# surfaced in the browser as an opaque cross-origin failure instead of as 429
configure_cors()
# SECURITY: security response headers on every response, including error, throttling and
# CORS preflight responses - none were emitted before. Outermost, per its registration
# contract.
app.add_middleware(SecurityHeadersMiddleware)
include_routers()