variable "project_id" {
  description = "The Google Cloud Project ID"
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

  validation {
    condition     = endswith(var.signer_service_account, "@${var.project_id}.iam.gserviceaccount.com")
    error_message = "Signer service account must belong to project_id: the address must end @PROJECT_ID.iam.gserviceaccount.com, because this configuration creates the account in project_id. An address in another project names no account that exists here."
  }
}

variable "runtime_service_account" {
  description = "The email address of the service account the backend API authenticates as, reached from the GKE workload through Workload Identity. This account is the IAM MEMBER granted roles/iam.serviceAccountTokenCreator on signer_service_account, which is what authorizes the signBlob call that mints a signed URL. Set it equal to signer_service_account only if the runtime is to sign as itself"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.runtime_service_account))
    error_message = "Runtime service account must be a user-managed service account email of the form NAME@PROJECT_ID.iam.gserviceaccount.com."
  }

  validation {
    condition     = endswith(var.runtime_service_account, "@${var.project_id}.iam.gserviceaccount.com")
    error_message = "Runtime service account must belong to project_id: the address must end @PROJECT_ID.iam.gserviceaccount.com, because this configuration creates the account in project_id."
  }
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

# Identity Platform Google sign-in credentials.
# main.tf has referenced both of these since the initial commit without either being
# declared, which made `terraform validate` fail before any plan could be reviewed.
variable "google_oauth_client_id" {
  description = "OAuth 2.0 client ID of the Google identity provider configured in Identity Platform"
  type        = string

  validation {
    condition     = can(regex("^[0-9A-Za-z._-]+\\.apps\\.googleusercontent\\.com$", var.google_oauth_client_id))
    error_message = "Google OAuth client ID must be of the form NNNNNN-XXXX.apps.googleusercontent.com."
  }
}

variable "google_oauth_client_secret" {
  description = "OAuth 2.0 client secret of the Google identity provider configured in Identity Platform. Supply it from a secret store rather than a checked-in tfvars file"
  type        = string
  # SECURITY: the value is withheld from plan and apply output — a client secret printed
  # into CI logs is a disclosed credential
  sensitive = true

  validation {
    condition     = length(var.google_oauth_client_secret) > 0
    error_message = "Google OAuth client secret must not be empty; Identity Platform rejects an empty secret."
  }
}

variable "csp_report_only" {
  description = "Whether the static single-page application origin emits its Content-Security-Policy as Content-Security-Policy-Report-Only, which reports violations without blocking them. Mirrors the backend csp_report_only setting; keep the two equal so the application and the API enforce the same mode"
  type        = bool
  default     = false
}
