# Main Terraform configuration file for provisioning Google Cloud resources

# Provider configuration for Google Cloud
provider "google" {
  project = var.project_id
  region  = var.region
}

# Canonical HTTP security header set for the static origin.
# Must not diverge from backend/app/core/security_headers.py, which defines the same set for
# API responses; connect-src here additionally admits the API origin the browser reaches.
locals {
  # Hosts parsed out of the browser origins the API accepts. An Identity Platform authorized
  # domain carries no scheme and no port, so the host is extracted rather than the scheme
  # merely stripped. Derived once and used both to authorize sign-in domains and to check that
  # the domain this load balancer serves is one of them.
  allowed_origin_hosts = distinct([
    for origin in var.allowed_origins :
    regex("^https?://(?P<host>\\[[0-9a-fA-F:.]+\\]|[^:/]+)(?::[0-9]+)?$", origin).host
  ])

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

# SECURITY: the login the backend connects as is provisioned here with a password supplied
# from outside the repository. No database user existed, so the DATABASE_URL secret named
# credentials that could not authenticate and the API could not reach its own data.
resource "google_sql_user" "app" {
  name     = var.db_user
  instance = google_sql_database_instance.main.name
  password = var.db_password
}

# Resource definitions for Google Cloud Storage buckets
# This is the ONE bucket the compiled SPA is published to, and the origin the load balancer
# below serves. scripts/deploy.sh uploads here and creates no bucket of its own.
resource "google_storage_bucket" "static_assets" {
  name     = "${var.project_id}-static-assets"
  location = var.region

  # SECURITY: object ACLs are disabled; access is granted by bucket IAM only.
  # Public access prevention is not enforced on this bucket: it serves the public SPA.
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

resource "google_storage_bucket" "user_uploads" {
  name     = "${var.project_id}-user-uploads"
  location = var.region

  # SECURITY: object ACLs are disabled and allUsers/allAuthenticatedUsers grants are
  # overridden; every uploaded object was previously world-readable by URL.
  # Uniform bucket-level access rejects object-ACL writes; application code must not set
  # them. Apply and revert this together with the code path that set a public object ACL.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

# Signed-URL signing identities.
# The signing call presents the RUNTIME identity's access token while naming the SIGNER
# account, so the runtime is the IAM member and the signer is the IAM resource.

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

  lifecycle {
    # SECURITY: refuses a runtime address outside this project. account_id above is taken
    # from the local part alone, and an out-of-project address provisioned a local account
    # under a name the workload never authenticates as.
    precondition {
      condition     = length(split("@", var.runtime_service_account)) == 2 && split("@", var.runtime_service_account)[1] == "${var.project_id}.iam.gserviceaccount.com"
      error_message = "runtime_service_account must be NAME@${var.project_id}.iam.gserviceaccount.com: the address must name an account in this project, because that is the account this configuration creates and binds Workload Identity to."
    }

    # SECURITY: the runtime and the signer must be two different accounts. This
    # configuration creates one service account per address, so equal addresses declare the
    # same account twice and the apply fails on a duplicate. It would also make the signing
    # grant a self-binding, which authorizes no caller and leaves signed URLs unproducible.
    precondition {
      condition     = var.runtime_service_account != var.signer_service_account
      error_message = "runtime_service_account and signer_service_account must differ: this configuration creates a distinct account for each, and granting roles/iam.serviceAccountTokenCreator from an account to itself authorizes no caller."
    }
  }
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

# SECURITY: the runtime may read exactly the three secret versions the application reads and
# no others - it held no Secret Manager permission at all, so every Settings() construction
# failed on the first access_secret_version call and the API could not start.
# The secrets themselves are provisioned by an operator, not by this configuration, so they
# are named by ID rather than referenced as resources. The grant is per secret, never
# project-wide, so a new secret is not readable until it is added here deliberately.
resource "google_secret_manager_secret_iam_member" "api_runtime_secret_accessor" {
  for_each = toset(["DATABASE_URL", "REDIS_URL", "SECRET_KEY"])

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.api_runtime.member
}

# SECURITY: the runtime may read Identity Platform user records, which is what
# firebaseauth.users.get authorizes. backend/app/core/security.py verifies every ID token
# with check_revoked=True, and that call reads the user record to learn whether the token was
# revoked; without this role it fails and every authenticated request is rejected.
resource "google_project_iam_member" "api_runtime_firebaseauth_viewer" {
  project = var.project_id
  role    = "roles/firebaseauth.viewer"
  member  = google_service_account.api_runtime.member
}

# SECURITY: the runtime may read and write Firestore documents.
# backend/app/services/real_time_sync.py uses the server client library, which authenticates
# as this identity and bypasses Firestore security rules entirely, so its access is governed
# by this IAM role alone. It previously held none, so every collaboration write failed.
resource "google_project_iam_member" "api_runtime_datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.api_runtime.member
}

# HTTPS edge serving the static single-page application.
# This configuration is the sole owner of every edge resource below. scripts/deploy.sh
# creates none of them, so the two cannot race over a name and leave the security headers
# off the winner.
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

  lifecycle {
    # SECURITY: the domain this certificate serves must be one of the origins the API accepts.
    # domain_name, allowed_origins and api_origin were validated only in isolation, so a
    # deployment could serve the SPA from a domain the API rejected as a cross-origin caller
    # and that Identity Platform never authorized for sign-in - each value individually valid
    # and the set as a whole broken. Both the authorized sign-in domains and this check read
    # the same derived host list, so the two cannot drift apart.
    precondition {
      condition     = contains(local.allowed_origin_hosts, var.domain_name)
      error_message = "domain_name must appear as the host of an entry in allowed_origins. This certificate serves ${var.domain_name}, but the API's accepted origins resolve to hosts [${join(", ", local.allowed_origin_hosts)}], so the browser origin this load balancer publishes would be refused by CORS and unauthorized for sign-in. Add https://${var.domain_name} to allowed_origins, and keep the backend ALLOWED_ORIGINS setting equal to it."
    }
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

  # Client-routed paths resolve to the single-page application's entry document. The SPA owns
  # its routes in the browser, but a deep link is a fresh request to the load balancer, and
  # Cloud Storage holds no object at that path - so without this every deep link and every
  # page reload away from "/" returned the bucket's 404 instead of the application.
  # The bucket's own MainPageSuffix and NotFoundPage settings are not consulted on this path;
  # they apply to the Cloud Storage website endpoints, not to a backend bucket behind this
  # load balancer, which is why the routing has to be expressed here.
  # Only a missing object is rewritten, so a real asset is still served as itself and this
  # matches what infrastructure/docker/nginx.conf does with try_files.
  default_custom_error_response_policy {
    error_response_rule {
      match_response_codes   = ["404"]
      path                   = "/index.html"
      override_response_code = 200
    }

    error_service = google_compute_backend_bucket.excel_app.self_link
  }
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

# SECURITY: this is the second stage of the cutover, and it exists only once
# https_cutover_enabled is true. A Google-managed certificate cannot be validated until the
# 443 listener above exists and DNS resolves to this address, so the 443 rule must be created
# while the certificate is still PROVISIONING. Sending users from port 80 to a certificate
# that is not yet serving would break the site, so the redirect is withheld until an operator
# has confirmed the certificate is ACTIVE and flipped this flag in a second apply.
# Until then port 80 has no listener at all, so no plaintext request is served either way.
resource "google_compute_global_forwarding_rule" "excel_app_http" {
  count = var.https_cutover_enabled ? 1 : 0

  name                  = "excel-app-http-forwarding-rule"
  target                = google_compute_target_http_proxy.excel_app_redirect.self_link
  ip_address            = google_compute_global_address.excel_app_lb.address
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"

  lifecycle {
    # SECURITY: refuses to create the redirect while the certificate is not ACTIVE. The
    # status is read from the certificate resource itself, so the flag cannot be flipped
    # ahead of the certificate actually being able to serve.
    precondition {
      condition     = contains(google_compute_managed_ssl_certificate.excel_app.subject_alternative_names, var.domain_name)
      error_message = "The managed certificate does not yet cover domain_name, so it is still provisioning. Wait until `gcloud compute ssl-certificates describe excel-app-ssl-cert` reports ACTIVE for this domain, then apply again with https_cutover_enabled = true."
    }
  }
}

# Resource definitions for Google Cloud Functions
# This configuration is the single authority for the function: its name, region, runtime and
# source archive are declared here and scripts/deploy.sh deploys none of it, so the two cannot
# disagree about which artifact is running.
# The source archive is uploaded by the operator to a bucket of their choosing and named by
# function_source_bucket and function_source_object. It is not a bucket this configuration
# creates: the archive is a build output, and a bucket created here would exist empty on the
# first apply and make the function reference an object that does not exist.
resource "google_cloudfunctions_function" "excel_app_function" {
  name        = "excel-app-function"
  description = "Excel Clone HTTP-triggered Cloud Function"
  region      = var.region

  # SECURITY: the runtime is supplied explicitly and validated against the decommissioned
  # list - it was pinned to nodejs14, which Google decommissioned on 30 January 2025 and no
  # longer permits for creation or redeployment, so the function could not be deployed and
  # could not receive a platform security update.
  runtime = var.function_runtime

  available_memory_mb   = 256
  source_archive_bucket = var.function_source_bucket
  source_archive_object = var.function_source_object
  trigger_http          = true
  entry_point           = "helloWorld"

  lifecycle {
    # SECURITY: refuses a source archive held in the bucket the load balancer serves to the
    # internet. That bucket grants allUsers read, so the function's deployable code would be
    # world-readable and its object path guessable.
    precondition {
      condition     = var.function_source_bucket != google_storage_bucket.static_assets.name
      error_message = "function_source_bucket must not be the static-assets bucket: that bucket grants allUsers read so the load balancer can serve the SPA, which would publish the function's source archive to the internet. Use a private bucket."
    }
  }
}

# SECURITY: only the API runtime identity may invoke the function, and no allUsers member is
# added - invoker IAM was previously unmanaged, and the account it named was one no workload
# could obtain credentials for, so the binding authorized nobody.
# This names the same identity the GKE workload already reaches through Workload Identity, so
# the caller can actually mint an identity token for the call.
# The binding is additive, so it does not by itself remove an allUsers binding an earlier
# deployment created - scripts/deploy.sh revokes that explicitly.
resource "google_cloudfunctions_function_iam_member" "invoker" {
  project        = google_cloudfunctions_function.excel_app_function.project
  region         = google_cloudfunctions_function.excel_app_function.region
  cloud_function = google_cloudfunctions_function.excel_app_function.name
  role           = "roles/cloudfunctions.invoker"
  member         = google_service_account.api_runtime.member
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
  authorized_domains = local.allowed_origin_hosts

  # SECURITY: email and password sign-in is enabled, which is the method the application
  # actually uses - frontend/src/services/auth.ts calls signInWithEmailAndPassword, and with
  # the provider disabled every sign-in was rejected, so no user could obtain the ID token
  # the API now requires.
  # allow_duplicate_emails stays false so one address maps to one account: the API resolves a
  # local user by the verified email claim, and duplicate addresses would make that mapping
  # ambiguous. password_required makes this provider password-based rather than email-link.
  sign_in {
    allow_duplicate_emails = false

    email {
      enabled           = true
      password_required = true
    }
  }
}

# HUMAN ASSISTANCE NEEDED
# The following resources may need additional configuration based on specific project requirements:
# - Cloud SQL: Consider adding more configuration options like backup settings, maintenance window, etc.
# - Cloud Storage: Add lifecycle rules, IAM permissions, and other bucket configurations as needed.
# - Cloud Functions: Update the function code source, memory, and other settings based on actual function requirements.
# - Firestore: Add necessary indexes and security rules.
# - GKE: Configure autoscaling, networking, and other advanced features as per project needs.
# - Identity Platform: Add more providers and configure advanced settings like multi-factor authentication.
