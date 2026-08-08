# =============================================================================
# ONE CANONICAL INPUT SET
# =============================================================================
# Several values below also exist as a backend setting or as a frontend build variable. They
# are the same fact recorded in more than one place, and a deployment is only coherent when
# every copy agrees. Divergence does not fail loudly: it surfaces as refused sign-ins, CORS
# rejections or a Content-Security-Policy that blocks every API call.
#
#   Terraform variable        Backend Settings field     Other copies of the same fact
#   -----------------------   ------------------------   ---------------------------------------
#   project_id                PROJECT_ID                 REACT_APP_FIREBASE_PROJECT_ID
#   allowed_origins           ALLOWED_ORIGINS            -
#   api_origin                -                          origin of REACT_APP_API_BASE_URL
#   csp_report_only           csp_report_only            -
#   signer_service_account    signer_service_account     -
#   domain_name               -                          -
#
# api_origin has NO backend counterpart: the API emits its own Content-Security-Policy on its
# own responses, where connect-src governs nothing, so backend/app/core/config.py declares no
# such field and reads none. It is a static-delivery value only, paired here with the origin of
# the frontend build's REACT_APP_API_BASE_URL and with the container's CSP_CONNECT_SRC_API.
#
# project_id is also the one project whose ID tokens the API accepts: the backend
# firebase_project_id setting is refused at start-up unless it is empty or equal to PROJECT_ID,
# so this value, that setting, REACT_APP_FIREBASE_PROJECT_ID and the scripts/deploy.sh preflight
# all name the same verifier project by construction.
#
# domain_name has no counterpart, but it is not independent: the certificate serves it, so it
# must be the host of an entry in allowed_origins. That is enforced as a precondition on
# google_compute_managed_ssl_certificate rather than as a validation block here, because the
# check compares two variables. signer_service_account carries a project-membership
# precondition on google_service_account.url_signer for the same reason. Preconditions require
# Terraform 1.2 or newer, which main.tf sets as its required_version floor.
#
# The backend template .env.example carries this same table, and backend/app/core/config.py is
# the source of truth for the Settings names and their accepted values. The backend validators
# and the validations here accept the same grammar: exact scheme://host[:port] origins, https
# except for loopback, and TLS modes that cannot negotiate plaintext.
# =============================================================================

variable "project_id" {
  description = "The Google Cloud Project ID. Must equal the backend PROJECT_ID setting and the frontend REACT_APP_FIREBASE_PROJECT_ID: a Firebase ID token names its issuing project, so a token minted for another project is refused by the API"
  type        = string
}

variable "region" {
  description = "The Google Cloud region where resources will be created"
  type        = string
  default     = "us-central1"
}

# NOT REFERENCED by this configuration. It is declared because it is part of the deployment
# contract an operator already works to, and it is defaulted so that contract never forces a
# value that changes nothing. Environments here are separated by Terraform state and by project,
# not by a name component: every resource name below is either fixed or derived from project_id,
# so no resource would change if this value changed. Point a second environment at a second
# project and a second state file.
variable "environment" {
  description = "NOT REFERENCED by any resource in this configuration. Environments are separated by Terraform state and by Google Cloud project rather than by a name component, so no resource name or argument derives from this value. Setting it has no effect; it is retained only so an existing tfvars file does not fail"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be one of: dev, staging, prod."
  }
}

# Database configuration variables
variable "db_instance_tier" {
  description = "The machine type for the database instance. Read by google_sql_database_instance.main; the default is the tier that instance previously hardcoded, so an apply that sets nothing provisions what it did before"
  type        = string
  default     = "db-f1-micro"
}

# The default matches both the name this configuration previously hardcoded and the database
# component of the .env.example DATABASE_URL template, so an apply that sets nothing provisions
# the same database it did before.
variable "db_name" {
  description = "The name of the PostgreSQL database the application connects to. This is the canonical name across all three planes: google_sql_database.database creates it, the DATABASE_URL secret in Secret Manager must name it as the path of its connection URL, and scripts/deploy.sh compares the two and refuses to deploy if they disagree. WARNING: changing this on an existing deployment REPLACES the database and destroys its contents. The default matches the .env.example DATABASE_URL template, so an operator who sets nothing gets a consistent deployment"
  type        = string
  default     = "main-database"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{0,62}$", var.db_name))
    error_message = "Database name must start with a lower-case letter and contain only lower-case letters, digits, underscores and hyphens, up to 63 characters. Keeping it to that set means the same literal is valid unquoted in the DATABASE_URL path, in psql and in gcloud, so the three planes cannot disagree through quoting."
  }
}

