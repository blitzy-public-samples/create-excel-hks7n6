# =============================================================================
# ONE CANONICAL INPUT SET
# =============================================================================
# Several values below also exist as a backend setting and as a frontend build variable. They
# are the same fact recorded in three places, and a deployment is only coherent when all three
# agree. Divergence does not fail loudly: it surfaces as refused sign-ins, CORS rejections or a
# Content-Security-Policy that blocks every API call.
#
#   Terraform variable        Backend Settings field     Frontend build variable
#   -----------------------   ------------------------   ------------------------------
#   project_id                PROJECT_ID                 REACT_APP_FIREBASE_PROJECT_ID
#   allowed_origins           ALLOWED_ORIGINS            -
#   api_origin                api_origin                 origin of REACT_APP_API_BASE_URL
#   csp_report_only           csp_report_only            -
#   signer_service_account    signer_service_account     -
#   domain_name               -                          -
#
# domain_name has no counterpart, but it is not independent: the certificate serves it, so it
# must be the host of an entry in allowed_origins. That is enforced as a precondition on
# google_compute_managed_ssl_certificate rather than as a validation block here, because the
# check compares two variables. The same applies to the signer/runtime account checks, which
# live on their google_service_account resources. Preconditions are evaluated at plan time and
# need no minimum Terraform version.
#
# The backend template .env.example carries this same table, and backend/app/core/config.py is
# the source of truth for the Settings names and their accepted values. The backend validators
# and the validations here deliberately accept the same grammar: exact scheme://host[:port]
# origins, https except for loopback, and TLS modes that cannot negotiate plaintext.
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

variable "environment" {
  description = "The environment (e.g., dev, staging, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be one of: dev, staging, prod."
  }
}

# Database configuration variables
variable "db_instance_tier" {
  description = "The machine type for the database instance"
  type        = string
  default     = "db-f1-micro"
}

variable "db_name" {
  description = "The name of the database to create"
  type        = string
}

variable "db_user" {
  description = "The username for the database"
  type        = string
}

variable "db_password" {
  description = "The password for the database user"
  type        = string
  sensitive   = true
}

# Storage bucket configuration variables
variable "storage_bucket_name" {
  description = "The name of the Google Cloud Storage bucket"
  type        = string
}

variable "storage_class" {
  description = "The storage class for the bucket"
  type        = string
  default     = "STANDARD"
}

# Kubernetes cluster configuration variables
variable "gke_num_nodes" {
  description = "Number of nodes in the GKE cluster"
  type        = number
  default     = 3
}

variable "gke_machine_type" {
  description = "Machine type for GKE nodes"
  type        = string
  default     = "e2-medium"
}

variable "gke_min_nodes" {
  description = "Minimum number of nodes in the GKE cluster"
  type        = number
  default     = 1
}

variable "gke_max_nodes" {
  description = "Maximum number of nodes in the GKE cluster"
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

  validation {
    condition = alltrue([
      for origin in var.allowed_origins :
      can(regex("^https?://([a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?|\\[::1\\])(:[0-9]{1,5})?$", origin))
    ])
    error_message = "Each allowed origin must be exactly scheme://host[:port], for example https://app.example.com. Wildcards, embedded credentials, paths, query strings and fragments are rejected."
  }

  validation {
    condition = alltrue([
      for origin in var.allowed_origins :
      can(regex("^https://", origin)) || can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]{1,5})?$", origin))
    ])
    error_message = "Allowed origins must use https, except http://localhost, http://127.0.0.1 and http://[::1] for local development."
  }
}

# SECURITY: the static Content-Security-Policy admits the API origin; the policy previously
# admitted no cross-origin API, so a browser enforcing it blocked every API call the
# single-page application makes
variable "api_origin" {
  description = "Exact origin of the API the single-page application calls, added to the Content-Security-Policy connect-src allow-list that the load balancer attaches to the static SPA document. Empty means one edge origin serves both the SPA and the API, which the policy's 'self' already covers; leave it empty ONLY in that case, because with the default a browser enforcing the policy blocks every cross-origin API call. Otherwise this is the ORIGIN of the frontend build's REACT_APP_API_BASE_URL - scheme://host[:port] and nothing else, so a build calling https://api.example.com/v1 sets https://api.example.com. It must equal the CSP_CONNECT_SRC_API value supplied to the frontend container image, so both static delivery paths publish one policy"
  type        = string
  default     = ""

  validation {
    condition     = var.api_origin == "" || can(regex("^https?://(\\[::1\\]|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*)(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$", var.api_origin))
    error_message = "API origin must be empty, or exactly scheme://host[:port] under the same grammar allowed_origins enforces, for example https://api.example.com: a lower-case ASCII host whose every dot-separated label starts and ends alphanumeric, and a port between 1 and 65535. A trailing slash, path, query string, fragment, wildcard, embedded credentials, upper-case or non-ASCII host, underscore, empty or hyphen-edged label and a trailing dot are rejected. A Content-Security-Policy source that is not an exact origin never matches the requests a browser sends, so it would silently fail to admit the API."
  }

  validation {
    condition     = var.api_origin == "" || can(regex("^https://", var.api_origin)) || can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?$", var.api_origin))
    error_message = "API origin must use https, except http://localhost, http://127.0.0.1 and http://[::1] for local development. Admitting a plaintext API origin authorises requests a network position can read and rewrite."
  }
}

