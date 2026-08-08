from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.core.config import get_settings

# Seconds psycopg2 waits for the TCP connection and the TLS handshake to complete before it
# gives up. Without a bound, an endpoint that accepts the connection and then stops answering
# holds the request worker that asked for it for as long as the operating system allows.
DB_CONNECT_TIMEOUT_SECONDS = 10

# Milliseconds the SERVER allows one statement to run before it aborts it. Applied per
# connection through libpq's options string, so it governs every statement on every session
# this engine opens, including one whose client has already gone away. Without it a single
# slow or blocked query occupies a request worker indefinitely.
DB_STATEMENT_TIMEOUT_MS = 30000

settings = get_settings()

# SECURITY: require TLS on the database connection - traffic was previously unencrypted
# SECURITY: bound how long a connection attempt and a statement may take - neither had a
# limit, so an unresponsive endpoint or a blocked query held a request worker indefinitely
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={
        "sslmode": settings.db_sslmode,
        "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
        "options": "-c statement_timeout={0}".format(DB_STATEMENT_TIMEOUT_MS),
    },
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
