import hashlib
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

import google.auth
import google.auth.transport.requests
from google.auth import credentials as google_credentials
from google.cloud import storage

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

# Characters of the object-name digest recorded in logs. Sixteen hex characters of a SHA-256
# digest, which carries none of the name itself.
_OBJECT_LOG_DIGEST_LENGTH = 16


def _object_log_identifier(object_name: Optional[str]) -> str:
    """Return a stable, log-safe identifier for ``object_name``.

    The same name always yields the same identifier, so records about one object correlate,
    while the name itself - which the caller supplies and which may carry personal data or
    control characters - never reaches the log.

    Args:
        object_name: The Cloud Storage object name, or ``None`` if the blob has none.

    Returns:
        str: ``sha256:`` followed by the first :data:`_OBJECT_LOG_DIGEST_LENGTH` hex
            characters of the name's SHA-256 digest, or ``"unnamed"`` when there is no name.
    """
    if not object_name:
        return "unnamed"
    digest = hashlib.sha256(object_name.encode("utf-8", "surrogatepass")).hexdigest()
    return "sha256:" + digest[:_OBJECT_LOG_DIGEST_LENGTH]

# Scope requested for the credentials used to sign URLs. Cloud Storage's own scopes do not
# authorize the IAM Credentials signBlob endpoint that signing falls back to when the runtime
# holds no private key.
IAM_SIGNING_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SIGNING_SCOPES = (IAM_SIGNING_SCOPE,)

# Placeholder the metadata server uses before an attached identity has been resolved.
_UNRESOLVED_SERVICE_ACCOUNT = "default"

# Largest object, in bytes, this service will write or read. Both operations hold the whole
# object in memory, so this value bounds how much of a worker's memory one call can consume.
MAX_OBJECT_BYTES = 50 * 1024 * 1024


