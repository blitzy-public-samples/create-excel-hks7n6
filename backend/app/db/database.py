from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.core.config import get_settings

# One Settings instance for both values below: every construction performs live Secret
# Manager reads, and this module is evaluated on import.
settings = get_settings()

# SECURITY: require TLS on the database connection — traffic was previously unencrypted
engine = create_engine(settings.DATABASE_URL, connect_args={"sslmode": settings.db_sslmode})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()