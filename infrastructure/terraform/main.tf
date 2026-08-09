# SECURITY: the provider is bounded to major version 7, so a major-version change - which
# alters IAM, TLS and header behaviour - cannot arrive through a `terraform init`. The range
# admits patch and minor releases inside that major version, and no .terraform.lock.hcl is
# committed, so the exact release inside the range can differ between a plan and an apply.
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

  # SECURITY: the configured API origin is admitted to connect-src, so an enforced policy
  # does not block the SPA's API calls.
  # api_origin is a required input that variables.tf validates as a non-empty exact origin,
  # so it is always a source rather than conditionally one. The conditional form advertised a
  # same-origin topology this URL map cannot serve.
  csp_connect_src_sources = [
    "'self'",
    "https://identitytoolkit.googleapis.com",
    "https://securetoken.googleapis.com",
    "https://firestore.googleapis.com",
    "https://firebaseinstallations.googleapis.com",
    var.api_origin,
  ]

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

    # SECURITY: the instance refuses unencrypted connections.
    ip_configuration {
      ssl_mode = "ENCRYPTED_ONLY"
    }
  }
}

# The one database the application connects to. var.db_name defaults to "main-database" and is
# the canonical name across all three planes: this resource creates it, the DATABASE_URL secret
# must name it as its path, and scripts/deploy.sh compares the two before it deploys. Changing it
# on an existing deployment REPLACES the database and destroys its contents.
resource "google_sql_database" "database" {
  name     = var.db_name
  instance = google_sql_database_instance.main.name
}