class FileStorageService:
    """Reads and writes workbook objects in the private uploads bucket.

    The instance owns a Cloud Storage client, which holds an HTTP connection pool. Release it
    with :meth:`close`, or use the instance as a context manager, which closes it on the way
    out. An instance that is not closed leaks its pool's sockets until the garbage collector
    reaches it.
    """

    _client: storage.Client
    _bucket: storage.Bucket

    def __init__(self):
        # One Settings instance per service instance: every construction performs live Secret
        # Manager reads, so all four values it supplies are read once and retained here.
        settings = get_settings()
        self._client = storage.Client()
        bucket_name = settings.gcs_bucket_name
        self._bucket = self._client.bucket(bucket_name)
        self._signed_url_expiration = timedelta(minutes=settings.signed_url_expiry_minutes)
        # SECURITY: the size ceiling every read and write is measured against. Held per
        # instance so a caller can narrow it, and defaulted from the module constant.
        self._max_object_bytes = MAX_OBJECT_BYTES
        self._signing_credentials: Optional[google_credentials.Credentials] = None
        self._auth_request: Optional[google.auth.transport.requests.Request] = None

    def close(self) -> None:
        """Release the Cloud Storage client's connection pool.

        Safe to call more than once, and safe to call on an instance whose client never
        opened a connection.
        """
        try:
            self._client.close()
        except Exception:
            logger.exception("Could not close the Cloud Storage client cleanly")

    def __enter__(self) -> "FileStorageService":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False

    def upload_file(self, file_content: bytes, file_name: str) -> str:
        # SECURITY: an object over MAX_OBJECT_BYTES is refused before anything is written, so
        # the bytes this method holds in memory are bounded.
        self._require_within_size_limit(len(file_content), file_name)
        # Resolved before the object is written so a credential fault cannot leave an
        # unreachable object behind.
        signing_arguments = self._signing_arguments()
        blob = self._bucket.blob(file_name)
        blob.upload_from_string(file_content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        try:
            # SECURITY: no public ACL is set. Access is a time-limited signed URL bound to
            # the generation this call wrote, so it cannot resolve to a later overwrite.
            return blob.generate_signed_url(
                version="v4",
                method="GET",
                expiration=self._signed_url_expiration,
                generation=blob.generation,
                **signing_arguments,
            )
        except Exception:
            # SECURITY: an object that cannot be signed is removed rather than left stored
            # and unreachable.
            self._delete_uploaded_generation(blob)
            raise

    def download_file(self, file_name: str) -> bytes:
        blob = self._bucket.blob(file_name)
        # SECURITY: the object's declared size is read first and refused if it is over the
        # maximum, so an oversized object is never pulled into memory. reload() fetches
        # metadata only; a missing object raises here rather than after a large transfer.
        blob.reload()
        self._require_within_size_limit(blob.size, file_name)
        return blob.download_as_bytes()

    def _require_within_size_limit(self, size: Optional[int], file_name: str) -> None:
        """Raise if ``size`` is over this instance's ceiling, :data:`MAX_OBJECT_BYTES`.

        Args:
            size: The object's size in bytes. ``None`` means Cloud Storage reported no size,
                which is not treated as within the limit.
            file_name: Identified in the error by a digest of its name rather than by the
                name itself.

        Raises:
            ValueError: if the size is unknown or over the maximum.
        """
        # SECURITY: the object is identified by a digest of its name, never by the name -
        # the name comes from the caller, and this exception's text reaches the caller
        # through the route handlers that answer with str(e), so a workbook title carrying
        # another user's data, or a guessed name confirmed by the wording of the refusal,
        # was disclosed back over the API (CWE-209, CWE-532)
        identifier = _object_log_identifier(file_name)
        if size is None:
            raise ValueError(
                "Cloud Storage reported no size for object {0}, so it cannot be "
                "confirmed to be within the {1}-byte maximum".format(
                    identifier, self._max_object_bytes
                )
            )
        if size > self._max_object_bytes:
            raise ValueError(
                "Object {0} is {1} bytes, over the {2}-byte maximum this service "
                "reads and writes".format(identifier, size, self._max_object_bytes)
            )

    def _signing_arguments(self) -> Dict[str, str]:
        """Return the arguments ``generate_signed_url`` needs to sign in this runtime.

        Empty when the ambient credentials carry private key material and can sign
        locally, which is the case for a service-account key file used in local
        development. Otherwise the signer's service-account email and a live access
        token, which routes signing through the IAM Credentials signBlob API. That is
        the path taken on GKE and every other Google runtime, where the attached
        identity holds a token and no key, and it requires no key file in the image.

        The runtime signs as ITSELF: the address is read from the ambient credentials,
        never from configuration. ``infrastructure/terraform`` creates one service account
        that is both the API runtime identity and the signer, and grants it
        ``roles/iam.serviceAccountTokenCreator`` on itself, which is the
        ``iam.serviceAccounts.signBlob`` permission this call needs.

        Raises:
            RuntimeError: if the ambient credentials can neither sign locally nor name
                a service account to sign on behalf of, because no signed URL can be
                produced in that case and a caller must not receive an unsigned one.
        """
        credentials = self._ambient_credentials()
        if isinstance(credentials, google_credentials.Signing):
            # A local key signs without an access token, so none is minted.
            return {}

        # The IAM signBlob call authenticates with an access token, so one must be live.
        # Refreshing is also what populates the service-account email on a runtime that
        # reads it from the metadata server.
        if not credentials.valid:
            credentials.refresh(self._auth_request)

        signer_email = getattr(credentials, "service_account_email", None)
        if not signer_email or signer_email == _UNRESOLVED_SERVICE_ACCOUNT:
            raise RuntimeError(
                "Application Default Credentials of type "
                f"{type(credentials).__name__} can neither sign locally nor name a "
                "service account to sign through IAM signBlob. Attach the service "
                "account infrastructure/terraform creates to the runtime, and confirm it "
                "holds roles/iam.serviceAccountTokenCreator on itself."
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
            # SECURITY: the object is identified by a digest of its name, never by the name
            # itself. The name comes from the caller, so logging it verbatim would publish
            # workbook-title content into the log and let a name carrying control characters
            # forge or split log lines (CWE-117, CWE-532).
            logger.exception(
                "Could not delete unsigned upload %s generation %s; it remains stored",
                _object_log_identifier(blob.name),
                blob.generation,
            )