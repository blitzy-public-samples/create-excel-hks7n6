from pydantic import BaseSettings, conint, validator
from google.cloud import secretmanager
from typing import List, Literal
from pathlib import Path
from urllib.parse import urlsplit
import re
from dotenv import load_dotenv

# Seven days expressed in minutes: the longest lifetime the Cloud Storage V4 signing
# scheme accepts for a signed URL.
MAX_SIGNED_URL_EXPIRY_MINUTES = 10080

# An exact browser origin: scheme, host and optional port, and nothing else. Rejects a
# trailing slash, path, query, fragment, userinfo and embedded whitespace, none of which a
# browser ever sends in an Origin header or matches in a Content-Security-Policy source.
# The host is lower-case ASCII, because a browser lower-cases the host it sends and
# converts an internationalised name to punycode, and Starlette compares an Origin header
# to this list as an exact string. Each dot-separated label starts and ends alphanumeric,
# so an empty label, a leading or trailing hyphen, an underscore and a trailing dot are
# all refused. The bracketed IPv6 loopback is the one non-DNS host form accepted.
# This is the single grammar the infrastructure/terraform/variables.tf allowed_origins
# variable mirrors.
BROWSER_ORIGIN_PATTERN = re.compile(
    r"^https?://"
    r"(?:\[::1\]|[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)"
    r"(?::[0-9]{1,5})?$"
)

# Characters a Content-Security-Policy report collector URL may not carry. `;` separates
# CSP directives, `,` separates Reporting-Endpoints members and `"` closes the quoted
# endpoint value, so any of the three lets a configured value escape the field it is
# written into and become policy of its own.
CSP_REPORT_URI_FORBIDDEN_CHARACTERS = frozenset(';,"')

# A header value may carry only printable US-ASCII. This range excludes space, tab, CR,
# LF, every other control character and every non-ASCII character, so a homoglyph host
# and a header-splitting newline are both refused.
PRINTABLE_ASCII_RANGE = range(0x21, 0x7F)

# The scheme a report collector must use. A violation report describes a security
# failure and is itself sent over the network.
CSP_REPORT_URI_SCHEME = "https"

# Repository-root .env, the file .env.example is copied to.
DOTENV_PATH = Path(__file__).resolve().parents[3] / ".env"

# SECURITY: the documented .env is loaded into the process environment here — the
# settings and Google credential variables it declares were previously never read.
# Existing environment variables take precedence over the file.
load_dotenv(DOTENV_PATH, override=False)

# Origins that may use plaintext http. Every other origin must use https.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

# The only schemes a browser origin may carry.
ORIGIN_SCHEMES = frozenset({"http", "https"})

# The assignable TCP port range. Port 0 and any number above 65535 cannot be reached, so
# a value outside this range names an endpoint no client can connect to.
MIN_TCP_PORT = 1
MAX_TCP_PORT = 65535

# Transport modes that guarantee an encrypted PostgreSQL connection. disable, allow
# and prefer are excluded because each of them can negotiate plaintext.
DatabaseSslMode = Literal["require", "verify-ca", "verify-full"]

# The server-side token verification paths that exist. A value outside this set names no
# verifier, so it is refused rather than silently selecting nothing.
AuthTokenVerifier = Literal["firebase", "legacy_jwt"]

# A user-managed service account address. Mirrors the signer_service_account validation
# in infrastructure/terraform/variables.tf, so the identity granted the signing role
# there and the identity the application names are spelled the same way. The default
# compute, App Engine and Google-managed addresses do not match this shape.
USER_MANAGED_SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]"
    r"\.iam\.gserviceaccount\.com$"
)

# Cloud Storage bucket naming, in the subset that is decidable locally: lower-case
# letters, numbers, hyphens, underscores and dots, starting and ending alphanumeric.
# The service remains authoritative for the rest of the rule set.
GCS_BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")
GCS_BUCKET_NAME_MIN_LENGTH = 3
GCS_BUCKET_NAME_MAX_LENGTH = 63
GCS_DOTTED_BUCKET_NAME_MAX_LENGTH = 222

# A dotted-decimal address. A bucket name may not be one, because that spelling is
# reserved for addressing the service itself.
IPV4_ADDRESS_PATTERN = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")

# The shortest token lifetime that can be used. A zero or negative lifetime mints a
# token that has already expired.
MIN_ACCESS_TOKEN_EXPIRE_MINUTES = 1


