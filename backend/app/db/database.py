from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from backend.app.core.config import get_settings

# The dialect and driver the connect_args below belong to. sslmode, connect_timeout and options
# are libpq parameters that only psycopg2 forwards; SQLAlchemy defers connect_args to connect
# time, so any other driver builds an engine here and fails on its first connection instead.
REQUIRED_DATABASE_BACKEND = "postgresql"
REQUIRED_DATABASE_DRIVER = "psycopg2"

# Seconds psycopg2 waits for the TCP connection and the TLS handshake to complete before it
# gives up. Unbounded, an endpoint that accepts the connection and then stops answering holds
# the request worker that asked for it for as long as the operating system allows.
DB_CONNECT_TIMEOUT_SECONDS = 10

# Milliseconds the SERVER allows one statement to run before aborting it. Applied per
# connection through libpq's options string, so it governs every statement on every session
# this engine opens, including one whose client has already gone away. Unbounded, a single
# slow or blocked query occupies a request worker indefinitely.
DB_STATEMENT_TIMEOUT_MS = 30000

settings = get_settings()

# SECURITY: the resolved URL is confirmed to select the driver the arguments below are for,
# before the engine is built. Settings validates the URL it is given and the URL it fetches from
# Secret Manager, and this is the check that holds when neither of those ran - a Settings built
# directly, or a future caller that assigns the field - so an unsupported dialect can no longer
# reach create_engine, silently accept the psycopg2-only arguments, and fail on its first
# connection with an invalid sslmode instead of at start-up.
_url = make_url(settings.DATABASE_URL)
if (
    _url.get_backend_name() != REQUIRED_DATABASE_BACKEND
    or _url.get_driver_name() != REQUIRED_DATABASE_DRIVER
):
    raise RuntimeError(
        "DATABASE_URL selects the {0}+{1} dialect, but this engine is built with "
        "psycopg2-only connection arguments (sslmode, connect_timeout, options) and requires "
        "{2}+{3}.".format(
            _url.get_backend_name(),
            _url.get_driver_name(),
            REQUIRED_DATABASE_BACKEND,
            REQUIRED_DATABASE_DRIVER,
        )
    )

# SECURITY: the transport mode is forwarded to psycopg2 - the connection was previously
# created with no transport argument at all, so it took whatever the server would accept.
# The mode is settings.db_sslmode and nothing else, taken from the single settings read
# above. Settings has already bound it to the endpoint: an encrypting mode is admissible for
# any host, and the unencrypted mode - which names the Cloud SQL Auth Proxy's plain TCP
# loopback listener - is admissible only for a local host. That check lives beside the
# resolved DATABASE_URL in Settings and is deliberately not repeated here, because two
# copies of one rule are two things that can disagree.
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