# There is deliberately no db_user or db_password variable. The database login is provisioned by
# an operator rather than by this configuration, because managing it here would persist its
# password in Terraform state, which this project holds in an unprotected local backend. Declaring
# inputs that no resource consumes misrepresents the input surface: an operator who sets them sees
# no effect and cannot distinguish that from a setting that failed to apply. The provisioning
# contract is stated on google_sql_database.database in main.tf, and scripts/deploy.sh verifies the
# login exists and that the DATABASE_URL secret names it.

# Storage bucket configuration variables
variable "storage_bucket_name" {
  description = "NOT REFERENCED by any resource in this configuration. Two buckets exist and both derive their names from project_id, as ${var.project_id}-static-assets and ${var.project_id}-user-uploads, so one input cannot name either without renaming - and renaming a bucket replaces it. Setting this has no effect, and it carries a default so it is not a required input either"
  type        = string
  default     = ""
}

variable "storage_class" {
  description = "The storage class for both buckets. Read by google_storage_bucket.static_assets and .user_uploads; the default is the class Cloud Storage applies when none is set"
  type        = string
  default     = "STANDARD"
}

# Kubernetes cluster configuration variables
variable "gke_num_nodes" {
  description = "Number of nodes in the GKE node pool. Read by google_container_node_pool.primary_nodes; the default is the count that pool previously hardcoded"
  type        = number
  default     = 3
}

variable "gke_machine_type" {
  description = "Machine type for GKE nodes. Read by google_container_node_pool.primary_nodes; the default is the type that pool previously hardcoded"
  type        = string
  default     = "e2-medium"
}

# NOT REFERENCED by this configuration, and both are defaulted so neither is a required input.
# Consuming them would mean giving google_container_node_pool.primary_nodes an autoscaling block,
# which is mutually exclusive with the node_count it uses: the pool would stop being fixed at
# gke_num_nodes and start resizing itself. That is a capacity decision, not the documentation fix
# this pair needs, so they are labelled rather than wired. Delete the node_count argument and add
# an autoscaling block to make them effective.
variable "gke_min_nodes" {
  description = "NOT REFERENCED by any resource in this configuration. google_container_node_pool.primary_nodes uses a fixed node_count from gke_num_nodes, and an autoscaling block is mutually exclusive with node_count, so honouring a minimum would be a capacity change rather than a wiring fix. The effective node count is gke_num_nodes. Setting this has no effect"
  type        = number
  default     = 1
}

variable "gke_max_nodes" {
  description = "NOT REFERENCED by any resource in this configuration, for the same reason as gke_min_nodes: node_count and autoscaling are mutually exclusive on google_container_node_pool. The effective node count is gke_num_nodes. Setting this has no effect"
  type        = number
  default     = 5
}

