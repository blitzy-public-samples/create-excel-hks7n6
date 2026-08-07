import logging
from datetime import timedelta
from typing import Dict, Optional

import google.auth
import google.auth.transport.requests
from google.auth import credentials as google_credentials
from google.cloud import storage

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

# Scope requested for the credentials used to sign URLs. Cloud Storage's own scopes do not
# authorize the IAM Credentials signBlob endpoint that signing falls back to when the runtime
# holds no private key.
IAM_SIGNING_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SIGNING_SCOPES = (IAM_SIGNING_SCOPE,)

# Placeholder the metadata server uses before an attached identity has been resolved.
_UNRESOLVED_SERVICE_ACCOUNT = "default"


class FileStorageService:
    _client: storage.Client
    _bucket: storage.Bucket

    def __init__(self):
        # One Settings instance per service instance: every construction performs live Secret
        # Manager reads, so all three values it supplies are read once and retained here.
        settings = get_settings()
        self._client = storage.Client()
        bucket_name = settings.gcs_bucket_name
        self._bucket = self._client.bucket(bucket_name)
        self._signed_url_expiration = timedelta(minutes=settings.signed_url_expiry_minutes)
        self._configured_signer = settings.signer_service_account
        self._signing_credentials: Optional[google_credentials.Credentials] = None
        self._auth_request: Optional[google.auth.transport.requests.Request] = None

    def upload_file(self, file_content: bytes, file_name: str) -> str:
        # Resolved before the object is written so a credential fault cannot leave an
        # unreachable object behind.
        signing_arguments = self._signing_arguments()
        blob = self._bucket.blob(file_name)
        blob.upload_from_string(file_content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        try:
            # SECURITY: no public ACL is set - every uploaded object was world-readable by a
            # permanent URL; access is now a time-limited signed URL bound to the generation
            # that was just written, so it cannot resolve to a later overwrite
            return blob.generate_signed_url(
                version="v4",
                method="GET",
                expiration=self._signed_url_expiration,
                generation=blob.generation,
                **signing_arguments,
            )
        except Exception:
            # SECURITY: an object that could not be signed is removed - it would otherwise
            # remain stored and unreachable, accumulating for as long as signing stays broken
            self._delete_uploaded_generation(blob)
            raise

    def download_file(self, file_name: str) -> bytes:
        blob = self._bucket.blob(file_name)
        return blob.download_as_bytes()

    def _signing_arguments(self) -> Dict[str, str]:
        """Return the arguments ``generate_signed_url`` needs to sign in this runtime.

        Empty when the ambient credentials carry private key material and can sign
        locally, which is the case for a service-account key file used in local
        development. Otherwise the signer's service-account email and a live access
        token, which routes signing through the IAM Credentials signBlob API. That is
        the path taken on GKE and every other Google runtime, where the attached
        identity holds a token and no key, and it requires no key file in the image.

        The signer is ``Settings.signer_service_account`` when configured, which is the
        account the Terraform ``signer_service_account`` variable grants
        ``roles/iam.serviceAccountTokenCreator`` to. With it unset the runtime's own
        attached identity signs, and its address is read from the credentials.

        Raises:
            RuntimeError: if the ambient credentials can neither sign locally nor name
                a service account to sign on behalf of, because no signed URL can be
                produced in that case and a caller must not receive an unsigned one.
        """
        credentials = self._ambient_credentials()
        if not self._configured_signer and isinstance(
            credentials, google_credentials.Signing
        ):
            # A local key signs without an access token, so none is minted.
            return {}

        # The IAM signBlob call authenticates with an access token, so one must be live.
        # Refreshing is also what populates the service-account email on a runtime that
        # reads it from the metadata server.
        if not credentials.valid:
            credentials.refresh(self._auth_request)

        signer_email = self._configured_signer or getattr(
            credentials, "service_account_email", None
        )
        if not signer_email or signer_email == _UNRESOLVED_SERVICE_ACCOUNT:
            raise RuntimeError(
                "Application Default Credentials of type "
                f"{type(credentials).__name__} can neither sign locally nor name a "
                "service account to sign through IAM signBlob. Attach a service "
                "account to the runtime, set Settings.signer_service_account to the "
                "dedicated signer account, or grant the runtime identity "
                "roles/iam.serviceAccountTokenCreator on that account."
            )
        return {"service_account_email": signer_email, "access_token": credentials.token}

    def _ambient_credentials(self) -> google_credentials.Credentials:
        """Return the Application Default Credentials, resolving them on first use."""
        if self._signing_credentials is None:
            self._signing_credentials, _ = google.auth.default(scopes=list(SIGNING_SCOPES))
            self._auth_request = google.auth.transport.requests.Request()
        return self._signing_credentials

    def _delete_uploaded_generation(self, blob: storage.Blob) -> None:
        """Delete exactly the generation ``blob`` names, never raising.

        Matching on the generation means a newer object written under the same name by
        another caller is left alone. A failure to clean up is logged rather than
        raised, so it cannot replace the error that triggered it.
        """
        try:
            blob.delete(if_generation_match=blob.generation)
        except Exception:
            logger.exception(
                "Could not delete unsigned upload %s generation %s; it remains stored",
                blob.name,
                blob.generation,
            )

# HUMAN ASSISTANCE NEEDED
# The following improvements may be needed for production readiness:
# 1. Error handling for file operations (e.g., file not found, permission issues)
# 2. Logging for important operations and errors
# 3. Implement retry logic for network-related operations
# 4. Add type hints for better code maintainability
# 5. Implement caching mechanism for frequently accessed files
# 6. Add methods for listing files, deleting files, and checking file existence
# 7. Implement proper authentication and authorization checks
# 8. Add support for different file types, not just Excel files
# 9. Implement file compression/decompression if needed
# 10. Add support for concurrent uploads/downloads for better performance