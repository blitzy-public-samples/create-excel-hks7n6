#!/bin/bash
#
# Deploys the Excel Clone application.
#
# The HTTPS edge — address, certificate, backend bucket, URL maps, target proxies and
# forwarding rules — is owned exclusively by infrastructure/terraform. Apply that first;
# this script creates none of it and only verifies that it exists.
# Rationale: documentation/Security Decision Log.md R12, R13, R20.

set -e

# Authenticate with Google Cloud
gcloud auth login

# Set GCP project and region. Override either through the environment.
PROJECT_ID="${PROJECT_ID:-excel-app-project}"
REGION="${REGION:-us-central1}"

# The domain the managed SSL certificate is issued for and the load balancer serves. It must
# equal the domain_name Terraform variable. Required: the only supported way to reach the
# application is through the load balancer, so there is no default to fall back to.
DOMAIN_NAME="${DOMAIN_NAME:?Set DOMAIN_NAME to the domain served by the HTTPS load balancer, matching the domain_name Terraform variable}"

# The bucket infrastructure/terraform creates and the load-balancer backend bucket serves.
# SECURITY: the compiled SPA is published here and nowhere else — it was published to a
# separate bucket the load balancer does not serve, so the security headers the balancer
# adds were absent from whatever users actually loaded.
STATIC_ASSETS_BUCKET="${PROJECT_ID}-static-assets"

gcloud config set project "$PROJECT_ID"
gcloud config set compute/region "$REGION"

# Build frontend assets
echo "Building frontend assets..."
cd frontend
npm run build
cd ..

# Deploy frontend to the Terraform-managed static assets bucket
echo "Publishing frontend to gs://${STATIC_ASSETS_BUCKET}..."
gsutil -m rsync -r frontend/build "gs://${STATIC_ASSETS_BUCKET}"

# Build and push backend Docker image
echo "Building and pushing backend Docker image..."
cd backend
docker build -t "gcr.io/${PROJECT_ID}/excel-app-backend:latest" .
docker push "gcr.io/${PROJECT_ID}/excel-app-backend:latest"
cd ..

# Deploy backend to Google Kubernetes Engine
echo "Deploying backend to Google Kubernetes Engine..."
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml

# Update Google Cloud Functions
# The name and region here are the ones infrastructure/terraform binds the named invoker to.
echo "Updating Google Cloud Functions..."
FUNCTION_NAME="excel-app-function"

# SECURITY: this deployment requests no unauthenticated invoker binding - it previously
# requested one, binding the invoker role to allUsers.
# Name, region and generation match google_cloudfunctions_function.excel_app_function in
# infrastructure/terraform/main.tf, which binds roles/cloudfunctions.invoker to the intended
# service account. --no-gen2 keeps this a generation 1 function; gcloud now defaults to
# generation 2, which would be a different resource.
gcloud functions deploy "$FUNCTION_NAME" \
    --region="$REGION" \
    --no-gen2 \
    --runtime nodejs14 \
    --trigger-http \
    --no-allow-unauthenticated \
    --source functions/

# SECURITY: any allUsers invoker binding an earlier deployment created is revoked here —
# the explicit no-unauthenticated flag above declines to add a public binding but revokes
# none that already exists, so a previously public function stayed public.
# The command fails when the binding is absent, which is the desired end state, so that
# outcome is reported and not treated as an error.
echo "Revoking any public invoker binding on ${FUNCTION_NAME}..."
if gcloud functions remove-iam-policy-binding "$FUNCTION_NAME" \
    --region="$REGION" \
    --member="allUsers" \
    --role="roles/cloudfunctions.invoker" \
    --quiet >/dev/null 2>&1; then
    echo "  Removed an allUsers invoker binding."
else
    echo "  No allUsers invoker binding present."
fi

# The intended caller is granted roles/cloudfunctions.invoker declaratively by
# infrastructure/terraform; this script grants nothing.
echo "Invoker identities for ${FUNCTION_NAME}:"
gcloud functions get-iam-policy "$FUNCTION_NAME" --region="$REGION" \
    --flatten='bindings[].members' \
    --filter='bindings.role:roles/cloudfunctions.invoker' \
    --format='value(bindings.members)'

# Apply database migrations
echo "Applying database migrations..."
# HUMAN ASSISTANCE NEEDED
# Please specify the database migration tool and commands to be used

# Update Google Cloud Firestore security rules
echo "Updating Firestore security rules..."
# SECURITY: the document-level authorization rules are deployed to the same project the API
# verifies tokens for — the Firebase CLI selects its project from its own active project or
# --project, not from the gcloud configuration set above, and the repository declares no
# .firebaserc. Authenticate non-interactively with FIREBASE_TOKEN or
# GOOGLE_APPLICATION_CREDENTIALS before running this script.
firebase deploy --only firestore:rules --project "$PROJECT_ID" --non-interactive