# Edge, CORS and signed-URL security configuration variables
# SECURITY: configure the HTTPS edge, explicit CORS origins, and dedicated signed-URL identity
variable "domain_name" {
  description = "The fully qualified domain name, without scheme, port or path, served by the HTTPS load balancer and named in the Google-managed SSL certificate"
  type        = string

  validation {
    condition     = can(regex("^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.domain_name))
    error_message = "Domain name must be a lower-case fully qualified domain name such as app.example.com, carrying no scheme, port, path or trailing dot."
  }
}

variable "allowed_origins" {
  description = "Exact browser origins (for example, https://app.example.com) permitted to call the API. Each entry is scheme://host[:port] and nothing else. Mirrors the backend ALLOWED_ORIGINS setting. An Identity Platform authorized domain is a host without a port, so a consumer deriving that list must parse the host out of each origin rather than only stripping the scheme"
  type        = list(string)

  validation {
    condition     = length(var.allowed_origins) > 0
    error_message = "At least one allowed origin is required; an empty list permits no cross-origin request at all."
  }

  # The two expressions below are THE canonical origin grammar, character for character the
  # same as backend/app/core/config.py ORIGIN_PATTERN, HTTPS_ORIGIN_PATTERN and
  # LOOPBACK_HTTP_ORIGIN_PATTERN, and the same as the api_origin validations further down.
  # backend/tests/test_security.py asserts that identity, so the three planes cannot drift.
  validation {
    condition = alltrue([
      for origin in var.allowed_origins :
      can(regex("^https?://(\\[::1\\]|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*)(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$", origin))
    ])
    error_message = "Each allowed origin must be exactly scheme://host[:port], for example https://app.example.com: a lower-case ASCII host whose every dot-separated label starts and ends alphanumeric, and a port between 1 and 65535. A trailing slash, path, query string, fragment, wildcard, embedded credentials, upper-case or non-ASCII host, underscore, empty or hyphen-edged label, trailing dot and surrounding whitespace are rejected."
  }

  validation {
    condition = alltrue([
      for origin in var.allowed_origins :
      can(regex("^https://", origin)) || can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$", origin))
    ])
    error_message = "Allowed origins must use https, except http://localhost, http://127.0.0.1 and http://[::1] for local development. Admitting a plaintext origin authorises credentialed requests a network position can read and rewrite."
  }
}

# SECURITY: this value is the cross-origin API source the static Content-Security-Policy
# admits under connect-src. Without it a browser enforcing the policy blocks every API call the
# single-page application makes.
variable "api_origin" {
  description = "Exact origin of the API the single-page application calls, added to the Content-Security-Policy connect-src allow-list that the load balancer attaches to the static SPA document. REQUIRED, and it must differ from https://<domain_name>: this configuration routes no API path to the API service - google_compute_url_map.excel_app has one backend, the static bucket, and rewrites an unmatched path to /index.html - so an API base URL on the edge domain receives the application document under status 200 instead of API responses. This is the ORIGIN of the frontend build's REACT_APP_API_BASE_URL - scheme://host[:port] and nothing else, so a build calling https://api.example.com/v1 sets https://api.example.com. The frontend container image receives this same origin prefixed with a single space as its CSP_CONNECT_SRC_API value - that template appends the value directly to the source before it, so the space is the separator - and both static delivery paths then publish one byte-identical policy"
  type        = string

  validation {
    condition     = can(regex("^https?://(\\[::1\\]|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*)(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$", var.api_origin))
    error_message = "API origin must be exactly scheme://host[:port] under the same grammar allowed_origins enforces, for example https://api.example.com: a lower-case ASCII host whose every dot-separated label starts and ends alphanumeric, and a port between 1 and 65535. An empty value, a trailing slash, path, query string, fragment, wildcard, embedded credentials, upper-case or non-ASCII host, underscore, empty or hyphen-edged label and a trailing dot are rejected. A Content-Security-Policy source that is not an exact origin never matches the requests a browser sends, so it would silently fail to admit the API."
  }

  validation {
    condition     = can(regex("^https://", var.api_origin)) || can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$", var.api_origin))
    error_message = "API origin must use https, except http://localhost, http://127.0.0.1 and http://[::1] for local development. Admitting a plaintext API origin authorises requests a network position can read and rewrite."
  }

  # The origin must also differ from the domain this edge serves. That check compares this
  # variable with another one, so it lives on google_compute_url_map.excel_app as a
  # precondition, which is evaluated at plan time and needs no minimum Terraform version.
}

# ONE service account. The API is deployed as this account and signs object URLs as itself, so
# there is a single address here and a single address to configure anywhere else. There is
# deliberately no second "runtime" account variable: a separate runtime identity would have to be
# named in a backend setting so the application knew to sign as something other than itself, and
# no such setting exists - backend/app/services/file_storage.py reads the signer address from the
# credentials attached to its own runtime. Every grant below is therefore made to this one
# account: signBlob on itself, object admin on the uploads bucket, Cloud SQL client, the three
# secret versions, Firestore, the Firebase Authentication read, and the Workload Identity binding
# from the Kubernetes service account.
variable "signer_service_account" {
  description = "The email address of the one service account the backend runs as and signs V4 Cloud Storage signed URLs as. There is a single runtime identity here, not two: backend/app/services/file_storage.py signs with the credentials attached to its own runtime and names no second account, so this address is both the IAM resource the signing role is granted on and the identity that presents the token when calling signBlob - which is why google_service_account_iam_member.url_signer_token_creator binds the account to itself, the form the IAM signBlob API requires when a runtime signs as itself with no private key. It must belong to project_id, because this configuration creates the account in that project. The backend must name this same address in its signer_service_account setting, and the pod spec must carry it in the iam.gke.io/gcp-service-account annotation"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.signer_service_account))
    error_message = "Signer service account must be a user-managed service account email of the form NAME@PROJECT_ID.iam.gserviceaccount.com."
  }

  # The address must also belong to project_id. That check compares this variable with another
  # one, so it lives on google_service_account.url_signer as a precondition, evaluated at plan
  # time.
}

# There is deliberately no runtime_service_account variable. One account is both the runtime and
# the signer, so a second address would be an input with no resource behind it and would invite a
# deployment to annotate its pods with an identity nothing in this configuration grants anything
# to. The single address is signer_service_account above.

