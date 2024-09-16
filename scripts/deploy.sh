#!/bin/bash

set -e

# Authenticate with Google Cloud
gcloud auth login

# Set GCP project and region
PROJECT_ID="excel-app-project"
REGION="us-central1"
gcloud config set project $PROJECT_ID
gcloud config set compute/region $REGION

# Build frontend assets
echo "Building frontend assets..."
cd frontend
npm run build
cd ..

# Deploy frontend to Google Cloud Storage
echo "Deploying frontend to Google Cloud Storage..."
gsutil -m rsync -r frontend/build gs://excel-app-frontend

# Build and push backend Docker image
echo "Building and pushing backend Docker image..."
cd backend
docker build -t gcr.io/$PROJECT_ID/excel-app-backend:latest .
docker push gcr.io/$PROJECT_ID/excel-app-backend:latest
cd ..

# Deploy backend to Google Kubernetes Engine
echo "Deploying backend to Google Kubernetes Engine..."
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml

# Update Google Cloud Functions
echo "Updating Google Cloud Functions..."
gcloud functions deploy excel-app-function \
    --runtime nodejs14 \
    --trigger-http \
    --allow-unauthenticated \
    --source functions/

# Apply database migrations
echo "Applying database migrations..."
# HUMAN ASSISTANCE NEEDED
# Please specify the database migration tool and commands to be used

# Update Google Cloud Firestore security rules
echo "Updating Firestore security rules..."
firebase deploy --only firestore:rules

# Configure Google Cloud CDN
echo "Configuring Google Cloud CDN..."
gcloud compute backend-buckets create excel-app-backend-bucket --gcs-bucket-name=excel-app-frontend
gcloud compute url-maps create excel-app-url-map --default-backend-bucket=excel-app-backend-bucket
gcloud compute target-http-proxies create excel-app-http-proxy --url-map=excel-app-url-map
gcloud compute forwarding-rules create excel-app-http-forwarding-rule --target-http-proxy=excel-app-http-proxy --ports=80

# Run post-deployment tests
echo "Running post-deployment tests..."
# HUMAN ASSISTANCE NEEDED
# Please specify the test runner and commands to be used for post-deployment tests

# Print deployment status and any necessary manual steps
echo "Deployment completed successfully!"
echo "Please perform the following manual steps:"
echo "1. Verify the frontend is accessible at https://excel-app-frontend.storage.googleapis.com"
echo "2. Check the backend services are running correctly in GKE"
echo "3. Test the Google Cloud Functions"
echo "4. Verify database migrations were applied successfully"
echo "5. Confirm Firestore security rules are in effect"
echo "6. Test the CDN configuration"