# The database login the backend connects as is provisioned by an operator, not by this
# configuration: managing it here would persist its password in Terraform state, which this
# project holds in an unprotected local backend. There is deliberately no db_user and no
# db_password variable to read: declaring db_password would write a live database credential
# into whatever tfvars supplied it and into state, for a value no resource here consumes.
# The login is created with `gcloud sql users create --prompt-for-password` and its password
# travels only inside the DATABASE_URL secret. scripts/deploy.sh takes the login from the
# operator's DB_USER and confirms it exists on the instance before deploying.

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
# bucket level, which is what makes the load-balancer origin readable while object ACLs are
# disabled.
#
# This grant is to allUsers, so every object in this bucket is ALSO readable anonymously and
# directly at https://storage.googleapis.com/PROJECT_ID-static-assets/OBJECT, without
# traversing the load balancer. That bounds two controls: the security headers this
# configuration adds as custom_response_headers on the backend bucket, and the HTTPS
# redirect, are properties of the load-balancer path only and are absent from a direct
# storage request. The HTTPS edge is the SUPPORTED and advertised entry point, not the only
# reachable one.
#
# MEASURED, not inferred. Serving the identical document from an origin carrying no security
# headers was compared against this configuration's load-balancer path: the load-balancer path
# returns 6 of 6 headers, the direct path 0 of 6. Two consequences were demonstrated rather
# than assumed:
#   * The document LOADS INSIDE A CROSS-ORIGIN IFRAME: the request completes 200, the whole
#     body is delivered and retained, the frame paints, the console stays silent. The
#     header-protected origin, asked for by an identically shaped request from the same
#     embedder, is refused after its 200 with net::ERR_BLOCKED_BY_RESPONSE and the console
#     message "Framing ... violates ... frame-ancestors 'none'". Both origins served
#     byte-identical bodies - same digest, same ETag - and the document carries no
#     frame-busting script and no frame-ancestors of its own, so framing protection comes
#     exclusively from the response header: a meta element ignores frame-ancestors entirely,
#     so nothing in the document can stand in for it. Two precisions: frame-ancestors is the
#     rule actually enforced (no X-Frame-Options message was emitted for either origin, so
#     that header is legacy defence in depth), and iframe.contentDocument does NOT tell the
#     two apart - it is null from any cross-origin embedder for both - so frameability must be
#     read from the network record, the console and the pixels. UI redress needs only that the
#     framed document renders, not that it be readable, so it is available on this path once a
#     compiled bundle ships.
#   * X-Content-Type-Options: nosniff is absent here too, so the type confusion the header
#     prevents on the load-balancer path is unprevented on this one.
# Cloud Storage cannot close this: an object serves only Content-Type, Content-Encoding,
# Content-Disposition, Content-Language and Cache-Control from its metadata, and no security
# header is expressible there. Closing it needs the bucket to stop granting allUsers and the
# load balancer to read it as an authorized principal - the private-bucket-behind-Cloud-CDN
# arrangement recorded as follow-up F12 - which is an edge redesign rather than a setting.
# The exposure is accepted, bounded and published as residual 10 in SECURITY.md.
#
# OPERATOR REQUIREMENT, not enforced by this configuration: publish only public assets here.
# Never compile a credential or secret into the bundle and never upload one to this bucket,
# because every object in it is world-readable by design.
resource "google_storage_bucket_iam_member" "static_assets_public_read" {
  bucket = google_storage_bucket.static_assets.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_storage_bucket" "user_uploads" {
  name          = "${var.project_id}-user-uploads"
  location      = var.region
  storage_class = var.storage_class

  # SECURITY: object ACLs are disabled and any allUsers or allAuthenticatedUsers grant is
  # overridden, so no uploaded object is readable by URL alone.
  # Uniform bucket-level access rejects object-ACL writes, so application code must not set
  # them. Apply and revert this together with the code path that writes object ACLs.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

# API runtime and signed-URL signing identity.
# ONE dedicated service account. This configuration CREATES it and grants it every role the
# runtime needs, including the signing role below, and it declares the Workload Identity binding
# that lets the pods act as it. What it cannot declare is the other half of that binding: the
# Kubernetes ServiceAccount, its iam.gke.io/gcp-service-account annotation and the pod spec that
# selects it all live in manifests outside this repository, so attaching the account to a running
# workload is still an operator step. scripts/deploy.sh reads both halves back and refuses to
# deploy manifests that do not agree with these resources.
# backend/app/services/file_storage.py reads the signer address from the credentials attached
# to its runtime and never names a second account, so there is one identity here and one
# address to configure. No key is issued for it.
# SECURITY: create the dedicated signing identity the signing role is granted on.
resource "google_service_account" "url_signer" {
  account_id   = split("@", var.signer_service_account)[0]
  display_name = "Excel Clone signed-URL signer"

  lifecycle {
    # SECURITY: refuses an address outside this project. account_id above is taken from the
    # local part alone, so an out-of-project address would create a local account under a
    # name the application never signs as.
    precondition {
      condition     = length(split("@", var.signer_service_account)) == 2 && split("@", var.signer_service_account)[1] == "${var.project_id}.iam.gserviceaccount.com"
      error_message = "signer_service_account must be NAME@${var.project_id}.iam.gserviceaccount.com: the address must name an account in this project, because that is the account this configuration creates and grants the runtime and signing roles to."
    }
  }
}

# SECURITY: the runtime reads, writes and deletes upload objects, scoped to this one bucket
# and never granted at project level. It needs all three: it uploads each workbook, reads it
# back, and deletes a generation it could not sign. No IAM granted it any access to the
# bucket before.
# One grant covers signing too. A V4 signature is authorized as the account it was minted on
# behalf of, and that is this same account, so the read authority a signed GET resolves against
# is already carried here - there is no second account to grant it to.
resource "google_storage_bucket_iam_member" "user_uploads_signer_object_admin" {
  bucket = google_storage_bucket.user_uploads.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.url_signer.member
}

# SECURITY: signBlob authority is granted on ONE account, to ONE member, and never at project
# level. roles/iam.serviceAccountTokenCreator on this account, held by this same account, is
# exactly the iam.serviceAccounts.signBlob permission generate_signed_url needs when the
# runtime holds an access token and no private key - and the self-binding is required rather
# than redundant, because backend/app/services/file_storage.py signs as the address its own
# ambient credentials report, so the signer and the caller are necessarily the same account.
# The permission cannot be narrowed by an IAM Condition, so the only available constraint is
# the authority this account holds elsewhere, which is one bucket, one Cloud SQL instance,
# three secrets, Firestore and a read of Identity Platform - and no project-level grant.
resource "google_service_account_iam_member" "url_signer_token_creator" {
  service_account_id = google_service_account.url_signer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.url_signer.member
}

# SECURITY: exactly one Kubernetes service account may act as this Google service account, and
# nothing else in the cluster can. Enabling the workload pool on the cluster only makes the
# exchange possible; this binding is what authorizes it, and without it the pods hold no Google
# identity - so the API cannot read its secrets, cannot open the Cloud SQL proxy connection and
# cannot sign an object URL, which is every request failing rather than a degraded feature.
# The member is the Kubernetes principal, not a Google account: the pool name is fixed by the
# platform and the bracketed pair is namespace/name, which must equal the pod spec's
# serviceAccountName and its namespace exactly. scripts/deploy.sh reads this binding back and
# refuses to deploy manifests that request any other pair.
# Scoped to this one account rather than granted at project level, so the grant cannot be reused
# to impersonate anything else.
resource "google_service_account_iam_member" "api_runtime_workload_identity" {
  service_account_id = google_service_account.url_signer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.kubernetes_namespace}/${var.kubernetes_service_account}]"
}

