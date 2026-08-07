# Main Terraform configuration file for provisioning Google Cloud resources

# SECURITY: the provider version is constrained and its selection is recorded in the
# committed .terraform.lock.hcl — an unconstrained, unlocked provider let each init select
# a different version, so the plan a reviewer approved was not the plan that applied.
# Rationale: documentation/Security Decision Log.md R7.
# Refresh the lock with, from this directory:
#   terraform providers lock -platform=linux_amd64 -platform=windows_amd64 \
#     -platform=darwin_amd64 -platform=darwin_arm64
terraform {
  # 1.9 is the floor for variable validation rules that reference other variables, which
  # the signer_service_account and runtime_service_account project checks use.
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.43"
    }
  }
}

# Provider configuration for Google Cloud
provider "google" {
  project = var.project_id
  region  = var.region
}

# Canonical HTTP security header set for the static origin.
# The values must not diverge from backend/app/core/security_headers.py, which defines the
# same set for API responses. connect-src carries one extra source here: the API origin the
# browser reaches, which only the SPA document's policy needs to admit.
# Rationale: documentation/Security Decision Log.md D9, R10.
locals {
  # SECURITY: the configured API origin is admitted to connect-src — the policy named no
  # cross-origin API, so an enforced policy blocked every API call from the SPA.
  csp_connect_src_sources = concat(
    [
      "'self'",
      "https://identitytoolkit.googleapis.com",
      "https://securetoken.googleapis.com",
      "https://firestore.googleapis.com",
      "https://firebaseinstallations.googleapis.com",
    ],
    var.api_origin == "" ? [] : [var.api_origin],
  )

  content_security_policy = join("; ", [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "style-src-elem 'self'",
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src ${join(" ", local.csp_connect_src_sources)}",
    "upgrade-insecure-requests",
  ])

  # Report-only reports violations without blocking; exactly one of the two header names
  # is ever emitted. Mirrors the backend csp_report_only setting.
  csp_header_name = var.csp_report_only ? "Content-Security-Policy-Report-Only" : "Content-Security-Policy"

  security_response_headers = [
    "Strict-Transport-Security: max-age=63072000; includeSubDomains; preload",
    "${local.csp_header_name}: ${local.content_security_policy}",
    "X-Frame-Options: DENY",
    "X-Content-Type-Options: nosniff",
    "Referrer-Policy: strict-origin-when-cross-origin",
    "Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
  ]
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
# This is the ONE bucket the compiled SPA is published to, and the origin the load balancer
# below serves. scripts/deploy.sh uploads here and creates no bucket of its own.
# Rationale: documentation/Security Decision Log.md D5, R12.
resource "google_storage_bucket" "static_assets" {
  name     = "${var.project_id}-static-assets"
  location = var.region

  # SECURITY: object ACLs are disabled; access is granted by bucket IAM only.
  # Public access prevention is deliberately NOT enforced here: this bucket serves the
  # public SPA. See Decision Log D5.
  uniform_bucket_level_access = true
}

# SECURITY: read access to the public single-page application is granted explicitly at
# bucket level. With object ACLs disabled and no such grant, the load balancer origin
# was unreadable and returned an authorization failure for every request.
resource "google_storage_bucket_iam_member" "static_assets_public_read" {
  bucket = google_storage_bucket.static_assets.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

# SECURITY: the Cloud Function source archive is held in a private bucket; it
# previously shared the bucket the load balancer serves to the internet
resource "google_storage_bucket" "function_source" {
  name     = "${var.project_id}-function-source"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

resource "google_storage_bucket" "user_uploads" {
  name     = "${var.project_id}-user-uploads"
  location = var.region

  # SECURITY: object ACLs are disabled and allUsers/allAuthenticatedUsers grants are
  # overridden; every uploaded object was previously world-readable by URL.
  # Uniform bucket-level access rejects object-ACL writes; application code must not set
  # them. Apply and revert this together with the code path that set a public object ACL.
  # See Decision Log D4.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

# Signed-URL signing identities.
# The signing call presents the RUNTIME identity's access token while naming the SIGNER
# account, so the runtime is the IAM member and the signer is the IAM resource.
# Rationale: documentation/Security Decision Log.md D3, R8.

# SECURITY: create the dedicated signer identity used when the application is configured
# for it; no key is issued for it
resource "google_service_account" "url_signer" {
  account_id   = split("@", var.signer_service_account)[0]
  display_name = "Signed URL signer for user uploads"

  lifecycle {
    # SECURITY: refuses a signer address outside this project. account_id above is taken
    # from the local part alone, and an out-of-project address provisioned a local
    # account under a name the application never signs as.
    precondition {
      condition     = length(split("@", var.signer_service_account)) == 2 && split("@", var.signer_service_account)[1] == "${var.project_id}.iam.gserviceaccount.com"
      error_message = "signer_service_account must be NAME@${var.project_id}.iam.gserviceaccount.com: the address must name an account in this project, because that is the account this configuration creates and grants the signing role to."
    }
  }
}

# SECURITY: the signer holds read access to the objects its signed URLs grant, scoped to
# the uploads bucket. A signed URL is authorized as the signer, so without this grant the
# URL resolves to an authorization failure.
resource "google_storage_bucket_iam_member" "user_uploads_signer_object_viewer" {
  bucket = google_storage_bucket.user_uploads.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.url_signer.member
}

# The identity the backend API authenticates as, reached from the GKE pod through Workload
# Identity. It holds no key either.
resource "google_service_account" "api_runtime" {
  account_id   = split("@", var.runtime_service_account)[0]
  display_name = "Excel Clone API runtime identity"
}

# SECURITY: the runtime reads, writes and deletes upload objects. The grant is scoped to
# the uploads bucket and is not made at project level; no IAM granted the runtime any
# access to it before.
resource "google_storage_bucket_iam_member" "user_uploads_runtime_object_admin" {
  bucket = google_storage_bucket.user_uploads.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.api_runtime.member
}

# SECURITY: signBlob authority is granted on the signer account alone, never at project
# level, and only to the API runtime identity — the binding previously named the signer
# itself, which authorizes no caller and left signed-URL generation unable to work
resource "google_service_account_iam_member" "url_signer_token_creator" {
  service_account_id = google_service_account.url_signer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.api_runtime.member
}

# SECURITY: only this one Kubernetes service account may act as the runtime identity —
# nothing connected the GKE workload to a signing-capable identity at all
resource "google_service_account_iam_member" "api_runtime_workload_identity" {
  service_account_id = google_service_account.api_runtime.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.kubernetes_namespace}/${var.kubernetes_service_account}]"
}

# HTTPS edge serving the static single-page application.
# This configuration is the sole owner of every edge resource below. scripts/deploy.sh
# creates none of them, so the two cannot race over a name and leave the security headers
# off the winner. Rationale: documentation/Security Decision Log.md R12.
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
  # previously returned no security header at all.
  # These headers are added only to requests that traverse this load balancer, never to a
  # direct storage.googleapis.com request for the same object.
  custom_response_headers = local.security_response_headers
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
# The name and region here are the ones scripts/deploy.sh deploys, so the invoker binding
# below applies to the function that actually exists.
# Rationale: documentation/Security Decision Log.md R13.
resource "google_cloudfunctions_function" "excel_app_function" {
  name        = "excel-app-function"
  description = "Excel Clone HTTP-triggered Cloud Function"
  region      = var.region
  runtime     = "nodejs14"

  available_memory_mb   = 256
  source_archive_bucket = google_storage_bucket.function_source.name
  source_archive_object = "function-source.zip"
  trigger_http          = true
  entry_point           = "helloWorld"
}

# Invoking identity for the Cloud Function
resource "google_service_account" "function_invoker" {
  account_id   = "excel-app-function-invoker"
  display_name = "Cloud Function invoker"
}

# SECURITY: grant the named service account Cloud Functions invoker access without adding
# an allUsers member - invoker IAM was previously unmanaged.
# The binding is additive, so it does not by itself remove an allUsers binding an earlier
# deployment created - scripts/deploy.sh revokes that explicitly. See Decision Log R13.
resource "google_cloudfunctions_function_iam_member" "invoker" {
  project        = google_cloudfunctions_function.excel_app_function.project
  region         = google_cloudfunctions_function.excel_app_function.region
  cloud_function = google_cloudfunctions_function.excel_app_function.name
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

  # SECURITY: pods reach a Google service account through Workload Identity, with no key
  # file in the image — no identity federation was configured at all, so the backend had no
  # way to authenticate as the account authorized to sign object URLs
  # Rationale: documentation/Security Decision Log.md R8.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "primary-node-pool"
  location   = var.region
  cluster    = google_container_cluster.primary.name
  node_count = 3

  node_config {
    preemptible  = true
    machine_type = "e2-medium"

    # SECURITY: the metadata server serves the pod's own Workload Identity, not the node's
    # credentials, so a pod cannot read the node service account's tokens
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Required for the token the runtime presents to the IAM signBlob endpoint.
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

# Resource definitions for Google Cloud Identity Platform
# SECURITY: var.project_id is the one project whose ID tokens the API accepts; it is the
# same value as the backend PROJECT_ID setting, from which firebase_project_id derives.
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

# Google sign-in for Identity Platform.
# google.com is one of the platform's default supported providers, so it is configured with
# default_supported_idp_config. The oauth_idp_config resource used before is for custom
# OIDC providers: it requires an issuer and a name beginning "oidc.", so it failed
# validation and would have failed apply with idp_id "google.com".
resource "google_identity_platform_default_supported_idp_config" "google" {
  idp_id        = "google.com"
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
