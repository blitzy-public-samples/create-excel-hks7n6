from pydantic import BaseSettings, conint, validator
from google.cloud import secretmanager
from typing import List, Literal, get_args
from urllib.parse import urlsplit
import re

# Seven days expressed in minutes: the longest lifetime the Cloud Storage V4 signing scheme
# accepts for a signed URL.
MAX_SIGNED_URL_EXPIRY_MINUTES = 10080

# Twenty-four hours expressed in minutes: the longest lifetime an access token minted by
# create_access_token may carry.
MAX_ACCESS_TOKEN_EXPIRE_MINUTES = 1440

# THE canonical browser-origin grammar. These three expressions are the single authority for
# what an origin may look like, and infrastructure/terraform/variables.tf carries each of them
# character for character in the validation blocks of both allowed_origins and api_origin, so
# a value one plane accepts is accepted by the other and a value one refuses is refused by both.
#
# ORIGIN_PATTERN: scheme://host[:port] and nothing else. The host is either the IPv6 loopback
# literal or a lower-case ASCII name whose every dot-separated label starts and ends
# alphanumeric; the optional port is 1..65535 with no leading zero. A path, query, fragment,
# trailing slash or dot, wildcard, embedded credentials, upper-case or non-ASCII host,
# underscore, empty or hyphen-edged label, and any surrounding whitespace all fail to match.
ORIGIN_PATTERN = re.compile(
    r"^https?://(\[::1\]|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*)(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$"
)

# An origin whose traffic is encrypted. Matching either this or LOOPBACK_HTTP_ORIGIN_PATTERN
# is what admits an origin's scheme.
HTTPS_ORIGIN_PATTERN = re.compile(r"^https://")

# The one exception to https: plaintext for a loopback host, so a development origin stays
# usable while a deployment origin must be encrypted.
LOOPBACK_HTTP_ORIGIN_PATTERN = re.compile(
    r"^http://(localhost|127\.0\.0\.1|\[::1\])(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$"
)

# Hosts that resolve inside the pod or the machine the process runs on. A URL naming one of
# these never puts its traffic on a network. urlsplit().hostname strips the brackets from an
# IPv6 literal, so the bracketed spelling is carried too for a caller that reads a raw host.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# The same set under the name the database transport gate reads it by: a DATABASE_URL naming
# one of these never puts database traffic on a network, which is the only condition under
# which the unencrypted transport mode is admissible. One set, so the two gates cannot drift.
LOCAL_DATABASE_HOSTS = LOOPBACK_HOSTS

# Transport modes that guarantee an encrypted PostgreSQL connection. allow and prefer are
# absent because each of them negotiates plaintext whenever the server offers it, silently
# and with no error to observe.
ENCRYPTING_DATABASE_SSL_MODES = frozenset({"require"})

# The transport mode that performs no encryption of its own. The Cloud SQL Auth Proxy
# presents a plain TCP listener on the loopback interface and encrypts its own leg to the
# instance, so a client asking for TLS on that listener cannot connect at all. Admissible
# ONLY for a local endpoint, which _assert_database_transport_topology enforces after the
# DATABASE_URL secret has been resolved.
UNENCRYPTED_DATABASE_SSL_MODE = "disable"

# The transport modes this application accepts. allow and prefer are absent because each of
# them negotiates plaintext whenever the server offers it. disable is present but is not
# freely selectable: _assert_database_transport_topology refuses it for any endpoint that is
# not local, so the plaintext mode can only ever describe a connection that never leaves the
# machine.
# verify-ca and verify-full are absent because infrastructure/terraform provisions the
# instance with no private network, so the only route to it is the Cloud SQL Auth Proxy,
# whose local listener carries no certificate for the instance name; verification against
# it fails closed at start-up, and scripts/deploy.sh refuses both values in the manifests.
DatabaseSslMode = Literal["disable", "require"]


# The database URL forms this application supports, as SQLAlchemy dialect+driver names.
# postgres:// is deliberately absent: SQLAlchemy 1.4 rejects that spelling outright.
SUPPORTED_DATABASE_DRIVERS = ("postgresql", "postgresql+psycopg2")