def _reject_unassignable_port(value: str, field_name: str) -> None:
    """Raise if the authority of ``value`` carries a port outside 1..65535.

    ``urlsplit(...).port`` raises :class:`ValueError` itself for a number above 65535, so
    the access is guarded to keep the message on the field rather than on the parser.
    """
    try:
        port = urlsplit(value).port
    except ValueError as exc:
        raise ValueError(
            f"{field_name} port must be between {MIN_TCP_PORT} and {MAX_TCP_PORT}; "
            f"rejected: {value!r}"
        ) from exc
    if port is not None and not MIN_TCP_PORT <= port <= MAX_TCP_PORT:
        raise ValueError(
            f"{field_name} port must be between {MIN_TCP_PORT} and {MAX_TCP_PORT}; "
            f"rejected: {value!r}"
        )


def _reject_unless_exact_browser_origin(value: str, field_name: str) -> None:
    """Raise unless ``value`` is an exact, encrypted-or-loopback browser origin.

    One grammar for every origin-typed setting: the shape
    :data:`BROWSER_ORIGIN_PATTERN` describes, an assignable port, and ``http`` only for a
    loopback host so a development origin stays usable while a deployment origin must be
    encrypted.

    Raises:
        ValueError: if the value is not an exact lower-case origin, carries an
            unassignable port, or is a non-loopback plaintext origin.
    """
    if not BROWSER_ORIGIN_PATTERN.match(value):
        raise ValueError(
            f"{field_name} must be an exact lower-case browser origin such as "
            "https://app.example.com: scheme, host and optional port only, with no "
            "trailing slash, path, query, fragment, userinfo, upper-case or non-ASCII "
            "character, underscore, empty or hyphen-edged host label, trailing dot or "
            f"whitespace; rejected: {value!r}"
        )
    _reject_unassignable_port(value, field_name)
    parts = urlsplit(value)
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError(
            f"{field_name} may only use http:// for a loopback host "
            f"({', '.join(sorted(LOOPBACK_HOSTS))}); rejected: {value!r}"
        )


