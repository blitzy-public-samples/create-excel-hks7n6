from pydantic import BaseSettings, conint, validator
from google.cloud import secretmanager
from ipaddress import ip_network
from typing import List, Literal
from urllib.parse import urlsplit
import re

# Seven days expressed in minutes: the longest lifetime the Cloud Storage V4 signing scheme
# accepts for a signed URL.
MAX_SIGNED_URL_EXPIRY_MINUTES = 10080

# Twenty-four hours expressed in minutes: the longest lifetime an access token minted by
# create_access_token may carry.
MAX_ACCESS_TOKEN_EXPIRE_MINUTES = 1440

# Origins that may use plaintext http. Every other origin must use https.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

# The only schemes a browser origin may carry.
ORIGIN_SCHEMES = frozenset({"http", "https"})

# A Google service account address. Mirrors the format the Terraform
# signer_service_account variable enforces, so one canonical value satisfies both planes.
SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)

# Transport modes that guarantee an encrypted PostgreSQL connection. disable, allow and
# prefer are excluded because each of them can negotiate plaintext.
DatabaseSslMode = Literal["require", "verify-ca", "verify-full"]


class Settings(BaseSettings):
    PROJECT_ID: str
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str
    ALGORITHM: str
    # SECURITY: bounds the lifetime of a minted access token - the value is now honoured, so
    # a non-positive setting invalidates every default token and an unbounded one mints a
    # credential that outlives any session policy. Constrained to
    # 1..MAX_ACCESS_TOKEN_EXPIRE_MINUTES and refused at construction. An explicit
    # expires_delta passed to create_access_token still takes precedence.
    ACCESS_TOKEN_EXPIRE_MINUTES: conint(gt=0, le=MAX_ACCESS_TOKEN_EXPIRE_MINUTES)

    # SECURITY: explicit CORS origin allow-list - main.py read this field before it was
    # declared, so the origin policy had no resolvable value
    ALLOWED_ORIGINS: List[str] = []

    # SECURITY: names the private uploads bucket - file_storage.py read this field before
    # it was declared
    gcs_bucket_name: str = ""

    # SECURITY: bounds the lifetime of every object-access grant - upload URLs were public
    # and permanent
    # Constrained to 1..MAX_SIGNED_URL_EXPIRY_MINUTES: a non-positive lifetime yields an
    # unusable grant and Cloud Storage rejects a V4 signature valid for longer than seven
    # days. Rejected at construction, before any storage operation runs.
    signed_url_expiry_minutes: conint(ge=1, le=MAX_SIGNED_URL_EXPIRY_MINUTES) = 15

    # SECURITY: names the dedicated service account that signs object URLs - the signing
    # identity was declared only in Terraform and never reached the application
    # Empty means the runtime's own attached identity signs. Otherwise this must equal the
    # Terraform signer_service_account variable, so the IAM grant and the signer are one
    # identity.
    signer_service_account: str = ""

    # SECURITY: database transport mode, restricted to the modes that cannot negotiate
    # plaintext - the connection was created with no TLS setting at all. disable, allow
    # and prefer are refused here, before database.py hands the value to the driver.
    db_sslmode: DatabaseSslMode = "require"

    # SECURITY: selects the server-side token verification path - client-asserted identity
    # was never verified
    # Constrained to the two supported values: security.py treats anything that is not
    # "legacy_jwt" as the Firebase path, so an unconstrained string let a misspelled rollback
    # mode silently keep verifying Firebase tokens and lock legacy callers out.
    auth_token_verifier: Literal["firebase", "legacy_jwt"] = "firebase"

    # SECURITY: authentication enforcement switch - previously no route required a valid token
    auth_enforcement_enabled: bool = True

    # SECURITY: request throttling switch - previously no route was rate limited
    rate_limit_enabled: bool = True

    # SECURITY: global per-client request ceiling - request volume was unbounded
    rate_limit_default: str = "600/minute"

    # SECURITY: tighter ceiling for mutating methods - write volume was unbounded
    rate_limit_write: str = "120/minute"

    # SECURITY: shared throttling window store - per-process counters let a client's
    # effective quota multiply by the number of workers and pods serving the API
    # Empty derives the store from REDIS_URL. Set "memory://" to count in process memory.
    rate_limit_storage_uri: str = ""

    # SECURITY: the proxies whose forwarded client address is trusted, as addresses or CIDR
    # networks - a forwarded header was previously trusted on the strength of the chain's
    # length alone, so a caller reaching the API without the expected proxy in front of it
    # could forge and rotate the identity its quota is counted against
    # Empty trusts no forwarded header at all and meters on the socket peer address. A
    # forwarded chain is read only when the socket peer itself matches an entry here.
    rate_limit_trusted_proxies: List[str] = []

    # SECURITY: Content-Security-Policy enforcement mode - no CSP was emitted on any response
    csp_report_only: bool = False

    # SECURITY: restricts token verification to a single Firebase project - the issuer was
    # unconstrained
    firebase_project_id: str = ""

    # SECURITY: rejects an address that cannot name the signer Terraform creates - a
    # malformed value surfaced only when the first object signature was attempted
    # allow_reuse keeps this module re-importable in one process, which Pydantic v1
    # otherwise refuses with a duplicate-validator ConfigError.
    @validator("signer_service_account", allow_reuse=True)
    def _signer_must_be_a_service_account_address(cls, value: str) -> str:
        """Return ``value`` unchanged once confirmed to be a service account address.

        Raises:
            ValueError: if ``value`` is non-empty and is not a Google service account
                address of the form ``name@project.iam.gserviceaccount.com``.
        """
        if value and not SERVICE_ACCOUNT_PATTERN.match(value):
            raise ValueError(
                "signer_service_account must be a Google service account address of the "
                "form name@project.iam.gserviceaccount.com, matching the Terraform "
                "signer_service_account variable"
            )
        return value

    # SECURITY: rejects an entry that is not an address or network - an unparseable entry
    # would silently never match, so the socket peer would be metered while the deployment
    # believed its proxy was trusted
    @validator("rate_limit_trusted_proxies", allow_reuse=True)
    def _validate_trusted_proxies(cls, proxies: List[str]) -> List[str]:
        """Return ``proxies`` as canonical CIDR networks, deduplicated in order.

        Each entry is a single IPv4 or IPv6 address, or a CIDR network. A bare address is
        normalised to its single-host network so that matching is one operation either way.

        Raises:
            ValueError: if any entry is empty or is neither an address nor a network.
        """
        validated: List[str] = []
        for proxy in proxies:
            candidate = proxy.strip()
            if not candidate:
                raise ValueError(
                    "rate_limit_trusted_proxies entries must not be empty"
                )
            try:
                network = str(ip_network(candidate, strict=False))
            except ValueError as exc:
                raise ValueError(
                    "rate_limit_trusted_proxies entries must be an IP address or a CIDR "
                    f"network such as 10.0.0.0/8; rejected: {proxy!r} ({exc})"
                ) from exc
            if network not in validated:
                validated.append(network)
        return validated

    # SECURITY: rejects wildcard, plaintext and non-origin values - a credentialed CORS
    # policy was built from an unvalidated list
    @validator("ALLOWED_ORIGINS", allow_reuse=True)
    def _validate_allowed_origins(cls, origins: List[str]) -> List[str]:
        """Return ``origins`` as exact browser origins, deduplicated in order.

        Each entry must be a scheme-qualified origin and nothing more: a scheme of ``http``
        or ``https``, a host, an optional port, and no userinfo, path, query or fragment.
        ``http`` is accepted only for a loopback host, so a development origin stays usable
        while a deployment origin must be encrypted. ``*`` and any other wildcard are
        rejected outright.

        Raises:
            ValueError: if any entry is not an exact origin, is a wildcard, or is a
                non-loopback plaintext origin.
        """
        validated: List[str] = []
        for origin in origins:
            candidate = origin.strip()
            if not candidate:
                raise ValueError("ALLOWED_ORIGINS entries must not be empty")
            if "*" in candidate:
                raise ValueError(
                    f"ALLOWED_ORIGINS must list exact origins; wildcard rejected: {origin!r}"
                )
            parts = urlsplit(candidate)
            if parts.scheme not in ORIGIN_SCHEMES:
                raise ValueError(
                    "ALLOWED_ORIGINS entries must start with http:// or https://; "
                    f"rejected: {origin!r}"
                )
            if not parts.netloc:
                raise ValueError(
                    f"ALLOWED_ORIGINS entries must name a host; rejected: {origin!r}"
                )
            if parts.path or parts.query or parts.fragment:
                raise ValueError(
                    "ALLOWED_ORIGINS entries must carry no path, query or fragment; "
                    f"rejected: {origin!r}"
                )
            if "@" in parts.netloc:
                raise ValueError(
                    f"ALLOWED_ORIGINS entries must carry no credentials; rejected: {origin!r}"
                )
            if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
                raise ValueError(
                    "ALLOWED_ORIGINS may only use http:// for a loopback host; "
                    f"rejected: {origin!r}"
                )
            if candidate not in validated:
                validated.append(candidate)
        return validated

    # HUMAN ASSISTANCE NEEDED
    # The following constructor implementation may need review and adjustment for production readiness
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # Retrieve secrets
        project_path = f"projects/{self.PROJECT_ID}"
        
        # Connect to Google Cloud Secret Manager. The client owns a gRPC channel and the
        # sockets beneath it, and this constructor runs once per get_settings() call, so the
        # channel is released when the three reads below return rather than being left for
        # the garbage collector. The reads themselves are unchanged and uncached.
        with secretmanager.SecretManagerServiceClient() as client:
            database_url_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/DATABASE_URL/versions/latest"})
            redis_url_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/REDIS_URL/versions/latest"})
            secret_key_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/SECRET_KEY/versions/latest"})
        
        # Set retrieved values to class properties
        self.DATABASE_URL = database_url_secret.payload.data.decode("UTF-8")
        self.REDIS_URL = redis_url_secret.payload.data.decode("UTF-8")
        self.SECRET_KEY = secret_key_secret.payload.data.decode("UTF-8")

def get_settings() -> Settings:
    return Settings()