def validate_database_url(value: str) -> str:
    """Return ``value`` unchanged once confirmed to be a URL this application can connect with.

    ``backend/app/db/database.py`` builds its engine at module scope and hands psycopg2 the
    libpq-specific ``sslmode``, ``connect_timeout`` and ``options`` arguments. SQLAlchemy defers
    ``connect_args`` to connect time, so a URL selecting any other dialect still *constructs* an
    engine and only fails on the first connection - by which time the process has started and the
    failure surfaces as an unexplained request error rather than as a configuration fault. An
    async driver fails differently and just as late, because ``create_engine`` is synchronous.
    Both are refused here instead.

    The value is a credential-bearing URL, so no rejection quotes it. Each message names only the
    driver scheme, which is drawn from a fixed vocabulary and carries no secret.

    Args:
        value: A SQLAlchemy connection URL, from the environment or from Secret Manager.

    Returns:
        str: ``value`` with surrounding whitespace removed.

    Raises:
        ValueError: if the URL is empty, does not select a synchronous PostgreSQL psycopg2
            driver, names no host, or names no single database.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("DATABASE_URL must not be empty")
    parts = urlsplit(candidate)
    driver = parts.scheme.lower()
    if driver not in SUPPORTED_DATABASE_DRIVERS:
        raise ValueError(
            "DATABASE_URL must select a synchronous PostgreSQL psycopg2 driver, one of "
            f"{'/'.join(SUPPORTED_DATABASE_DRIVERS)}; rejected the driver "
            f"{driver or '(none)'!r}. The engine is built with psycopg2-only connect "
            "arguments, so another driver ignores or rejects them after start-up rather than "
            "here"
        )
    if not parts.hostname:
        raise ValueError(
            "DATABASE_URL must name a host. The Cloud SQL Auth Proxy listens on a TCP "
            "loopback address inside the pod, so the URL is host-based rather than "
            "socket-based"
        )
    database = parts.path.lstrip("/")
    if not database or "/" in database:
        raise ValueError(
            "DATABASE_URL must name exactly one database as its path, as in "
            "postgresql+psycopg2://USER:PASSWORD@HOST:5432/DATABASE"
        )
    return candidate

# Signing algorithms create_access_token and the legacy verifier may use. All three are
# HMAC over the shared SECRET_KEY, which is the only key material this application holds.
SigningAlgorithm = Literal["HS256", "HS384", "HS512"]
SIGNING_ALGORITHMS = frozenset(get_args(SigningAlgorithm))

# Shortest SECRET_KEY accepted, in characters. An HMAC key shorter than the digest it
# produces adds no strength beyond its own length, and HS256 produces 32 bytes.
MIN_SECRET_KEY_LENGTH = 32

# Fewest distinct characters a SECRET_KEY must contain. A long run of one character passes a
# length check while carrying almost no entropy.
MIN_SECRET_KEY_DISTINCT_CHARACTERS = 8

# Redis URL schemes. rediss is TLS; redis is cleartext and is accepted only for a loopback
# host, mirroring the rule ALLOWED_ORIGINS applies to http.
REDIS_TLS_SCHEME = "rediss"
REDIS_CLEARTEXT_SCHEME = "redis"

# Window-store schemes that keep counters inside this process and therefore reach no network.
LOCAL_STORAGE_SCHEMES = frozenset({"memory", "async+memory"})


def _require_strong_signing_key(value: str) -> None:
    """Raise if ``value`` is too short or too repetitive to be an HMAC signing key.

    Args:
        value: The ``SECRET_KEY`` this process will sign and verify local tokens with.

    Raises:
        ValueError: if the key is shorter than :data:`MIN_SECRET_KEY_LENGTH` characters or
            carries fewer than :data:`MIN_SECRET_KEY_DISTINCT_CHARACTERS` distinct
            characters.
    """
    if len(value) < MIN_SECRET_KEY_LENGTH:
        raise ValueError(
            "SECRET_KEY must be at least {0} characters; the value in use is {1}. "
            "It is the HMAC key every locally-issued token is signed with, so a short "
            "key is brute-forceable and lets an attacker mint tokens.".format(
                MIN_SECRET_KEY_LENGTH, len(value)
            )
        )
    if len(set(value)) < MIN_SECRET_KEY_DISTINCT_CHARACTERS:
        raise ValueError(
            "SECRET_KEY must carry at least {0} distinct characters; the value in use "
            "carries {1}. A long repetitive value passes a length check while carrying "
            "almost no entropy.".format(
                MIN_SECRET_KEY_DISTINCT_CHARACTERS, len(set(value))
            )
        )


def _require_encrypted_redis_url(value: str, field_name: str) -> None:
    """Raise if ``value`` reaches a non-loopback Redis over cleartext.

    A non-Redis scheme is left alone: the window store also accepts ``memory://`` and other
    backends, and those are checked by their own scheme list rather than here.

    Args:
        value: The URL to check.
        field_name: Named in the error so the caller knows which setting was refused.

    Raises:
        ValueError: if the URL uses the cleartext ``redis`` scheme for a host that is not
            loopback.
    """
    parts = urlsplit(value)
    if parts.scheme != REDIS_CLEARTEXT_SCHEME:
        return
    if parts.hostname in LOOPBACK_HOSTS:
        return
    raise ValueError(
        "{0} uses the cleartext {1}:// scheme for host {2!r}. Redis carries the "
        "throttling counters and the Celery job payloads, so an unencrypted hop exposes "
        "them to any network position. Use {3}:// , or {1}:// only for a loopback "
        "host.".format(
            field_name, REDIS_CLEARTEXT_SCHEME, parts.hostname, REDIS_TLS_SCHEME
        )
    )


class Settings(BaseSettings):
    PROJECT_ID: str
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str
    # SECURITY: restricted to the HMAC algorithms this application holds a key for - an
    # unconstrained value admitted "none", which strips signature verification from the
    # legacy token path entirely, and admitted asymmetric algorithms whose verification key
    # would be the shared secret
    ALGORITHM: SigningAlgorithm
    # SECURITY: bounds the lifetime of a minted access token - the value is now honoured, so
    # a non-positive setting invalidates every default token and an unbounded one mints a
    # credential that outlives any session policy. Constrained to
    # 1..MAX_ACCESS_TOKEN_EXPIRE_MINUTES and refused at construction. An explicit
    # expires_delta passed to create_access_token still takes precedence.
    ACCESS_TOKEN_EXPIRE_MINUTES: conint(gt=0, le=MAX_ACCESS_TOKEN_EXPIRE_MINUTES)

    # SECURITY: the explicit CORS origin allow-list main.py builds its policy from.
    ALLOWED_ORIGINS: List[str] = []

    # SECURITY: names the private uploads bucket file_storage.py reads and writes.
    gcs_bucket_name: str = ""

    # SECURITY: bounds the lifetime of every object-access grant - upload URLs were public
    # and permanent
    # CONTRACT: 1..MAX_SIGNED_URL_EXPIRY_MINUTES, rejected at construction, before any storage
    # operation runs. Cloud Storage refuses a V4 signature valid for longer than seven days.
    signed_url_expiry_minutes: conint(ge=1, le=MAX_SIGNED_URL_EXPIRY_MINUTES) = 15

    # SECURITY: database transport mode - the connection was created with no TLS setting at
    # all. allow and prefer are refused here, before database.py hands the value to the
    # driver, because both negotiate plaintext silently whenever the server offers it.
    # "disable" is accepted by the type but not freely: it names the Cloud SQL Auth Proxy's
    # plain TCP loopback listener, and __init__ refuses it for any DATABASE_URL that reaches
    # a network host. verify-ca and verify-full cannot complete through that proxy, because
    # they would validate its local listener against the instance certificate. The default
    # is an encrypting mode, so an unset value is never the plaintext one.
    db_sslmode: DatabaseSslMode = "require"

    # SECURITY: selects the server-side token verification path - client-asserted identity
    # was never verified
    # CONTRACT: these two values only. security.py treats anything that is not "legacy_jwt" as
    # the Firebase path, so a misspelled value is refused at construction rather than silently
    # selecting Firebase. Read per request.
    auth_token_verifier: Literal["firebase", "legacy_jwt"] = "firebase"

    # SECURITY: authentication enforcement switch. False admits requests on unverified token
    # claims; every such admission is logged and marked on the response.
    auth_enforcement_enabled: bool = True

    # SECURITY: request throttling switch. False installs neither tier.
    rate_limit_enabled: bool = True

    # SECURITY: global per-client request ceiling, applied to every route.
    rate_limit_default: str = "600/minute"

    # SECURITY: tighter ceiling for mutating methods - write volume was unbounded
    # One budget is shared by every write a client makes, so the default is set above the
    # aggregate a normal editing session produces rather than at one route's rate. The client
    # coalesces cell writes into at most one request per worksheet per 1000 ms, which is 60 a
    # minute per edited worksheet; 300 admits five worksheets edited at that rate at once,
    # and four leave 60 a minute for the workbook-creation and share routes. It stays half
    # the rate_limit_default ceiling, so the write tier remains the tighter of the two.
    rate_limit_write: str = "300/minute"

    # SECURITY: Content-Security-Policy enforcement mode. True emits the policy under the
    # Content-Security-Policy-Report-Only header name instead.
    csp_report_only: bool = False

    # SECURITY: restricts token verification to a single Firebase project - the issuer was
    # unconstrained
    # CONTRACT: one project issues the tokens this deployment accepts. Set this only to make
    # that project explicit; it must equal PROJECT_ID, which _verifier_project_is_the_project
    # enforces at construction. Empty resolves to PROJECT_ID, so both spellings name the same
    # verifier project and no other plane can disagree with it.
    firebase_project_id: str = ""

    # SECURITY: rejects a verifier project other than PROJECT_ID - the accepted token issuer
    # could differ from the project the frontend build signs in against and the project the
    # infrastructure and the deployment preflight check, so a token every API call rejects
    # could be published as a working configuration
    @validator("firebase_project_id", allow_reuse=True)
    def _verifier_project_is_the_project(cls, value: str, values: dict) -> str:
        """Return ``value`` unchanged once confirmed to name the deployment's project.

        Raises:
            ValueError: if ``value`` is non-empty and differs from ``PROJECT_ID``.
        """
        project = values.get("PROJECT_ID")
        if value and project and value != project:
            raise ValueError(
                "firebase_project_id must equal PROJECT_ID, which is the one project this "
                "deployment accepts ID tokens from; leave it empty to resolve to PROJECT_ID"
            )
        return value

    # SECURITY: rejects a URL whose driver the engine's psycopg2-only connect arguments do not
    # fit - an unsupported dialect built an engine at import and then failed on its first
    # connection, so a misconfigured driver surfaced as a request error rather than at start-up
    @validator("DATABASE_URL", allow_reuse=True)
    def _database_url_must_be_supported(cls, value: str) -> str:
        """Return ``value`` once :func:`validate_database_url` accepts it.

        This covers the environment-supplied value only. ``__init__`` replaces it with the
        Secret Manager value afterwards, which Pydantic does not re-validate, so that value is
        checked again there against the same function.
        """
        return validate_database_url(value)


    # SECURITY: rejects wildcard, plaintext and non-origin values - a credentialed CORS
    # policy was built from an unvalidated list
    # allow_reuse keeps this module re-importable in one process, which Pydantic v1
    # otherwise refuses with a duplicate-validator ConfigError.
    @validator("ALLOWED_ORIGINS", allow_reuse=True)
    def _validate_allowed_origins(cls, origins: List[str]) -> List[str]:
        """Return ``origins`` as exact browser origins, deduplicated in order.

        Every entry is matched against :data:`ORIGIN_PATTERN` and then against
        :data:`HTTPS_ORIGIN_PATTERN` or :data:`LOOPBACK_HTTP_ORIGIN_PATTERN`, which are the
        same three expressions ``infrastructure/terraform/variables.tf`` applies to
        ``allowed_origins`` and ``api_origin``. The value is matched exactly as supplied -
        it is not trimmed, lower-cased or otherwise normalised - so an entry accepted here
        is accepted by Terraform and an entry Terraform refuses is refused here.

        Raises:
            ValueError: if any entry is not an exact ``scheme://host[:port]`` origin under
                that grammar, or is a plaintext origin for a non-loopback host.
        """
        validated: List[str] = []
        for origin in origins:
            if not ORIGIN_PATTERN.match(origin):
                raise ValueError(
                    "ALLOWED_ORIGINS entries must be exactly scheme://host[:port], for "
                    "example https://app.example.com: a lower-case ASCII host whose every "
                    "dot-separated label starts and ends alphanumeric, and a port between 1 "
                    "and 65535. A trailing slash, path, query string, fragment, wildcard, "
                    "embedded credentials, upper-case or non-ASCII host, underscore, empty "
                    "or hyphen-edged label, trailing dot and surrounding whitespace are "
                    f"rejected. Rejected: {origin!r}"
                )
            if not (
                HTTPS_ORIGIN_PATTERN.match(origin)
                or LOOPBACK_HTTP_ORIGIN_PATTERN.match(origin)
            ):
                raise ValueError(
                    "ALLOWED_ORIGINS entries must use https, except http://localhost, "
                    "http://127.0.0.1 and http://[::1] for local development. Admitting a "
                    "plaintext origin authorises credentialed requests a network position "
                    f"can read and rewrite. Rejected: {origin!r}"
                )
            if origin not in validated:
                validated.append(origin)
        return validated

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        project_path = f"projects/{self.PROJECT_ID}"
        
        # The Secret Manager client owns a gRPC channel and the sockets beneath it, and this
        # constructor runs once per get_settings() call, so the channel is released when the
        # three reads below return rather than being left for the garbage collector.
        with secretmanager.SecretManagerServiceClient() as client:
            database_url_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/DATABASE_URL/versions/latest"})
            redis_url_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/REDIS_URL/versions/latest"})
            secret_key_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/SECRET_KEY/versions/latest"})
        
        # Set retrieved values to class properties
        # SECURITY: the fetched URL is validated before it is assigned. Pydantic ran the
        # DATABASE_URL validator against the ENVIRONMENT value during super().__init__() above
        # and does not re-run it on assignment, so without this the value the engine is actually
        # built from - the secret - reached create_engine unchecked.
        self.DATABASE_URL = validate_database_url(
            database_url_secret.payload.data.decode("UTF-8")
        )
        self.REDIS_URL = redis_url_secret.payload.data.decode("UTF-8")
        self.SECRET_KEY = secret_key_secret.payload.data.decode("UTF-8")

        # SECURITY: the two fetched values that carry a security property are checked HERE,
        # after the assignments above, because these three fields are overwritten with what
        # Secret Manager returned. A field validator inspects the value the environment
        # supplied, which is then discarded, so it would have certified a value this process
        # never uses.
        # SECURITY: the signing key is refused if it is too short or too repetitive to be an
        # HMAC key - any value at all was accepted, so a placeholder secret produced
        # forgeable tokens on the legacy verifier path
        _require_strong_signing_key(self.SECRET_KEY)
        # SECURITY: a cleartext Redis hop is refused for a non-loopback host - the throttling
        # counters and the Celery payloads crossed the network unencrypted
        _require_encrypted_redis_url(self.REDIS_URL, "REDIS_URL")
        # SECURITY: refuse the unencrypted transport mode for any endpoint reached over a
        # network - the mode named the Cloud SQL Auth Proxy's loopback listener and nothing
        # prevented it describing a connection that crossed a network.
        # Last statement of __init__, so it reads the resolved DATABASE_URL above rather
        # than the pre-secret placeholder.
        self._assert_database_transport_topology()

    def _assert_database_transport_topology(self) -> None:
        """Confirm that ``db_sslmode`` is admissible for the resolved ``DATABASE_URL``.

        An encrypting mode is admissible for any endpoint. The unencrypted mode is
        admissible only when the connection stays on the machine: a loopback host, which is
        the Cloud SQL Auth Proxy sidecar listening beside the application, or no network
        host at all, which is a Unix socket or a file-backed engine.

        The URL is never included in the message it raises, because it carries the database
        password.

        Raises:
            ValueError: if ``db_sslmode`` is the unencrypted mode and ``DATABASE_URL`` names
                a host that is not local, or a URL that cannot be parsed at all.
        """
        if self.db_sslmode != UNENCRYPTED_DATABASE_SSL_MODE:
            return
        try:
            host = urlsplit(self.DATABASE_URL).hostname
        except ValueError as exc:
            # Fail closed. An unparseable authority cannot be shown to be local, and the
            # mode under consideration performs no encryption.
            raise ValueError(
                f"db_sslmode={UNENCRYPTED_DATABASE_SSL_MODE!r} requires a DATABASE_URL whose "
                f"host is one of {sorted(LOCAL_DATABASE_HOSTS)}, and the URL could not be "
                f"parsed to determine its host ({exc})"
            ) from exc
        if host is None or host in LOCAL_DATABASE_HOSTS:
            return
        raise ValueError(
            f"db_sslmode={UNENCRYPTED_DATABASE_SSL_MODE!r} is permitted only when DATABASE_URL "
            f"names a local endpoint - one of {sorted(LOCAL_DATABASE_HOSTS)}, or no network "
            f"host - because that mode performs no encryption and is intended for the Cloud "
            f"SQL Auth Proxy's plain TCP listener inside the same pod. The resolved host is "
            f"{host!r}, which is reached over a network, so the connection must use one of "
            f"{sorted(ENCRYPTING_DATABASE_SSL_MODES)}."
        )

def get_settings() -> Settings:
    return Settings()