# The remaining runtime permissions this account needs are granted below: the Cloud SQL client
# role, read access to exactly the three Secret Manager secrets the application reads, Firestore
# access for the server client library, and the Identity Platform read the revocation check
# performs. The Kubernetes manifests that carry the matching pod spec are not part of this
# repository; scripts/deploy.sh verifies them against these resources before it applies them.

# SECURITY: the runtime may open a connection to the Cloud SQL instance.
# google_sql_database_instance.main is reachable over its public address only - no VPC or
# private route is provisioned here - so the pods reach it through the Cloud SQL Auth Proxy,
# which the manifests run as a sidecar and scripts/deploy.sh verifies. The proxy authenticates
# as this identity, and roles/cloudsql.client is the role that authorizes it; without this
# grant the proxy cannot start, so no request completes, authentication included.
#
# Transport, upstream leg: the proxy dials the instance over its own mutually-authenticated
# TLS session. That is the connection the instance's ENCRYPTED_ONLY ssl_mode governs, and it
# is encrypted independently of any client setting.
#
# Transport, application leg: the backend's db_sslmode applies to the pod-local connection
# between the application and the proxy sidecar, which is a separate connection this
# configuration does not govern. On this topology that value is "disable", because the proxy's
# local listener is plain TCP and carries no certificate for a client to ask for or validate.
# Settings accepts "disable" only when the resolved DATABASE_URL names a loopback endpoint, and
# scripts/deploy.sh asserts the same pair against the manifests before it applies them.
resource "google_project_iam_member" "api_runtime_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = google_service_account.url_signer.member
}

# SECURITY: the runtime may read exactly the three secret versions the application reads and
# no others. Every Settings() construction calls access_secret_version on all three, so
# without this grant the API cannot start.
# The secrets themselves are provisioned by an operator, not by this configuration, so they
# are named by ID rather than referenced as resources. The grant is per secret, never
# project-wide, so a new secret is not readable until it is added here.
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
# by this IAM role alone.
resource "google_project_iam_member" "api_runtime_datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.url_signer.member
}

