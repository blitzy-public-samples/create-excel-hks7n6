# Main Terraform configuration file for provisioning Google Cloud resources

# Provider configuration for Google Cloud
provider "google" {
  project = var.project_id
  region  = var.region
}

# Resource definitions for Google Cloud SQL
resource "google_sql_database_instance" "main" {
  name             = "main-instance"
  database_version = "POSTGRES_13"
  region           = var.region

  settings {
    tier = "db-f1-micro"

    # SECURITY: the instance refuses unencrypted connections; it previously accepted them
    ip_configuration {
      ssl_mode = "ENCRYPTED_ONLY"
    }
  }
}

resource "google_sql_database" "database" {
  name     = "main-database"
  instance = google_sql_database_instance.main.name
}

# Resource definitions for Google Cloud Storage buckets
resource "google_storage_bucket" "static_assets" {
  name     = "${var.project_id}-static-assets"
  location = var.region

  # SECURITY: object ACLs are disabled; access is granted by bucket IAM only
  uniform_bucket_level_access = true
}

resource "google_storage_bucket" "user_uploads" {
  name     = "${var.project_id}-user-uploads"
  location = var.region

  # SECURITY: object ACLs are disabled and allUsers/allAuthenticatedUsers grants are
  # overridden; every uploaded object was previously world-readable by URL.
  # Apply order: the code path setting a public object ACL must be removed no later than
  # this change.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

# Signing identity for Cloud Storage V4 signed URLs
# SECURITY: uploaded objects are reached through signed URLs issued by this one identity
resource "google_service_account" "url_signer" {
  account_id   = split("@", var.signer_service_account)[0]
  display_name = "Signed URL signer for user uploads"
}

# SECURITY: signBlob is authorized on this single account, not at project level; no key is issued
resource "google_service_account_iam_member" "url_signer_token_creator" {
  service_account_id = google_service_account.url_signer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.url_signer.member
}

# HTTPS edge serving the static single-page application
# SECURITY: TLS terminates at the edge; the edge previously exposed a plaintext HTTP
# listener on port 80 and had no certificate
resource "google_compute_global_address" "excel_app_lb" {
  name = "excel-app-lb-ip"
}

resource "google_compute_managed_ssl_certificate" "excel_app" {
  name = "excel-app-ssl-cert"

  managed {
    domains = [var.domain_name]
  }
}

resource "google_compute_backend_bucket" "excel_app" {
  name        = "excel-app-backend-bucket"
  bucket_name = google_storage_bucket.static_assets.name

  # SECURITY: the canonical security header set is emitted on the static origin, which
  # previously returned no security header at all. Values are the canonical set defined in
  # backend/app/core/security_headers.py and must not diverge from it.
  custom_response_headers = [
    "Strict-Transport-Security: max-age=63072000; includeSubDomains; preload",
    "Content-Security-Policy: default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://firestore.googleapis.com https://firebaseinstallations.googleapis.com; upgrade-insecure-requests",
    "X-Frame-Options: DENY",
    "X-Content-Type-Options: nosniff",
    "Referrer-Policy: strict-origin-when-cross-origin",
    "Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
  ]
}

resource "google_compute_url_map" "excel_app" {
  name            = "excel-app-url-map"
  default_service = google_compute_backend_bucket.excel_app.self_link
}

# SECURITY: port 80 answers only with a redirect to https; it never reaches content
resource "google_compute_url_map" "excel_app_https_redirect" {
  name = "excel-app-https-redirect-url-map"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_https_proxy" "excel_app" {
  name             = "excel-app-https-proxy"
  url_map          = google_compute_url_map.excel_app.self_link
  ssl_certificates = [google_compute_managed_ssl_certificate.excel_app.self_link]
}

resource "google_compute_target_http_proxy" "excel_app_redirect" {
  name    = "excel-app-http-proxy"
  url_map = google_compute_url_map.excel_app_https_redirect.self_link
}

resource "google_compute_global_forwarding_rule" "excel_app_https" {
  name                  = "excel-app-https-forwarding-rule"
  target                = google_compute_target_https_proxy.excel_app.self_link
  ip_address            = google_compute_global_address.excel_app_lb.address
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

resource "google_compute_global_forwarding_rule" "excel_app_http" {
  name                  = "excel-app-http-forwarding-rule"
  target                = google_compute_target_http_proxy.excel_app_redirect.self_link
  ip_address            = google_compute_global_address.excel_app_lb.address
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# Resource definitions for Google Cloud Functions
resource "google_cloudfunctions_function" "example_function" {
  name        = "example-function"
  description = "An example Cloud Function"
  runtime     = "nodejs14"

  available_memory_mb   = 256
  source_archive_bucket = google_storage_bucket.static_assets.name
  source_archive_object = "function-source.zip"
  trigger_http          = true
  entry_point           = "helloWorld"
}

# Invoking identity for the Cloud Function
resource "google_service_account" "function_invoker" {
  account_id   = "excel-app-function-invoker"
  display_name = "Cloud Function invoker"
}

# SECURITY: invoking the function requires this named identity; invoker IAM was previously
# unmanaged and the function was deployed to allow unauthenticated callers
resource "google_cloudfunctions_function_iam_member" "invoker" {
  project        = google_cloudfunctions_function.example_function.project
  region         = google_cloudfunctions_function.example_function.region
  cloud_function = google_cloudfunctions_function.example_function.name
  role           = "roles/cloudfunctions.invoker"
  member         = google_service_account.function_invoker.member
}

# Resource definitions for Google Cloud Firestore
resource "google_firestore_database" "database" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
}

# Resource definitions for Google Kubernetes Engine cluster
resource "google_container_cluster" "primary" {
  name     = "primary-cluster"
  location = var.region

  remove_default_node_pool = true
  initial_node_count       = 1
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "primary-node-pool"
  location   = var.region
  cluster    = google_container_cluster.primary.name
  node_count = 3

  node_config {
    preemptible  = true
    machine_type = "e2-medium"
  }
}

# Resource definitions for Google Cloud Identity Platform
resource "google_identity_platform_config" "default" {
  project = var.project_id

  # Enable Identity Platform
  # SECURITY: sign-in origins derive from the CORS allow-list; a placeholder domain was
  # previously authorized. Each entry is the host parsed out of an origin: an authorized
  # domain carries no scheme and no port.
  authorized_domains = distinct([
    for origin in var.allowed_origins :
    regex("^https?://(?P<host>\\[[0-9a-fA-F:.]+\\]|[^:/]+)(?::[0-9]+)?$", origin).host
  ])
}

resource "google_identity_platform_oauth_idp_config" "google" {
  name          = "google.com"
  display_name  = "Google"
  client_id     = var.google_oauth_client_id
  client_secret = var.google_oauth_client_secret
  enabled       = true
}

# HUMAN ASSISTANCE NEEDED
# The following resources may need additional configuration based on specific project requirements:
# - Cloud SQL: Consider adding more configuration options like backup settings, maintenance window, etc.
# - Cloud Storage: Add lifecycle rules, IAM permissions, and other bucket configurations as needed.
# - Cloud Functions: Update the function code source, memory, and other settings based on actual function requirements.
# - Firestore: Add necessary indexes and security rules.
# - GKE: Configure autoscaling, networking, and other advanced features as per project needs.
# - Identity Platform: Add more providers and configure advanced settings like multi-factor authentication.
