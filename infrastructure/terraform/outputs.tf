output "database_connection_string" {
  description = "Connection string for the database"
  value       = google_sql_database_instance.main.connection_name
  sensitive   = true
}

output "storage_bucket_urls" {
  description = "URLs of the created storage buckets"
  value = {
    raw_data     = google_storage_bucket.raw_data.url
    processed_data = google_storage_bucket.processed_data.url
    model_artifacts = google_storage_bucket.model_artifacts.url
  }
}

output "kubernetes_cluster_endpoint" {
  description = "Endpoint for the Kubernetes cluster"
  value       = google_container_cluster.primary.endpoint
}

output "cloud_functions_urls" {
  description = "URLs for the deployed Cloud Functions"
  value = {
    data_ingestion  = google_cloudfunctions_function.data_ingestion.https_trigger_url
    data_processing = google_cloudfunctions_function.data_processing.https_trigger_url
    model_training  = google_cloudfunctions_function.model_training.https_trigger_url
  }
}

output "firestore_database_name" {
  description = "Name of the Firestore database"
  value       = google_firestore_database.main.name
}

# HUMAN ASSISTANCE NEEDED
# Please verify that the resource names (e.g., google_sql_database_instance.main, google_storage_bucket.raw_data, etc.) 
# match the actual resource names defined in other Terraform files. 
# Also, ensure that all necessary resources are included in the outputs.