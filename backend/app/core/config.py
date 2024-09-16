from pydantic import BaseSettings
from google.cloud import secretmanager

class Settings(BaseSettings):
    PROJECT_ID: str
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int

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