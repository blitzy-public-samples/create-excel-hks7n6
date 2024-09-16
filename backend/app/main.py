from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.api import workbooks, worksheets, cells, collaboration
from backend.app.core.config import get_settings
from backend.app.db.database import init_db

app = FastAPI()

@app.on_event('startup')
async def startup_event():
    await init_db()

def configure_cors():
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# HUMAN ASSISTANCE NEEDED
# Please review the CORS configuration to ensure it meets security requirements
# and aligns with the specific needs of the application.

def include_routers():
    app.include_router(workbooks.router)
    app.include_router(worksheets.router)
    app.include_router(cells.router)
    app.include_router(collaboration.router)

configure_cors()
include_routers()