class Settings(BaseSettings):
    PROJECT_ID: str
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str
    ALGORITHM: str
    # SECURITY: refuses a token lifetime that is already expired — a non-positive value
    # was accepted and produced tokens no caller could use
    ACCESS_TOKEN_EXPIRE_MINUTES: conint(ge=MIN_ACCESS_TOKEN_EXPIRE_MINUTES)

    # SECURITY: explicit CORS origin allow-list — the origin policy main.py reads was undeclared and unresolvable
    ALLOWED_ORIGINS: List[str] = []

    # SECURITY: identify the private uploads bucket used for signed object access
    gcs_bucket_name: str = ""

    # SECURITY: bounds the lifetime of every object-access grant — upload URLs were public and permanent
    # Constrained to 1..MAX_SIGNED_URL_EXPIRY_MINUTES: a non-positive lifetime yields an unusable
    # grant and Cloud Storage rejects a V4 signature valid for longer than seven days. Rejected at
    # construction, before any storage operation runs.
    signed_url_expiry_minutes: conint(ge=1, le=MAX_SIGNED_URL_EXPIRY_MINUTES) = 15

    # SECURITY: names the dedicated service account that signs object URLs — the signing identity was
    # declared only in Terraform and never reached the application
    # Empty means the runtime's own attached identity signs. Must match the Terraform
    # signer_service_account variable so the IAM grant and the signer are one identity.
    signer_service_account: str = ""

    # SECURITY: database transport mode, restricted to modes that cannot negotiate
    # plaintext — the connection was created with no TLS setting at all
    db_sslmode: DatabaseSslMode = "require"

    # SECURITY: server-side token verification path — client-asserted identity was never verified
    # Restricted to the paths that exist, so a misspelling cannot select no verifier.
    auth_token_verifier: AuthTokenVerifier = "firebase"

    # SECURITY: authentication enforcement switch — previously no route required a valid token
    auth_enforcement_enabled: bool = True

    # SECURITY: request throttling switch — previously no route was rate limited
    rate_limit_enabled: bool = True

    # SECURITY: global per-client request ceiling — request volume was unbounded
    rate_limit_default: str = "600/minute"

    # SECURITY: tighter ceiling for mutating methods — writes were unbounded
    rate_limit_write: str = "120/minute"

    # SECURITY: shared throttling window store — per-process counters let a client's
    # effective quota multiply by the number of workers and pods serving the API
    rate_limit_storage_uri: str = "memory://"

    # SECURITY: number of trailing proxy hops whose forwarded client address is trusted;
    # 0 uses the socket address only, so a forwarded header cannot be spoofed
    rate_limit_trusted_proxy_hops: int = 0

    # SECURITY: Content-Security-Policy enforcement mode — no CSP was emitted on any response
    csp_report_only: bool = False

    # SECURITY: Content-Security-Policy violation collector — report-only mode blocks
    # nothing and, with no collector, recorded nothing either
    csp_report_uri: str = ""

    # SECURITY: restricts token verification to a single Firebase project — the issuer was unconstrained
    firebase_project_id: str = ""

    # SECURITY: admits the API to the Content-Security-Policy connect-src allow-list — the policy admitted
    # no cross-origin API, so a browser enforcing it blocked every API call
    # Empty means the same edge origin serves the SPA and the API, which 'self' already covers. Otherwise
    # this is the exact origin of the frontend's REACT_APP_API_BASE_URL.
    api_origin: str = ""

    # SECURITY: rejects a plaintext API origin — the policy admitted an unencrypted API
    # that a network position could rewrite
    # allow_reuse keeps this module re-importable in one process, which Pydantic v1
    # otherwise refuses with a duplicate-validator ConfigError.
    @validator("api_origin", allow_reuse=True)
    def _api_origin_must_be_an_exact_origin(cls, value: str) -> str:
        """Return ``value`` if it is an exact browser origin. Empty means same-origin."""
        if value:
            _reject_unless_exact_browser_origin(value, "api_origin")
        return value

    # SECURITY: rejects a collector URL that could rewrite the Content-Security-Policy or
    # split the response header — the value was written into both headers unchecked
    @validator("csp_report_uri", allow_reuse=True)
    def _csp_report_uri_must_be_a_safe_https_url(cls, value: str) -> str:
        """Return ``value`` if it is an absolute https URL safe to embed in a header.

        A collector legitimately carries a path and a query string, so this is a URL
        grammar rather than the origin grammar ``api_origin`` uses. Every character must
        be printable US-ASCII and none may be ``;``, ``,`` or ``"``. The scheme must be
        ``https``, a host is required, and userinfo and a fragment are refused. An empty
        value means no collector and is accepted.

        Raises:
            ValueError: if the value carries a forbidden or non-printable character, is
                not an absolute https URL, names no host, or carries userinfo, a fragment
                or an unassignable port.
        """
        if not value:
            return value
        for character in value:
            if (
                ord(character) not in PRINTABLE_ASCII_RANGE
                or character in CSP_REPORT_URI_FORBIDDEN_CHARACTERS
            ):
                raise ValueError(
                    "csp_report_uri must carry only printable ASCII and none of "
                    '; , " because whitespace, a line break and a directive separator '
                    "would alter the response headers it is written into; "
                    f"rejected: {value!r}"
                )
        parts = urlsplit(value)
        if parts.scheme != CSP_REPORT_URI_SCHEME:
            raise ValueError(
                "csp_report_uri must be an absolute "
                f"{CSP_REPORT_URI_SCHEME}:// URL such as "
                f"https://csp.example.com/report; rejected: {value!r}"
            )
        if not parts.netloc:
            raise ValueError(
                f"csp_report_uri must name a host; rejected: {value!r}"
            )
        if "@" in parts.netloc:
            raise ValueError(
                f"csp_report_uri must carry no credentials; rejected: {value!r}"
            )
        if parts.fragment:
            raise ValueError(
                f"csp_report_uri must carry no fragment; rejected: {value!r}"
            )
        _reject_unassignable_port(value, "csp_report_uri")
        return value

    # SECURITY: pins the signing identity to a user-managed service account — any address
    # was accepted, including one outside the project
    @validator("signer_service_account", allow_reuse=True)
    def _signer_service_account_must_be_user_managed(cls, value: str) -> str:
        """Return ``value`` if it is a user-managed service account address.

        Empty means the runtime's own attached identity signs. Otherwise the address must
        match the grammar the Terraform ``signer_service_account`` variable enforces, so
        the account granted the signing role and the account the application names cannot
        drift apart.

        Raises:
            ValueError: if the value is not of the form
                ``NAME@PROJECT_ID.iam.gserviceaccount.com``.
        """
        if value and not USER_MANAGED_SERVICE_ACCOUNT_PATTERN.match(value):
            raise ValueError(
                "signer_service_account must be a user-managed service account address "
                "of the form NAME@PROJECT_ID.iam.gserviceaccount.com, matching the "
                "Terraform signer_service_account variable; a personal address, a "
                "default compute or App Engine address and an upper-case address are "
                f"rejected: {value!r}"
            )
        return value

    # SECURITY: rejects a bucket name that is not a bucket name — an arbitrary string,
    # including a traversal sequence, reached the storage client unchecked
    @validator("gcs_bucket_name", allow_reuse=True)
    def _gcs_bucket_name_must_be_well_formed(cls, value: str) -> str:
        """Return ``value`` if it is a syntactically valid Cloud Storage bucket name.

        Empty means no bucket is configured. Otherwise: 3 to 63 characters, or up to 222
        when dot-separated with every component at most 63; lower-case letters, numbers,
        hyphens, underscores and dots only; starting and ending alphanumeric; no
        consecutive dots; and not a dotted-decimal address. Cloud Storage remains
        authoritative for the rest of its naming rules.

        Raises:
            ValueError: if the value breaks any of those rules.
        """
        if not value:
            return value
        dotted = "." in value
        limit = (
            GCS_DOTTED_BUCKET_NAME_MAX_LENGTH if dotted else GCS_BUCKET_NAME_MAX_LENGTH
        )
        if not GCS_BUCKET_NAME_MIN_LENGTH <= len(value) <= limit:
            raise ValueError(
                f"gcs_bucket_name must be {GCS_BUCKET_NAME_MIN_LENGTH} to {limit} "
                f"characters; rejected: {value!r}"
            )
        if not GCS_BUCKET_NAME_PATTERN.match(value):
            raise ValueError(
                "gcs_bucket_name must use only lower-case letters, numbers, hyphens, "
                "underscores and dots, and start and end with a letter or number; "
                f"rejected: {value!r}"
            )
        if ".." in value:
            raise ValueError(
                f"gcs_bucket_name must carry no consecutive dots; rejected: {value!r}"
            )
        if IPV4_ADDRESS_PATTERN.match(value):
            raise ValueError(
                "gcs_bucket_name must not be a dotted-decimal address; "
                f"rejected: {value!r}"
            )
        for component in value.split("."):
            if len(component) > GCS_BUCKET_NAME_MAX_LENGTH:
                raise ValueError(
                    "gcs_bucket_name dot-separated components must each be at most "
                    f"{GCS_BUCKET_NAME_MAX_LENGTH} characters; rejected: {value!r}"
                )
        return value

    # SECURITY: rejects wildcard, plaintext and non-origin values — a credentialed CORS
    # policy was built from an unvalidated list
    @validator("ALLOWED_ORIGINS", allow_reuse=True)
    def _validate_allowed_origins(cls, origins: List[str]) -> List[str]:
        """Return ``origins`` as exact browser origins, deduplicated in order.

        Each entry must be a scheme-qualified origin and nothing more: a scheme of
        ``http`` or ``https``, a host, an optional port, and no userinfo, path, query
        or fragment. ``http`` is accepted only for a loopback host, so a development
        origin stays usable while a deployment origin must be encrypted. ``*`` and any
        other wildcard are rejected outright.

        Surrounding whitespace is trimmed before an entry is validated, and that is the
        one respect in which this accepts a string the mirroring Terraform variable
        rejects. Everything else is decided by
        :func:`_reject_unless_exact_browser_origin`, the single grammar both enforce.

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
            _reject_unless_exact_browser_origin(candidate, "ALLOWED_ORIGINS")
            if candidate not in validated:
                validated.append(candidate)
        return validated

    # HUMAN ASSISTANCE NEEDED
    # The following constructor implementation may need review and adjustment for production readiness
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # Connect to Google Cloud Secret Manager
        client = secretmanager.SecretManagerServiceClient()
        
        # Retrieve secrets
        project_path = f"projects/{self.PROJECT_ID}"
        
        database_url_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/DATABASE_URL/versions/latest"})
        redis_url_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/REDIS_URL/versions/latest"})
        secret_key_secret = client.access_secret_version(request={"name": f"{project_path}/secrets/SECRET_KEY/versions/latest"})
        
        # Set retrieved values to class properties
        self.DATABASE_URL = database_url_secret.payload.data.decode("UTF-8")
        self.REDIS_URL = redis_url_secret.payload.data.decode("UTF-8")
        self.SECRET_KEY = secret_key_secret.payload.data.decode("UTF-8")

def get_settings() -> Settings:
    return Settings()