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
BROWSER_ORIGIN_PATTERN = re.compile(
    r"^https?://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$"
)

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

# Transport modes that guarantee an encrypted PostgreSQL connection. disable, allow
# and prefer are excluded because each of them can negotiate plaintext.
DatabaseSslMode = Literal["require", "verify-ca", "verify-full"]


class Settings(BaseSettings):
    PROJECT_ID: str
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int

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
    auth_token_verifier: str = "firebase"

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

    # allow_reuse keeps this module re-importable in one process, which Pydantic v1
    # otherwise refuses with a duplicate-validator ConfigError.
    @validator("api_origin", allow_reuse=True)
    def _api_origin_must_be_an_exact_origin(cls, value: str) -> str:
        if value and not BROWSER_ORIGIN_PATTERN.match(value):
            raise ValueError(
                "api_origin must be an exact lower-case browser origin such as "
                "https://api.example.com, with no trailing slash, path, query, fragment, "
                "userinfo or whitespace"
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