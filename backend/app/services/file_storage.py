from google.cloud import storage
from backend.app.core.config import get_settings

class FileStorageService:
    _client: storage.Client
    _bucket: storage.Bucket

    def __init__(self):
        self._client = storage.Client()
        bucket_name = get_settings().gcs_bucket_name
        self._bucket = self._client.bucket(bucket_name)

    def upload_file(self, file_content: bytes, file_name: str) -> str:
        blob = self._bucket.blob(file_name)
        blob.upload_from_string(file_content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        blob.make_public()
        return blob.public_url

    def download_file(self, file_name: str) -> bytes:
        blob = self._bucket.blob(file_name)
        return blob.download_as_bytes()

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