variable "signer_service_account" {
  description = "The email address of the dedicated service account that signs V4 Cloud Storage signed URLs for the uploads bucket. This account is the IAM RESOURCE the signing role is granted on, not the member: the runtime identity presents its own access token while naming this account, so runtime_service_account is the member. It must belong to project_id, because this configuration creates the account in that project. The application does not name this account: it signs with its own attached runtime identity, which this configuration authorizes to sign on this account's behalf"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.signer_service_account))
    error_message = "Signer service account must be a user-managed service account email of the form NAME@PROJECT_ID.iam.gserviceaccount.com."
  }

  # The address must also belong to project_id. That check compares this variable with
  # another one, so it lives on google_service_account.url_signer as a precondition, which
  # is evaluated at plan time and needs no minimum Terraform version.
}

variable "runtime_service_account" {
  description = "The email address of the service account the backend API authenticates as, reached from the GKE workload through Workload Identity. This account is the IAM MEMBER granted roles/iam.serviceAccountTokenCreator on signer_service_account, which is what authorizes the signBlob call that mints a signed URL. Set it equal to signer_service_account only if the runtime is to sign as itself"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.runtime_service_account))
    error_message = "Runtime service account must be a user-managed service account email of the form NAME@PROJECT_ID.iam.gserviceaccount.com."
  }

  # It must also belong to project_id and must differ from signer_service_account. Both
  # compare this variable with another one, so they live on
  # google_service_account.api_runtime as preconditions, which are evaluated at plan time and
  # need no minimum Terraform version.
}

variable "kubernetes_namespace" {
  description = "The Kubernetes namespace the backend workload runs in. Together with kubernetes_service_account it forms the Workload Identity member that may impersonate runtime_service_account, so both must match the deployed pod spec exactly"
  type        = string
  default     = "default"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.kubernetes_namespace))
    error_message = "Kubernetes namespace must be a lower-case DNS label of at most 63 characters."
  }
}

variable "kubernetes_service_account" {
  description = "The Kubernetes service account name the backend pods run as. It must carry the iam.gke.io/gcp-service-account annotation naming runtime_service_account for Workload Identity to bind the two"
  type        = string
  default     = "excel-app-backend"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.kubernetes_service_account))
    error_message = "Kubernetes service account name must be a lower-case DNS label of at most 63 characters."
  }
}

variable "function_runtime" {
  description = "The Cloud Functions runtime the HTTP function is deployed on. There is deliberately no default: the value that was hardcoded here, nodejs14, was decommissioned by Google on 30 January 2025, and choosing its replacement is a platform decision an operator must make explicitly rather than inherit from this file. Supply a runtime Google currently supports for function creation"
  type        = string

  validation {
    condition     = can(regex("^(nodejs|python|go|java|dotnet|ruby|php)[0-9]+$", var.function_runtime))
    error_message = "function_runtime must be a Cloud Functions runtime identifier such as nodejs20 or python312."
  }

  # SECURITY: refuses a runtime Google has decommissioned. A decommissioned runtime cannot be
  # created or redeployed and receives no platform security updates, so an apply that named one
  # would fail while leaving any already-deployed function frozen on unpatched software.
  validation {
    condition = !contains([
      "nodejs6", "nodejs8", "nodejs10", "nodejs12", "nodejs14", "nodejs16",
      "python37", "python38",
      "go111", "go113", "go116",
      "java11",
      "dotnet3",
      "ruby26", "ruby27",
      "php74",
    ], var.function_runtime)
    error_message = "function_runtime names a decommissioned Cloud Functions runtime. Google no longer permits creating or redeploying functions on it, and it receives no security updates. Choose a currently supported runtime."
  }
}

variable "function_source_bucket" {
  description = "Name of the EXISTING private Cloud Storage bucket holding the function's deployable source archive. The archive is a build output, so it is uploaded by the operator or the build pipeline rather than created by this configuration. It must not be the static-assets bucket, which grants allUsers read"
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

variable "https_cutover_enabled" {
  description = "Whether the port-80 HTTP-to-HTTPS redirect listener exists. Apply this configuration in two stages, because a Google-managed certificate is validated through the load balancer and so cannot be ACTIVE before the 443 listener exists. Stage one: leave this false, apply, and point the domain's DNS A record at the excel-app-lb-ip address; the 443 listener is created and the certificate begins provisioning, while port 80 has no listener so nothing is redirected to a certificate that cannot yet serve. Stage two: once `gcloud compute ssl-certificates describe excel-app-ssl-cert` reports ACTIVE, set this true and apply again to add the redirect. Provisioning commonly takes up to an hour"
  type        = bool
  default     = false
}

variable "csp_report_only" {
  description = "Whether the static single-page application origin emits its Content-Security-Policy as Content-Security-Policy-Report-Only, which reports violations without blocking them. Mirrors the backend csp_report_only setting; keep the two equal so the application and the API enforce the same mode"
  type        = bool
  default     = false
}