# SECURITY: the runtime may read a Firebase user record, which is what makes the revocation
# check in backend/app/core/security.py possible.
# verify_id_token is called with check_revoked=True, and that lookup calls firebaseauth.users.get
# on the account named by the token. roles/firebaseauth.viewer is the role that carries that
# permission. Without it every verification fails the lookup rather than the signature check, so
# NO request can authenticate - and the alternative, dropping check_revoked, would let a
# signed-out, password-reset or disabled user's already-issued token stay valid for its full
# lifetime. Viewer, not admin: the application only reads.
resource "google_project_iam_member" "api_runtime_firebaseauth_viewer" {
  project = var.project_id
  role    = "roles/firebaseauth.viewer"
  member  = google_service_account.url_signer.member
}

# HTTPS edge serving the static single-page application.
# This configuration is the sole owner of every edge resource below; scripts/deploy.sh
# creates none of them.
# SECURITY: TLS terminates at the edge, and port 80 carries a redirect-only listener.
resource "google_compute_global_address" "excel_app_lb" {
  name = "excel-app-lb-ip"
}

resource "google_compute_managed_ssl_certificate" "excel_app" {
  name = "excel-app-ssl-cert"

  managed {
    domains = [var.domain_name]
  }

  lifecycle {
    # SECURITY: the domain this certificate serves must be one of the origins the API accepts,
    # so the SPA cannot be published on a domain CORS refuses and Identity Platform has not
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

  # SECURITY: the canonical security header set is emitted on the static origin.
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
  #
  # SCOPE: this rewrite applies to client-routed paths ONLY. The asset paths listed in the
  # path matcher below are excluded from it, because rewriting them is actively harmful:
  # a missing bundle, stylesheet or icon was answered with the entry document under status
  # 200, so a status-code health check called this origin healthy while it served nothing
  # usable, a CDN cached an HTML body under a script's cache key, /robots.txt returned the
  # 28-line document, and X-Content-Type-Options: nosniff became the only control standing
  # between a browser and executing that document as JavaScript. The same scoping is applied
  # at the container edge by the asset location in infrastructure/docker/nginx.conf, so the
  # two delivery paths behave alike.
  default_custom_error_response_policy {
    error_response_rule {
      match_response_codes   = ["404"]
      path                   = "/index.html"
      override_response_code = 200
    }

    error_service = google_compute_backend_bucket.excel_app.self_link
  }

  host_rule {
    hosts        = ["*"]
    path_matcher = "spa"
  }

  path_matcher {
    name            = "spa"
    default_service = google_compute_backend_bucket.excel_app.self_link

    # Inherited from the url-map level above, restated because a path matcher that declares
    # none of its own does not necessarily receive it, and a deep link must keep resolving to
    # the entry document.
    default_custom_error_response_policy {
      error_response_rule {
        match_response_codes   = ["404"]
        path                   = "/index.html"
        override_response_code = 200
      }

      error_service = google_compute_backend_bucket.excel_app.self_link
    }

    # An asset request keeps the bucket's own status. A URL map path supports only a trailing
    # wildcard, so the content-hashed tree is matched by prefix and the root-level assets a
    # Create React App build publishes are named individually.
    path_rule {
      paths = [
        "/static/*",
        "/asset-manifest.json",
        "/manifest.json",
        "/favicon.ico",
        "/logo192.png",
        "/og-image.jpg",
        "/robots.txt",
        "/service-worker.js",
      ]
      service = google_compute_backend_bucket.excel_app.self_link

      # Declining the inherited rewrite requires a rule that MATCHES the code, not the absence
      # of one. A custom error response policy is resolved per error code at the lowest level
      # that matches it: the url-map and path-matcher policies above apply only where no
      # matching policy is declared here. So an EMPTY policy does not decline anything -- it
      # matches nothing, the inherited 404 -> /index.html rewrite still wins, and the scoping
      # would be silently inert. The rule below matches 404 and names no path: with no path
      # there is nothing to rewrite to, and 404 -> 404 leaves the bucket's own status and body
      # intact. This is what try_files $uri =404 expresses in infrastructure/docker/nginx.conf.
      custom_error_response_policy {
        error_response_rule {
          match_response_codes   = ["404"]
          override_response_code = 404
        }

        error_service = google_compute_backend_bucket.excel_app.self_link
      }
    }
  }

  lifecycle {
    # SECURITY: the API must be a separate origin, and the Content-Security-Policy must name
    # it. An empty api_origin means "one origin serves both", which this map cannot do - it
    # routes every path to the static bucket - so the policy would fall back to 'self' and a
    # browser enforcing it would block every API call the application makes.
    precondition {
      condition     = var.api_origin != ""
      error_message = "api_origin must name the origin of the API the single-page application calls. This URL map routes every path to the static-assets bucket, so the API is necessarily served from a different origin, and connect-src 'self' does not admit it: with api_origin empty a browser enforcing the served policy blocks every API request."
    }

    # SECURITY: the API may not be published on the domain this map serves. This map has one
    # backend, the static bucket above, and no path rule routes an API path to the API
    # service, so an application configured to call its own origin receives the rewritten
    # /index.html under status 200 and reads that document as API data. The same-origin API
    # topology was advertised as supported by the default of an empty api_origin, by the
    # container's empty CSP_CONNECT_SRC_API and by the deployment preflight, none of which
    # checked that any API route existed there.
    precondition {
      condition     = var.api_origin != "https://${var.domain_name}"
      error_message = "api_origin must not be the origin this load balancer serves. It is set to ${var.api_origin} while this URL map serves https://${var.domain_name}, whose only backend is the static bucket and whose unmatched paths are rewritten to /index.html with status 200 - so every API call would receive the application document instead of an API response. Publish the API on its own origin, set api_origin and the frontend build's REACT_APP_API_BASE_URL to it, or add an API backend service and path rules to this map before naming this domain."
    }
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

# SECURITY: the TLS versions and cipher suites the edge will negotiate are declared, and the
# floor is TLS 1.2. A target proxy with no ssl_policy uses Google's default COMPATIBLE profile,
# whose minimum is TLS 1.0 - so the edge that exists to stop cleartext traffic would still
# complete a TLS 1.0 session with the weak cipher suites of that era, and a client capable of
# nothing better would be served rather than refused.
# MODERN is chosen over RESTRICTED because RESTRICTED drops RSA key exchange entirely, which
# some older-but-current clients still require; MODERN keeps TLS 1.2 as the floor while
# admitting the suites those clients need.
resource "google_compute_ssl_policy" "excel_app" {
  name            = "excel-app-ssl-policy"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

resource "google_compute_target_https_proxy" "excel_app" {
  name             = "excel-app-https-proxy"
  url_map          = google_compute_url_map.excel_app.self_link
  ssl_certificates = [google_compute_managed_ssl_certificate.excel_app.self_link]
  ssl_policy       = google_compute_ssl_policy.excel_app.self_link
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

# SECURITY: port 80 is answered by a redirect-only listener. This rule is unconditional, so
# every apply of this configuration creates both listeners together.
# Sequencing for a first apply: a Google-managed certificate is validated through this load
# balancer, so it reports PROVISIONING until DNS for domain_name resolves to excel-app-lb-ip.
# During that window both listeners exist and neither serves the application. Point DNS at the
# address, wait for `gcloud compute ssl-certificates describe excel-app-ssl-cert` to report
# ACTIVE, and only then announce the domain. scripts/deploy.sh refuses to publish until that
# status is ACTIVE and both forwarding rules answer on this address.
resource "google_compute_global_forwarding_rule" "excel_app_http" {
  name                  = "excel-app-http-forwarding-rule"
  target                = google_compute_target_http_proxy.excel_app_redirect.self_link
  ip_address            = google_compute_global_address.excel_app_lb.address
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"

  # SECURITY: the redirect target must already be listening before port 80 starts sending
  # users to it. Nothing in this resource's arguments refers to the 443 rule, so this explicit
  # dependency is what orders the two within a single apply.
  depends_on = [google_compute_global_forwarding_rule.excel_app_https]
}

# Resource definitions for Google Cloud Functions
# This configuration is the single authority for the function: its name, region, runtime and
# source archive are declared here, and scripts/deploy.sh deploys none of it.
# Operator prerequisite: upload the source archive and name it through function_source_bucket
# and function_source_object. This configuration does not create that bucket.

# SECURITY: the function runs as its own identity, holding nothing.
# A 1st-gen function with no service_account_email runs as the App Engine default service
# account, which Google grants the project Editor role - so the example function would execute
# with read and write authority over every resource in the project, including the uploads bucket,
# Cloud SQL and Secret Manager. This account is created with NO role binding anywhere in this
# configuration, so a compromise of the function's code yields no project access at all. Grant it
# something only when the function needs it, and grant it narrowly.
resource "google_service_account" "function_runtime" {
  account_id   = "excel-app-function-runtime"
  display_name = "Excel Clone Cloud Function runtime identity"
}

# SECURITY: the source archive must live in a bucket that cannot be read anonymously.
# The archive is the function's deployable code: readable, it discloses the logic and any
# configuration compiled into it, and it is what an attacker would read to find a way in. The
# bucket is not created here - it holds a build output - so its posture is READ from the live
# resource and asserted, rather than assumed from a naming convention.
# The two assertions are the public_access_prevention and uniform_bucket_level_access
# preconditions on google_cloudfunctions_function.excel_app_function below, which is where they
# have to live: a data source cannot carry a precondition of its own on the provider version this
# configuration pins, and attaching them to the function means the function cannot be created or
# updated from an archive whose bucket fails either check.
data "google_storage_bucket" "function_source" {
  name = var.function_source_bucket
}

resource "google_cloudfunctions_function" "excel_app_function" {
  name        = "excel-app-function"
  description = "Excel Clone HTTP-triggered Cloud Function"
  region      = var.region

  # SECURITY: the runtime is supplied explicitly, with no default, so a decommissioned runtime
  # cannot be inherited from this file. Nothing here checks that Google still offers the value:
  # variables.tf validates its shape only, scripts/deploy.sh checks it against
  # `gcloud functions runtimes list` for a 1st-gen environment, and the Cloud Functions API
  # refuses it at apply time otherwise.
  runtime = var.function_runtime

  available_memory_mb   = 256
  source_archive_bucket = var.function_source_bucket
  source_archive_object = var.function_source_object
  trigger_http          = true
  entry_point           = "helloWorld"

  # SECURITY: the function executes as its own unprivileged account. Left unset, a 1st-gen
  # function runs as the App Engine default service account, which carries project Editor.
  service_account_email = google_service_account.function_runtime.email

  # SECURITY: the trigger refuses plaintext http and answers it with a redirect to https.
  # The provider's default for this argument is SECURE_OPTIONAL, which serves the function
  # on both schemes, so the identity token a caller presents could travel in cleartext to
  # any network position on the path.
  https_trigger_security_level = "SECURE_ALWAYS"

  lifecycle {
    # SECURITY: refuses a source archive held in the bucket the load balancer serves to the
    # internet. That bucket grants allUsers read, so the function's deployable code would be
    # world-readable and its object path guessable.
    precondition {
      condition     = var.function_source_bucket != google_storage_bucket.static_assets.name
      error_message = "function_source_bucket must not be the static-assets bucket: that bucket grants allUsers read so the load balancer can serve the SPA, which would publish the function's source archive to the internet. Use a private bucket."
    }

    # SECURITY: refuses a source archive held in the bucket user workbooks are uploaded to.
    # That bucket is written by the API on behalf of end users, so a deployable artifact
    # kept there could be replaced through the upload path, and the function would then run
    # code an end user supplied.
    precondition {
      condition     = var.function_source_bucket != google_storage_bucket.user_uploads.name
      error_message = "function_source_bucket must not be the user-uploads bucket: end users' workbook uploads are written there by the API, so an attacker with an upload could overwrite the function's source archive and have the platform execute it. Use a bucket only the build pipeline writes to."
    }

    # SECURITY: refuses a source archive held in a bucket that permits an anonymous grant. The
    # two preconditions above rule out the two buckets this configuration creates, by name -
    # which says nothing about any OTHER bucket an operator names here. This one reads the live
    # posture of whichever bucket that is, so the guarantee no longer depends on recognising a
    # name. Public access prevention overrides an allUsers or allAuthenticatedUsers grant
    # however it arrived, including one added by hand after the apply.
    precondition {
      condition     = data.google_storage_bucket.function_source.public_access_prevention == "enforced"
      error_message = "The bucket named by function_source_bucket does not enforce public access prevention. The archive is the function's deployable code: read anonymously it discloses the logic and any configuration compiled into it. Set public_access_prevention to enforced on that bucket, or move the archive to a bucket that does."
    }

    # SECURITY: refuses a source archive held in a bucket where object ACLs are still live.
    # Public access prevention blocks the two anonymous principals; an object ACL can still
    # grant a named party read on the archive alone, invisibly to any bucket-level policy
    # review. Uniform bucket-level access disables object ACLs outright, so bucket IAM is the
    # only way access is granted and the posture above is the whole answer.
    precondition {
      condition     = data.google_storage_bucket.function_source.uniform_bucket_level_access
      error_message = "The bucket named by function_source_bucket does not enable uniform bucket-level access, so per-object ACLs can still grant read on the function's source archive independently of the bucket's IAM policy. Enable uniform bucket-level access on that bucket, or move the archive to a bucket that has it."
    }
  }
}

# SECURITY: this is the AUTHORITATIVE list of who may invoke the function - it is the complete
# membership of the invoker role, not an addition to it.
# Invoker IAM was previously unmanaged, so the function was invocable by anyone who discovered
# its URL. It was then managed with google_cloudfunctions_function_iam_member, which is
# ADDITIVE: it granted the intended caller while leaving every pre-existing member in place, so
# an allUsers or allAuthenticatedUsers binding from an earlier deployment - or a broad group a
# person added by hand - survived the apply untouched and was invisible in the plan. An
# authoritative binding removes any member not listed here, on every apply, without needing a
# script to know in advance which members to look for.
# The one member is the identity the API is deployed as, so the caller can mint an identity
# token for the call.
resource "google_cloudfunctions_function_iam_binding" "invoker" {
  project        = google_cloudfunctions_function.excel_app_function.project
  region         = google_cloudfunctions_function.excel_app_function.region
  cloud_function = google_cloudfunctions_function.excel_app_function.name
  role           = "roles/cloudfunctions.invoker"
  members        = [google_service_account.url_signer.member]
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

  # SECURITY: enable the cluster's Workload Identity pool. This is the half of Workload
  # Identity that lives on the cluster, and it is a prerequisite for the other two halves:
  # the node pool's GKE_METADATA mode below, and the
  # google_service_account_iam_member.api_runtime_workload_identity binding above. Without the
  # pool declared here there is no principal for that binding to name, so pods hold no Google
  # identity, Application Default Credentials resolve to nothing usable, and the first
  # Settings() construction fails on Secret Manager - the API cannot start at all.
  # The pool identifier is fixed by Google as <project>.svc.id.goog and must match the member
  # syntax used in that binding.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
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

  # SECURITY: the sign-in origins derive from the CORS allow-list, so the two cannot name
  # different domains. Each entry is the host parsed out of an origin: an authorized domain
  # carries no scheme and no port.
  authorized_domains = local.allowed_origin_hosts

  # SECURITY: email and password sign-in is enabled, which is the method the application uses -
  # frontend/src/services/auth.ts calls signInWithEmailAndPassword, and a disabled provider
  # rejects every sign-in, so no user can obtain the ID token the API requires.
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
