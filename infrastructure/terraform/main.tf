# Main Terraform configuration file for provisioning Google Cloud resources

# SECURITY: the provider version is constrained, so the plan a reviewer approves is built from
# the same provider schema the apply uses - an unconstrained provider is resolved afresh on
# every `terraform init`, and a major-version change alters IAM, TLS and header behaviour
# silently. The range admits patch and minor releases inside one major version; no
# .terraform.lock.hcl is committed, so the exact build inside that range is not fixed and
# remains a recorded residual.
# required_version reflects the newest language feature this configuration uses: resource
# preconditions, added in Terraform 1.2.
terraform {
  required_version = ">= 1.2.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

# Provider configuration for Google Cloud
provider "google" {
  project = var.project_id
  region  = var.region
}

# Canonical HTTP security header set for the static origin, in the directive order
# backend/app/core/security_headers.py defines. For a given api_origin and csp_report_only the
# policy rendered below matches CONTENT_SECURITY_POLICY in that module and the policy
# infrastructure/docker/nginx.conf renders, differing only in the connect-src sources each
# origin needs. The compiled single-page application carries a meta policy as well, which is
# always enforced because a meta element cannot carry Content-Security-Policy-Report-Only; it
# therefore names neither connect-src nor default-src, so it governs loading only and cannot
# block the configured API. A browser applies the intersection of every policy it receives, so
# csp_report_only relaxes the header-delivered policies only.
locals {
  # Hosts parsed out of the browser origins the API accepts. An Identity Platform authorized
  # domain carries neither scheme nor port, so the host is extracted rather than the scheme
  # stripped. Derived once and used both to authorize sign-in domains and to check that the
  # domain this load balancer serves is one of them.
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
  # is ever emitted. Mirrors the backend csp_report_only setting and the container's
  # CSP_HEADER_NAME. It does not reach the document's meta policy, which has no report-only
  # form.
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
    tier = var.db_instance_tier

    # SECURITY: the instance refuses unencrypted connections; it previously accepted them
    ip_configuration {
      ssl_mode = "ENCRYPTED_ONLY"
    }
  }
}

resource "google_sql_database" "database" {
  name     = var.db_name
  instance = google_sql_database_instance.main.name
}

# The database login the backend connects as is provisioned by an operator, not by this
# configuration: managing it here would persist its password in Terraform state, which this
# project holds in an unprotected local backend. db_user and db_password remain declared
# inputs for a deployment that supplies them to a protected backend of its own.

# Resource definitions for Google Cloud Storage buckets
# This is the ONE bucket the compiled SPA is published to, and the origin the load balancer
# below serves. scripts/deploy.sh uploads here and creates no bucket of its own.
resource "google_storage_bucket" "static_assets" {
  name          = "${var.project_id}-static-assets"
  location      = var.region
  storage_class = var.storage_class

  # SECURITY: object ACLs are disabled; access is granted by bucket IAM only.
  # Public access prevention is not enforced on this bucket: it serves the public SPA.
  uniform_bucket_level_access = true
}

