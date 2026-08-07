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
variable "domain_name" {
  description = "The fully qualified domain name, without scheme or path, served by the HTTPS load balancer and named in the Google-managed SSL certificate"
  type        = string
}

variable "allowed_origins" {
  description = "Scheme-qualified browser origins (for example, https://app.example.com) permitted to call the API. Mirrors the backend ALLOWED_ORIGINS setting; the scheme is stripped when deriving the Identity Platform authorized domains"
  type        = list(string)
  validation {
    condition     = alltrue([for origin in var.allowed_origins : can(regex("^https?://[^/]+$", origin))])
    error_message = "Allowed origins must be scheme-qualified with no trailing path, for example https://app.example.com."
  }
}

variable "signer_service_account" {
  description = "The email address of the dedicated service account that signs V4 Cloud Storage signed URLs for the uploads bucket"
  type        = string
}
