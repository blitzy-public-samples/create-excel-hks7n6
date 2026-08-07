from pydantic import BaseSettings
from google.cloud import secretmanager
from typing import List

class Settings(BaseSettings):
    PROJECT_ID: str
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int

    # SECURITY: explicit CORS origin allow-list - main.py read this field before it was
    # declared, so the origin policy had no resolvable value
    ALLOWED_ORIGINS: List[str] = []

    # SECURITY: names the private uploads bucket - file_storage.py read this field before
    # it was declared
    gcs_bucket_name: str = ""

    # SECURITY: bounds the lifetime of every object-access grant - upload URLs were public
    # and permanent
    signed_url_expiry_minutes: int = 15

    # SECURITY: database transport mode - the connection was created with no TLS setting
    db_sslmode: str = "require"

    # SECURITY: selects the server-side token verification path - client-asserted identity
    # was never verified
    auth_token_verifier: str = "firebase"

    # SECURITY: authentication enforcement switch - previously no route required a valid token
    auth_enforcement_enabled: bool = True

    # SECURITY: request throttling switch - previously no route was rate limited
    rate_limit_enabled: bool = True

    # SECURITY: global per-client request ceiling - request volume was unbounded
    rate_limit_default: str = "600/minute"

    # SECURITY: tighter ceiling for mutating methods - write volume was unbounded
    rate_limit_write: str = "120/minute"

    # SECURITY: Content-Security-Policy enforcement mode - no CSP was emitted on any response
    csp_report_only: bool = False

    # SECURITY: restricts token verification to a single Firebase project - the issuer was
    # unconstrained
    firebase_project_id: str = ""

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