# SECURITY: these two names ARE the Workload Identity member. google_service_account_iam_member
# .api_runtime_workload_identity grants roles/iam.workloadIdentityUser to
# PROJECT.svc.id.goog[NAMESPACE/NAME] built from exactly these values, so a pod in any other
# namespace or under any other service account name authenticates as nobody. Both must match the
# deployed pod spec, and the Kubernetes service account must carry the
# iam.gke.io/gcp-service-account annotation naming signer_service_account - scripts/deploy.sh
# asserts both halves against the manifests before it deploys them.
variable "kubernetes_namespace" {
  description = "The Kubernetes namespace the backend workload runs in. Together with kubernetes_service_account it forms the Workload Identity principal that google_service_account_iam_member.api_runtime_workload_identity authorizes to act as signer_service_account, so both must match the deployed pod spec exactly - a mismatch leaves the pods with no Google identity at all"
  type        = string
  default     = "default"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.kubernetes_namespace))
    error_message = "Kubernetes namespace must be a lower-case DNS label of at most 63 characters."
  }
}

variable "kubernetes_service_account" {
  description = "The Kubernetes service account name the backend pods run as. It must carry the iam.gke.io/gcp-service-account annotation naming signer_service_account, and it must be the pair this namespace grants roles/iam.workloadIdentityUser to, for a pod to reach that Google identity"
  type        = string
  default     = "excel-app-backend"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.kubernetes_service_account))
    error_message = "Kubernetes service account name must be a lower-case DNS label of at most 63 characters."
  }
}

variable "function_runtime" {
  description = "The Cloud Functions runtime the HTTP function is deployed on. There is deliberately no default: the value that was hardcoded here, nodejs14, was decommissioned by Google, and choosing its replacement is a platform decision an operator must make explicitly rather than inherit from this file. Availability is NOT verified here - see the note below - so verify it before applying with `gcloud functions runtimes list --region REGION`, and choose one whose ENVIRONMENTS column includes '1st gen', because google_cloudfunctions_function deploys through the 1st-gen API"
  type        = string

  # SECURITY: a decommissioned or 2nd-gen-only runtime receives no platform security update on
  # this deployment path and cannot be created at all. This file does not detect either
  # condition: which runtimes Google offers, and which of them the 1st-gen API accepts, are
  # live platform properties no list kept here can track.
  #
  # Two authorities read the live platform instead: scripts/deploy.sh asserts the configured
  # value appears in `gcloud functions runtimes list` with a 1st-gen environment, and the Cloud
  # Functions API rejects anything else at apply time. The validation below checks the shape of
  # the identifier only, which is the stable part, so a typo fails locally and in seconds.
  validation {
    condition     = can(regex("^(nodejs|python|go|java|dotnet|ruby|php)[0-9]{1,3}$", var.function_runtime))
    error_message = "function_runtime must be a Cloud Functions runtime identifier: a language name followed by a version number and nothing else, such as nodejs20 or python312. Note that this is a format check only - whether Google still permits creating a 1st-gen function on that runtime is verified by scripts/deploy.sh against `gcloud functions runtimes list`, and by the Cloud Functions API when this configuration is applied."
  }
}

variable "function_source_bucket" {
  description = "Name of the EXISTING private Cloud Storage bucket holding the function's deployable source archive. The archive is a build output, so it is uploaded by the operator or the build pipeline rather than created by this configuration. It must enforce public access prevention and enable uniform bucket-level access, and it must not be the static-assets bucket, which grants allUsers read. Both requirements are preconditions on google_cloudfunctions_function.excel_app_function: the second compares two variables, and the first reads the bucket's live posture through a data source, because a naming convention cannot prove a bucket is private"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.function_source_bucket))
    error_message = "function_source_bucket must be a valid Cloud Storage bucket name, without a gs:// prefix."
  }
}

variable "function_source_object" {
  description = "Object path of the function's source archive within function_source_bucket, for example function-source.zip. Terraform is the single authority for which artifact the function runs, so this is the only place the archive is named"
  type        = string
  default     = "function-source.zip"

  validation {
    condition     = can(regex("^[^/].*\\.zip$", var.function_source_object))
    error_message = "function_source_object must be a .zip object path with no leading slash."
  }
}

# There is deliberately no https_cutover_enabled variable. The port-80 redirect listener is part
# of the final topology and google_compute_global_forwarding_rule.excel_app_http creates it
# unconditionally: a deployment that omitted it left port 80 with no listener at all, which a
# client experiences as a connection failure on the plaintext URL rather than as an upgrade to
# TLS, and nothing guaranteed a later apply would ever add it. The certificate-provisioning
# sequencing the variable existed to manage is handled where it can actually be observed -
# scripts/deploy.sh refuses to publish until `gcloud compute ssl-certificates describe` reports
# ACTIVE and both forwarding rules answer on the reserved address.

variable "csp_report_only" {
  description = "Whether the static single-page application origin emits its Content-Security-Policy as Content-Security-Policy-Report-Only, which reports violations without blocking them. Mirrors the backend csp_report_only setting; keep the two equal so the application and the API enforce the same mode"
  type        = bool
  default     = false
}
