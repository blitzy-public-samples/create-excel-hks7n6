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
    # SECURITY: explicit CORS origin, method and header allow-lists, with credentials
    # enabled. No wildcard is accepted in any of the three.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

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
# the security headers, the CORS headers and the bypass marker. Innermost, per its
# registration contract.
app.add_middleware(ServerErrorBoundaryMiddleware)
# SECURITY: marks every response served while authentication enforcement is disabled, so a
# bypassed request is distinguishable from an enforced one. Registered inside every
# BaseHTTPMiddleware, per its registration contract.
app.add_middleware(AuthEnforcementBypassMarkerMiddleware)
# SECURITY: per-client request throttling and a request-body size bound on every route.
register_rate_limiting(app)
# SECURITY: the cross-origin policy wraps the throttling tiers, so a 429 and an exhausted
# preflight reach the browser carrying their CORS headers rather than as an opaque
# cross-origin failure.
configure_cors()
# SECURITY: security response headers on every response, including error, throttling and
# CORS preflight responses. Outermost, per its registration contract.
app.add_middleware(SecurityHeadersMiddleware)
include_routers()