# SECURITY: read access to the public single-page application is granted explicitly at
# bucket level. With object ACLs disabled and no such grant, the load balancer origin
# was unreadable and returned an authorization failure for every request.
#
# This grant is to allUsers, so every object in this bucket is ALSO readable anonymously
# and directly at https://storage.googleapis.com/PROJECT_ID-static-assets/OBJECT, without
# traversing the load balancer. That is the intended consequence of hosting a public SPA
# from a bucket, and it bounds two controls: the security headers this configuration adds
# as custom_response_headers on the backend bucket, and the HTTPS redirect, are properties
# of the load-balancer path only and are absent from a direct storage request. The HTTPS
# edge is the SUPPORTED and advertised entry point, not the only reachable one. Nothing
# secret may be published to this bucket, because bucket contents are public by design;
# the compiled bundle is public code, and no credential or secret is compiled into it.
resource "google_storage_bucket_iam_member" "static_assets_public_read" {
  bucket = google_storage_bucket.static_assets.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_storage_bucket" "user_uploads" {
  name          = "${var.project_id}-user-uploads"
  location      = var.region
  storage_class = var.storage_class

  # SECURITY: object ACLs are disabled and allUsers/allAuthenticatedUsers grants are
  # overridden; every uploaded object was previously world-readable by URL.
  # Uniform bucket-level access rejects object-ACL writes; application code must not set
  # them. Apply and revert this together with the code path that set a public object ACL.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

# Signed-URL signing identity.
# ONE dedicated service account: the API is deployed as this account and signs object URLs
# as itself. backend/app/services/file_storage.py reads the address from the credentials
# attached to its runtime and never names a second account, so there is one identity here
# and one address to configure. No key is issued for it.
# SECURITY: create the dedicated signing identity; the signing role was granted nowhere
resource "google_service_account" "url_signer" {
  account_id   = split("@", var.signer_service_account)[0]
  display_name = "Excel Clone API runtime and signed-URL signer"

  lifecycle {
    # SECURITY: refuses an address outside this project. account_id above is taken from
    # the local part alone, and an out-of-project address provisioned a local account
    # under a name the application never signs as.
    precondition {
      condition     = length(split("@", var.signer_service_account)) == 2 && split("@", var.signer_service_account)[1] == "${var.project_id}.iam.gserviceaccount.com"
      error_message = "signer_service_account must be NAME@${var.project_id}.iam.gserviceaccount.com: the address must name an account in this project, because that is the account this configuration creates and grants the signing role to."
    }
  }
}

# SECURITY: the account reads, writes and deletes upload objects, scoped to this one bucket
# and never granted at project level. It needs all three: it uploads each workbook, deletes
# a generation it could not sign, and a signed URL is authorized as its signer, so without
# read access the URL resolves to an authorization failure. No IAM granted it any access to
# the bucket before.
resource "google_storage_bucket_iam_member" "user_uploads_signer_object_admin" {
  bucket = google_storage_bucket.user_uploads.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.url_signer.member
}

# SECURITY: signBlob authority is granted on this one account and never at project level.
# The account is both the caller and the resource, because the runtime signs as itself:
# roles/iam.serviceAccountTokenCreator on its own account is exactly the
# iam.serviceAccounts.signBlob permission generate_signed_url needs when the runtime holds
# a token and no private key. Signed-URL generation was authorized nowhere before.
resource "google_service_account_iam_member" "url_signer_token_creator" {
  service_account_id = google_service_account.url_signer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.url_signer.member
}

# The remaining runtime permissions this account needs are granted below: the Cloud SQL
# client role, read access to exactly the three Secret Manager secrets the application reads,
# and Firestore access for the server client library. Binding the account to the Kubernetes
# service account through Workload Identity remains an operator provisioning step, listed in
# the .env.example checklist, because the Kubernetes manifests are not part of this
# repository.

# SECURITY: the runtime may open a connection to the Cloud SQL instance.
# google_sql_database_instance.main is reachable over its public address only - no VPC or
# private route is provisioned here - so the pods reach it through the Cloud SQL Auth Proxy,
# which the manifests run as a sidecar and scripts/deploy.sh verifies. The proxy authenticates
# as this identity, and roles/cloudsql.client is the role that authorizes it; without this
# grant the proxy cannot start, so no request completes. Authentication itself queries the
# users table, which made the missing grant a total outage rather than a degraded one.
#
# On encryption: the proxy dials the instance over its own mutually-authenticated TLS session,
# which is what satisfies the instance's ENCRYPTED_ONLY ssl_mode, and it presents a plain
# loopback listener to the application inside the same pod. The backend's db_sslmode is
# therefore a client-side setting on a connection that never leaves the pod. It must stay
# "require": "verify-ca" and "verify-full" would try to validate the proxy's local listener
# against the instance certificate and fail, and Settings.db_sslmode deliberately cannot
# express "disable".
resource "google_project_iam_member" "api_runtime_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = google_service_account.url_signer.member
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
  member    = google_service_account.url_signer.member
}

# SECURITY: the runtime may read and write Firestore documents.
# backend/app/services/real_time_sync.py uses the server client library, which authenticates
# as this identity and bypasses Firestore security rules entirely, so its access is governed
# by this IAM role alone. It previously held none, so every collaboration write failed.
resource "google_project_iam_member" "api_runtime_datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.url_signer.member
}

