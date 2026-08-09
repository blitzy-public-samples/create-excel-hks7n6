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

# Connections kept open between requests, and the extra ones the pool may open under load and
# discard afterwards. Their SUM is the most connections one worker process can hold at once,
# published below as DB_POOL_CAPACITY.
#
# The budget these have to fit inside: the Cloud SQL instance's max_connections, divided by
# the number of worker processes per pod times the number of pods, with headroom left for
# administrative sessions and for the maintenance operations Cloud SQL performs itself. At the
# instance default of 100 the values below allow five workers before that budget is reached.
DB_POOL_SIZE = 5
DB_MAX_OVERFLOW = 15

# The most connections one worker can hold at once. Published because it is not only a
# database setting: rate_limit.register_rate_limiting bounds the number of requests in flight
# to this number, which is what keeps connection demand inside the pool. See the note on
# DB_POOL_TIMEOUT_SECONDS for why demand is bounded by requests rather than by threads.
DB_POOL_CAPACITY = DB_POOL_SIZE + DB_MAX_OVERFLOW

# Seconds a request waits for a pooled connection before the pool gives up and the request
# fails. Reached only if the concurrency bound above is ever removed or misconfigured, because
# with it in place connection demand cannot exceed capacity: get_db hands each request one
# Session, and the identity lookup in core/security.py closes its own Session before the
# handler's acquires a connection, so one request in flight needs one connection at a time.
#
# Kept SHORT rather than at SQLAlchemy's 30-second default because of what the failure looks
# like. Runtime verification measured 600 requests at a concurrency of 120 against the
# unbounded configuration: 284 of them - 47 per cent - waited the full default timeout and then
# received HTTP 500, with pg_stat_activity showing every pooled connection idle inside an open
# transaction while only one or two were executing anything. Thirty seconds of waiting followed
# by a server error is the worst available answer; ten seconds is a fault report.
DB_POOL_TIMEOUT_SECONDS = 10

# Seconds after which an idle pooled connection is discarded and reopened. Unbounded - which is
# SQLAlchemy's default - a connection lives until something closes it, and the thing that
# closes it is usually the server: a Cloud SQL maintenance restart, a failover, or an idle
# reaper. Recycling below any such interval means the pool retires connections before the
# server does.
DB_POOL_RECYCLE_SECONDS = 1800

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
# SECURITY: the pool is sized, bounded and health-checked explicitly rather than left on
# SQLAlchemy's defaults - the previous configuration answered HTTP 500 to 47 per cent of a
# 120-request burst after a 30-second wait, and returned a 500 to a caller on every
# server-side connection close.
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={
        "sslmode": settings.db_sslmode,
        "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
        "options": "-c statement_timeout={0}".format(DB_STATEMENT_TIMEOUT_MS),
    },
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT_SECONDS,
    # SECURITY: a pooled connection is checked before it is handed out. Without this, a
    # connection the SERVER closed - a Cloud SQL failover, a maintenance restart, an idle
    # reaper - is handed to the next request, which fails: runtime verification terminated this
    # engine's backends and measured exactly one user-visible HTTP 500 before SQLAlchemy
    # invalidated the pool. The check costs one round trip on checkout and makes the event
    # invisible to callers.
    pool_pre_ping=True,
    pool_recycle=DB_POOL_RECYCLE_SECONDS,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