# Verify the Google Cloud CDN edge
echo "Verifying the HTTPS edge..."
# SECURITY: TLS terminates at the edge and port 80 answers only with a redirect — the edge
# previously served application traffic in cleartext through an HTTP proxy on port 80, and
# this script created that listener itself.
# infrastructure/terraform/main.tf is the sole creator of every edge resource checked below
# (address excel-app-lb-ip, certificate excel-app-ssl-cert, backend bucket, both URL maps,
# both target proxies and both global forwarding rules). Apply it before running this
# script. Each check below reads state and creates nothing; a mismatch aborts the deployment.
# resource|name|scope|field|expected substring
EDGE_EXPECTATIONS="\
ssl-certificates|excel-app-ssl-cert|--global|managed.status|ACTIVE
backend-buckets|excel-app-backend-bucket||bucketName|$STATIC_ASSETS_BUCKET
url-maps|excel-app-url-map|--global|defaultService|excel-app-backend-bucket
url-maps|excel-app-https-redirect-url-map|--global|defaultUrlRedirect.httpsRedirect|True
target-https-proxies|excel-app-https-proxy|--global|sslCertificates|excel-app-ssl-cert
target-https-proxies|excel-app-https-proxy|--global|urlMap|excel-app-url-map
target-http-proxies|excel-app-http-proxy|--global|urlMap|excel-app-https-redirect-url-map
forwarding-rules|excel-app-https-forwarding-rule|--global|loadBalancingScheme|EXTERNAL_MANAGED
forwarding-rules|excel-app-https-forwarding-rule|--global|portRange|443
forwarding-rules|excel-app-https-forwarding-rule|--global|target|excel-app-https-proxy
forwarding-rules|excel-app-http-forwarding-rule|--global|loadBalancingScheme|EXTERNAL_MANAGED
forwarding-rules|excel-app-http-forwarding-rule|--global|portRange|80
forwarding-rules|excel-app-http-forwarding-rule|--global|target|excel-app-http-proxy"

while IFS='|' read -r resource name scope field expected; do
    if [ -n "$scope" ]; then
        observed="$(gcloud compute "$resource" describe "$name" "$scope" --format="value($field)" || true)"
    else
        observed="$(gcloud compute "$resource" describe "$name" --format="value($field)" || true)"
    fi
    case "$observed" in
        *"$expected"*) ;;
        *)
            echo "Edge resource $resource/$name: expected $field to contain '$expected', observed '${observed:-nothing}'." >&2
            echo "Apply infrastructure/terraform before deploying this script; it is the sole creator of the edge." >&2
            echo "A managed certificate stays PROVISIONING until the DNS A record for its domain points at excel-app-lb-ip, so confirm DNS and wait for ACTIVE." >&2
            exit 1
            ;;
    esac
done <<EOF
$EDGE_EXPECTATIONS
EOF
EDGE_DOMAIN="$(gcloud compute ssl-certificates describe excel-app-ssl-cert --global --format='value(managed.domains[0])')"
echo "HTTPS edge verified for https://$EDGE_DOMAIN"

# Run post-deployment tests
echo "Running post-deployment tests..."
# HUMAN ASSISTANCE NEEDED
# Please specify the test runner and commands to be used for post-deployment tests

# Print deployment status and any necessary manual steps
echo "Deployment completed successfully!"
echo ""
echo "The application is served ONLY at https://${DOMAIN_NAME}."
# SECURITY: the direct Cloud Storage URL is deliberately not advertised — custom response
# headers are added by the load balancer, so an object fetched straight from
# storage.googleapis.com carries none of them. It was previously the advertised entry point.
echo "Do not use a direct storage.googleapis.com URL: the security response headers are"
echo "added by the load balancer and are absent from a direct bucket request."
echo ""
echo "Please perform the following manual steps:"
echo "1. Confirm https://${DOMAIN_NAME} serves the application"
echo "2. Confirm http://${DOMAIN_NAME} returns a 301 redirect to https"
echo "3. Confirm the response from https://${DOMAIN_NAME} carries Content-Security-Policy,"
echo "   Strict-Transport-Security, X-Frame-Options, X-Content-Type-Options,"
echo "   Referrer-Policy and Permissions-Policy"
echo "4. Check the backend services are running correctly in GKE, and that the pods run as"
echo "   the Kubernetes service account bound to the runtime service account through"
echo "   Workload Identity, so signed-URL generation can call signBlob"
echo "5. Confirm ${FUNCTION_NAME} refuses an unauthenticated request and accepts one"
echo "   carrying an identity token for the invoker service account"
echo "6. Verify database migrations were applied successfully"
echo "7. Confirm Firestore security rules are in effect"
echo "8. Confirm an uploaded object is NOT readable without a signed URL"
