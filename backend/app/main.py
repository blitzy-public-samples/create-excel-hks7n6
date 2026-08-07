from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.api import workbooks, worksheets, cells, collaboration
from backend.app.core.config import get_settings
from backend.app.core.rate_limit import register_rate_limiting
from backend.app.core.security import AuthEnforcementBypassMarkerMiddleware
from backend.app.core.security_headers import SecurityHeadersMiddleware
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

# SECURITY: marks every response served while authentication enforcement is
# disabled - a bypassed request was otherwise indistinguishable from an
# enforced one. Registered first so it is the innermost middleware, which is
# where the bypass flag set while the route is served is visible.
app.add_middleware(AuthEnforcementBypassMarkerMiddleware)
configure_cors()
# SECURITY: per-client request throttling - no rate limit existed on any route
register_rate_limiting(app)
# SECURITY: security response headers on every response, including error
# and CORS preflight responses - none were emitted before
app.add_middleware(SecurityHeadersMiddleware)
include_routers()