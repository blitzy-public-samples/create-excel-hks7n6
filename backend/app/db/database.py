from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.core.config import get_settings

# SECURITY: require TLS on the database connection — traffic was previously unencrypted
engine = create_engine(get_settings().DATABASE_URL, connect_args={"sslmode": get_settings().db_sslmode})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()