# HTTPS edge serving the static single-page application.
# This configuration is the sole owner of every edge resource below; scripts/deploy.sh
# creates none of them.
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
    # These values were validated only in isolation, so a deployment could serve the SPA from
    # a domain the API rejected as a cross-origin caller and that Identity Platform never
    # authorized for sign-in. The authorized sign-in domains and this check read the same
    # derived host list, so the two cannot drift apart.
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

  # Client-routed paths resolve to the single-page application's entry document. A deep link
  # is a fresh request to the load balancer and Cloud Storage holds no object at that path,
  # so without this a deep link or a reload away from "/" returns the bucket's 404.
  # The bucket's own MainPageSuffix and NotFoundPage settings are not consulted here; they
  # apply to the Cloud Storage website endpoints, not to a backend bucket behind a load
  # balancer.
  # Only a missing object is rewritten, so a real asset is still served as itself. This is
  # the behaviour infrastructure/docker/nginx.conf expresses with try_files.
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

# SECURITY: port 80 is answered by a redirect-only listener, and it exists unconditionally.
# The redirect is part of the final topology rather than an optional second stage: a
# deployment that omits it leaves port 80 with no listener, which a client experiences as a
# connection failure on the plaintext URL rather than as an upgrade to TLS, and it leaves
# nothing to guarantee that a later apply ever adds it.
# Sequencing note for a first apply: a Google-managed certificate is validated through this
# load balancer, so it reports PROVISIONING until DNS for domain_name resolves to
# excel-app-lb-ip. During that window both listeners exist and neither serves the
# application; point DNS at the address, wait for
# `gcloud compute ssl-certificates describe excel-app-ssl-cert` to report ACTIVE, and only
# then announce the domain. scripts/deploy.sh refuses to publish until that status is ACTIVE
# and both forwarding rules are present on this address.
resource "google_compute_global_forwarding_rule" "excel_app_http" {
  name                  = "excel-app-http-forwarding-rule"
  target                = google_compute_target_http_proxy.excel_app_redirect.self_link
  ip_address            = google_compute_global_address.excel_app_lb.address
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"

  # SECURITY: the redirect target must already be listening before port 80 starts sending
  # users to it. Nothing in this resource's arguments refers to the 443 rule, so without this
  # the two would be created concurrently in a single apply and port 80 could redirect to an
  # address with no https listener at all.
  depends_on = [google_compute_global_forwarding_rule.excel_app_https]
}

# Resource definitions for Google Cloud Functions
# This configuration is the single authority for the function: its name, region, runtime and
# source archive are declared here, and scripts/deploy.sh deploys none of it.
# Operator prerequisite: upload the source archive and name it through function_source_bucket
# and function_source_object. This configuration does not create that bucket.
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

# SECURITY: only the named API service account may invoke the function, and no allUsers
# member is added - invoker IAM was previously unmanaged, so the function was invocable by
# anyone who discovered its URL.
# This names the identity the API is deployed as, so the caller can mint an identity token
# for the call.
# The binding is additive, so it does not by itself remove an allUsers binding an earlier
# deployment created - scripts/deploy.sh revokes that explicitly.
resource "google_cloudfunctions_function_iam_member" "invoker" {
  project        = google_cloudfunctions_function.excel_app_function.project
  region         = google_cloudfunctions_function.excel_app_function.region
  cloud_function = google_cloudfunctions_function.excel_app_function.name
  role           = "roles/cloudfunctions.invoker"
  member         = google_service_account.url_signer.member
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
  node_count = var.gke_num_nodes

  node_config {
    preemptible  = true
    machine_type = var.gke_machine_type

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
# - Firestore: Add necessary indexes. Security rules are no longer outstanding: they are
#   defined in firestore.rules at the repository root and deployed by scripts/deploy.sh.
# - GKE: Configure autoscaling, networking, and other advanced features as per project needs.
# - Identity Platform: Add more providers and configure advanced settings like multi-factor authentication.
