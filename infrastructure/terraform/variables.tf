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
  description = "Exact browser origins (for example, https://app.example.com) permitted to call the API. Each entry is scheme://host[:port] and nothing else. Mirrors the backend ALLOWED_ORIGINS setting, which enforces the same grammar. An Identity Platform authorized domain is a host without a port, so a consumer deriving that list must parse the host out of each origin rather than only stripping the scheme"
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
    error_message = "Allowed origins must use https, except http://localhost, http://127.0.0.1 and http://[::1] for local development. A plaintext origin is modifiable in transit and must not be trusted to call the API."
  }
}

variable "signer_service_account" {
  description = "The email address of the dedicated service account that signs V4 Cloud Storage signed URLs for the uploads bucket. Must equal the backend signer_service_account setting, so the identity granted roles/iam.serviceAccountTokenCreator here is the identity the application names when it signs"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", var.signer_service_account))
    error_message = "Signer service account must be a user-managed service account email of the form NAME@PROJECT_ID.iam.gserviceaccount.com."
  }
}
