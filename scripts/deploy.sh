#!/bin/bash
#
# Deploys the Excel Clone application.
#
# The HTTPS edge - address, certificate, backend bucket, URL maps, target proxies and
# forwarding rules - and the Cloud Function are owned exclusively by
# infrastructure/terraform. Apply that first; this script creates none of them and only
# verifies that they exist.
#
# Every check in the PREFLIGHT section runs before the first mutation. Nothing is built,
# published, pushed or applied until the whole preflight has passed.

set -e

fail() {
    echo "" >&2
    echo "DEPLOYMENT ABORTED: $1" >&2
    shift
    while [ "$#" -gt 0 ]; do
        echo "  $1" >&2
        shift
    done
    exit 1
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# SECURITY: the project and region are supplied explicitly - both defaulted to a hardcoded
# value, so a run with an unset environment published the application, its image and its
# Firestore rules into whichever project those names resolved to rather than the one Terraform
# provisioned.
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to the Google Cloud project Terraform provisioned. There is deliberately no default: a default silently targets the wrong project}"
REGION="${REGION:?Set REGION to the region Terraform provisioned, matching the region Terraform variable. There is deliberately no default}"

# The domain the managed SSL certificate is issued for and the load balancer serves. It must
# equal the domain_name Terraform variable. Required: the only supported way to reach the
# application is through the load balancer, so there is no default to fall back to.
DOMAIN_NAME="${DOMAIN_NAME:?Set DOMAIN_NAME to the domain served by the HTTPS load balancer, matching the domain_name Terraform variable}"

# Identities and placement. These must equal the Terraform variables of the same name, because
# the preflight compares the deployed pod spec against them.
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:?Set RUNTIME_SERVICE_ACCOUNT to the runtime_service_account Terraform variable, the identity the pods authenticate as through Workload Identity}"
KUBERNETES_NAMESPACE="${KUBERNETES_NAMESPACE:-default}"
KUBERNETES_SERVICE_ACCOUNT="${KUBERNETES_SERVICE_ACCOUNT:-excel-app-backend}"
DB_USER="${DB_USER:?Set DB_USER to the db_user Terraform variable, the login the backend connects to Cloud SQL as}"

# Resources Terraform names. Overriding any of these means checking something other than what
# Terraform provisioned, so they track the Terraform resource names.
GKE_CLUSTER="${GKE_CLUSTER:-primary-cluster}"
SQL_INSTANCE="${SQL_INSTANCE:-main-instance}"
K8S_MANIFEST_DIR="${K8S_MANIFEST_DIR:-k8s}"

# The bucket infrastructure/terraform creates and the load-balancer backend bucket serves.
# SECURITY: the compiled SPA is published here and nowhere else - it was published to a
# separate bucket the load balancer does not serve, so the security headers the balancer
# adds were absent from whatever users actually loaded.
STATIC_ASSETS_BUCKET="${PROJECT_ID}-static-assets"

FUNCTION_NAME="excel-app-function"

# Authenticate with Google Cloud
gcloud auth login

gcloud config set project "$PROJECT_ID"
gcloud config set compute/region "$REGION"

# ===========================================================================
# PREFLIGHT - reads state, mutates nothing, and aborts before any mutation
# ===========================================================================

# --- 1. The supplied project and region are the ones Terraform provisioned --
# SECURITY: the supplied values are confirmed against infrastructure that only Terraform
# creates, so a wrong project or region aborts here instead of being discovered after the
# image, the compiled application and the Firestore rules have already been published to it.
echo "Preflight: verifying PROJECT_ID and REGION against provisioned infrastructure..."

OBSERVED_SQL_REGION="$(gcloud sql instances describe "$SQL_INSTANCE" \
    --project="$PROJECT_ID" --format='value(region)' 2>/dev/null || true)"
if [ -z "$OBSERVED_SQL_REGION" ]; then
    fail "Cloud SQL instance '$SQL_INSTANCE' was not found in project '$PROJECT_ID'." \
        "Either PROJECT_ID names the wrong project, or infrastructure/terraform has not been applied." \
        "Apply Terraform first; this script provisions nothing."
fi
if [ "$OBSERVED_SQL_REGION" != "$REGION" ]; then
    fail "REGION mismatch: REGION is '$REGION' but '$SQL_INSTANCE' is in '$OBSERVED_SQL_REGION'." \
        "Set REGION to the region Terraform provisioned, so every regional lookup below reads the right resources."
fi

if ! gcloud container clusters describe "$GKE_CLUSTER" \
    --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    fail "GKE cluster '$GKE_CLUSTER' was not found in project '$PROJECT_ID' region '$REGION'." \
        "This is google_container_cluster.primary in infrastructure/terraform. Apply Terraform first."
fi

if ! gsutil ls -b "gs://${STATIC_ASSETS_BUCKET}" >/dev/null 2>&1; then
    fail "Static assets bucket gs://${STATIC_ASSETS_BUCKET} does not exist." \
        "It is google_storage_bucket.static_assets and is the origin the load balancer serves." \
        "Publishing anywhere else would bypass the load balancer, and with it every security response header."
fi
echo "  project '$PROJECT_ID' and region '$REGION' match the provisioned infrastructure."

# --- 2. The HTTPS edge, before anything is published ------------------------
# SECURITY: TLS terminates at the edge and port 80 answers only with a redirect - the edge
# previously served application traffic in cleartext through an HTTP proxy on port 80, and
# this script created that listener itself.
# SECURITY: the edge is verified before any publication or cutover - the certificate was
# checked only after the application, the image, the Kubernetes manifests and the Firestore
# rules had already been deployed, so a still-provisioning certificate was discovered with the
# release already live and unreachable over TLS.
# infrastructure/terraform/main.tf is the sole creator of every edge resource checked below.
# Each check reads state and creates nothing; a mismatch aborts before the first mutation.
echo "Preflight: verifying the HTTPS edge..."

# resource|name|scope|field|expected substring
EDGE_EXPECTATIONS="\
ssl-certificates|excel-app-ssl-cert|--global|managed.status|ACTIVE
backend-buckets|excel-app-backend-bucket||bucketName|$STATIC_ASSETS_BUCKET
url-maps|excel-app-url-map|--global|defaultService|excel-app-backend-bucket
url-maps|excel-app-url-map|--global|defaultCustomErrorResponsePolicy.errorResponseRules[0].path|/index.html
url-maps|excel-app-https-redirect-url-map|--global|defaultUrlRedirect.httpsRedirect|True
target-https-proxies|excel-app-https-proxy|--global|sslCertificates|excel-app-ssl-cert
target-https-proxies|excel-app-https-proxy|--global|urlMap|excel-app-url-map
target-http-proxies|excel-app-http-proxy|--global|urlMap|excel-app-https-redirect-url-map
forwarding-rules|excel-app-https-forwarding-rule|--global|loadBalancingScheme|EXTERNAL_MANAGED
forwarding-rules|excel-app-https-forwarding-rule|--global|portRange|443
forwarding-rules|excel-app-https-forwarding-rule|--global|target|excel-app-https-proxy"

while IFS='|' read -r resource name scope field expected; do
    if [ -n "$scope" ]; then
        observed="$(gcloud compute "$resource" describe "$name" "$scope" --format="value($field)" || true)"
    else
        observed="$(gcloud compute "$resource" describe "$name" --format="value($field)" || true)"
    fi
    case "$observed" in
        *"$expected"*) ;;
        *)
            fail "Edge resource $resource/$name: expected $field to contain '$expected', observed '${observed:-nothing}'." \
                "Apply infrastructure/terraform before running this script; it is the sole creator of the edge." \
                "A managed certificate stays PROVISIONING until the DNS A record for its domain points at excel-app-lb-ip, so confirm DNS and wait for ACTIVE."
            ;;
    esac
done <<EOF
$EDGE_EXPECTATIONS
EOF

# SECURITY: the certificate is confirmed to be issued for the domain this deployment
# advertises - the domain was read and printed but never compared, so a certificate issued for
# a different domain passed the check and every client reached the application over a name the
# certificate does not cover.
EDGE_DOMAIN="$(gcloud compute ssl-certificates describe excel-app-ssl-cert --global \
    --format='value(managed.domains[0])' || true)"
if [ "$EDGE_DOMAIN" != "$DOMAIN_NAME" ]; then
    fail "Certificate domain mismatch: excel-app-ssl-cert covers '${EDGE_DOMAIN:-nothing}' but DOMAIN_NAME is '$DOMAIN_NAME'." \
        "DOMAIN_NAME must equal the domain_name Terraform variable, which is the single domain this certificate is issued for."
fi

# SECURITY: every forwarding rule is confirmed to answer on the reserved address - a rule
# holding some other address serves the domain's traffic from an endpoint outside this
# configuration, where none of the security response headers is added.
LB_IP="$(gcloud compute addresses describe excel-app-lb-ip --global --format='value(address)' || true)"
if [ -z "$LB_IP" ]; then
    fail "Global address excel-app-lb-ip was not found." \
        "It is google_compute_global_address.excel_app_lb and is the address both forwarding rules must answer on."
fi

HTTPS_RULE_IP="$(gcloud compute forwarding-rules describe excel-app-https-forwarding-rule \
    --global --format='value(IPAddress)' || true)"
if [ "$HTTPS_RULE_IP" != "$LB_IP" ]; then
    fail "excel-app-https-forwarding-rule answers on '${HTTPS_RULE_IP:-nothing}', not on excel-app-lb-ip '$LB_IP'."
fi

# The port-80 redirect rule is the second stage of the Terraform cutover
# (https_cutover_enabled), so it legitimately does not exist yet on a first deployment. It is
# checked only when present; port 80 serves no content either way.
HTTP_RULE_IP="$(gcloud compute forwarding-rules describe excel-app-http-forwarding-rule \
    --global --format='value(IPAddress)' 2>/dev/null || true)"
if [ -n "$HTTP_RULE_IP" ]; then
    if [ "$HTTP_RULE_IP" != "$LB_IP" ]; then
        fail "excel-app-http-forwarding-rule answers on '$HTTP_RULE_IP', not on excel-app-lb-ip '$LB_IP'."
    fi
    HTTP_RULE_PORTS="$(gcloud compute forwarding-rules describe excel-app-http-forwarding-rule \
        --global --format='value(portRange)' || true)"
    case "$HTTP_RULE_PORTS" in
        *80*) ;;
        *) fail "excel-app-http-forwarding-rule does not listen on port 80 (observed '${HTTP_RULE_PORTS:-nothing}')." ;;
    esac
    HTTP_REDIRECT_ONLY="1"
else
    HTTP_REDIRECT_ONLY=""
    echo "  note: the port-80 redirect rule does not exist yet. Terraform creates it in the"
    echo "        second stage of the cutover, once the certificate reports ACTIVE"
    echo "        (https_cutover_enabled = true). Port 80 serves no content in the meantime."
fi
echo "  HTTPS edge verified for https://$EDGE_DOMAIN on $LB_IP"

# --- 3. The frontend build agrees with the project, domain and served policy -
# SECURITY: the compiled application's project and API origin are compared with the deployed
# infrastructure - they were independent build-time inputs, so a bundle could be published
# that authenticates against another Firebase project, or calls an API origin the served
# Content-Security-Policy does not admit, and the failure appeared only in the browser.
# Only non-secret build values are read; every REACT_APP_* value is public by design because
# react-scripts inlines it into the bundle.
echo "Preflight: verifying the frontend build values..."
FRONTEND_ENV="${FRONTEND_ENV:-frontend/.env}"
if [ ! -f "$FRONTEND_ENV" ]; then
    fail "Frontend build configuration '$FRONTEND_ENV' not found." \
        "The compiled bundle takes its Firebase project and API base URL from this file, and they must" \
        "agree with PROJECT_ID and the served Content-Security-Policy. See .env.example section E."
fi

read_build_value() {
    grep -E "^[[:space:]]*$1[[:space:]]*=" "$FRONTEND_ENV" 2>/dev/null |
        tail -n 1 | cut -d= -f2- | tr -d '"'"'"' \t\r'
}

BUILD_FIREBASE_PROJECT="$(read_build_value REACT_APP_FIREBASE_PROJECT_ID)"
BUILD_API_BASE_URL="$(read_build_value REACT_APP_API_BASE_URL)"

if [ "$BUILD_FIREBASE_PROJECT" != "$PROJECT_ID" ]; then
    fail "REACT_APP_FIREBASE_PROJECT_ID is '${BUILD_FIREBASE_PROJECT:-unset}' but PROJECT_ID is '$PROJECT_ID'." \
        "A Firebase ID token names its issuing project, and the API pins verification to PROJECT_ID," \
        "so a bundle signing in against another project produces tokens every API call rejects."
fi

if [ -z "$BUILD_API_BASE_URL" ]; then
    fail "REACT_APP_API_BASE_URL is not set in '$FRONTEND_ENV'." \
        "The bundle cannot reach the API without it, and its origin is what the Content-Security-Policy must admit."
fi

# The ORIGIN is scheme://host[:port]; the policy matches origins, not paths.
BUILD_API_ORIGIN="$(printf '%s\n' "$BUILD_API_BASE_URL" | sed -E 's#^([a-zA-Z][a-zA-Z0-9+.-]*://[^/]+).*#\1#')"
case "$BUILD_API_ORIGIN" in
    https://*) ;;
    http://localhost*|http://127.0.0.1*|http://\[::1\]*)
        fail "REACT_APP_API_BASE_URL origin '$BUILD_API_ORIGIN' is a loopback address." \
            "A deployed bundle cannot call the API on the developer's own machine." ;;
    *)
        fail "REACT_APP_API_BASE_URL origin '$BUILD_API_ORIGIN' is not https." \
            "A plaintext API origin exposes every request, and its bearer token, to any network position." ;;
esac

# SECURITY: the origin the bundle calls is confirmed to be admitted by the policy the load
# balancer actually serves, read from the live configuration rather than assumed.
SERVED_HEADERS="$(gcloud compute backend-buckets describe excel-app-backend-bucket \
    --format='value(customResponseHeaders)' || true)"
case "$SERVED_HEADERS" in
    *Content-Security-Policy*) ;;
    *) fail "The load balancer's backend bucket serves no Content-Security-Policy header." \
        "It is set from local.security_response_headers in infrastructure/terraform/main.tf." ;;
esac
if [ "$BUILD_API_ORIGIN" != "https://${DOMAIN_NAME}" ]; then
    case "$SERVED_HEADERS" in
        *"$BUILD_API_ORIGIN"*) ;;
        *)
            fail "The served Content-Security-Policy does not admit '$BUILD_API_ORIGIN'." \
                "Set the api_origin Terraform variable to that origin and re-apply, so connect-src admits it." \
                "Otherwise a browser enforcing the policy blocks every API call the application makes."
            ;;
    esac
fi
echo "  frontend build targets project '$PROJECT_ID' and API origin '$BUILD_API_ORIGIN', both admitted."

# --- 4. The Kubernetes manifests carry the identity and database path -------
# SECURITY: credentials are fetched for the cluster Terraform provisioned and the manifests are
# checked before they are applied - the manifests were applied to whichever cluster the local
# kubeconfig happened to point at, so a deployment could land in an unrelated or production
# cluster, and nothing confirmed the pods requested the identity that authorizes signing.
echo "Preflight: verifying the Kubernetes manifests..."
if [ ! -f "${K8S_MANIFEST_DIR}/deployment.yaml" ] || [ ! -f "${K8S_MANIFEST_DIR}/service.yaml" ]; then
    fail "Kubernetes manifests not found: '${K8S_MANIFEST_DIR}/deployment.yaml' and '${K8S_MANIFEST_DIR}/service.yaml' are both required." \
        "They are not present in this repository and are not created by this script or by Terraform." \
        "Supply them, or point K8S_MANIFEST_DIR at the directory that holds them. They must:" \
        "  - set spec.template.spec.serviceAccountName to '$KUBERNETES_SERVICE_ACCOUNT'" \
        "  - declare that ServiceAccount in namespace '$KUBERNETES_NAMESPACE' annotated" \
        "    iam.gke.io/gcp-service-account: $RUNTIME_SERVICE_ACCOUNT" \
        "  - provide a Cloud SQL connectivity path, either a cloud-sql-proxy sidecar or a" \
        "    private-IP route to '$SQL_INSTANCE'"
fi

MANIFESTS="$(cat "${K8S_MANIFEST_DIR}/deployment.yaml" "${K8S_MANIFEST_DIR}/service.yaml")"

if ! printf '%s\n' "$MANIFESTS" | grep -qE "serviceAccountName:[[:space:]]*[\"']?${KUBERNETES_SERVICE_ACCOUNT}[\"']?[[:space:]]*$"; then
    fail "The manifests do not set serviceAccountName to '$KUBERNETES_SERVICE_ACCOUNT'." \
        "Without it the pods run as the namespace default service account, which holds no Workload Identity" \
        "binding, so the backend cannot read its secrets and cannot sign object URLs."
fi

if ! printf '%s\n' "$MANIFESTS" | grep -q "iam.gke.io/gcp-service-account:[[:space:]]*${RUNTIME_SERVICE_ACCOUNT}"; then
    fail "No ServiceAccount in the manifests is annotated iam.gke.io/gcp-service-account: $RUNTIME_SERVICE_ACCOUNT." \
        "Workload Identity binds the Kubernetes service account to the Google service account through that" \
        "annotation, and google_service_account_iam_member.api_runtime_workload_identity authorizes exactly" \
        "'$KUBERNETES_NAMESPACE/$KUBERNETES_SERVICE_ACCOUNT'. Both halves must agree or the pods hold no Google identity."
fi

# SECURITY: the pod spec is confirmed to carry a Cloud SQL connectivity path, and the login it
# uses is confirmed to exist - the instance refuses unencrypted connections and no route or
# database user was provisioned, so the API started and then failed every database call.
SQL_CONNECTION_NAME="$(gcloud sql instances describe "$SQL_INSTANCE" \
    --project="$PROJECT_ID" --format='value(connectionName)' || true)"
if ! printf '%s\n' "$MANIFESTS" | grep -qE "cloud-sql-proxy|cloudsql|${SQL_CONNECTION_NAME}"; then
    fail "The manifests provide no Cloud SQL connectivity path for '$SQL_INSTANCE'." \
        "Add a cloud-sql-proxy sidecar for connection name '$SQL_CONNECTION_NAME', or route the pods to the" \
        "instance's private IP. The instance is provisioned ENCRYPTED_ONLY, and the backend passes" \
        "sslmode from its db_sslmode setting, so the connection is encrypted either way - but it must exist."
fi

if ! gcloud sql users list --instance="$SQL_INSTANCE" --project="$PROJECT_ID" \
    --format='value(name)' 2>/dev/null | grep -qx "$DB_USER"; then
    fail "Database user '$DB_USER' does not exist on '$SQL_INSTANCE'." \
        "It is google_sql_user.app in infrastructure/terraform, created from the db_user and db_password" \
        "variables. The DATABASE_URL secret names this login, so without it every database call fails."
fi
echo "  manifests request '$KUBERNETES_SERVICE_ACCOUNT', annotate '$RUNTIME_SERVICE_ACCOUNT', and reach '$SQL_INSTANCE'."

# --- 5. The browser can use the Firestore rules about to be deployed --------
# SECURITY: the rules are deployed only when the browser's document reference can actually
# resolve under them - the rules were deployed while the client built an invalid reference, so
# the collaboration path was broken and the rules protecting it were never exercised.
echo "Preflight: verifying the browser Firestore reference..."
COLLAB_CLIENT="frontend/src/services/collaboration.ts"
if [ ! -f "$COLLAB_CLIENT" ]; then
    fail "'$COLLAB_CLIENT' not found; it is the only browser reader of the collaboration store."
fi
if grep -qE "collection\([[:space:]]*db[[:space:]]*,[[:space:]]*'workbooks'[[:space:]]*," "$COLLAB_CLIENT"; then
    fail "'$COLLAB_CLIENT' builds a collection reference with an even number of path segments." \
        "workbooks/{workbookId} names a document, so Firestore rejects the reference before any rule is" \
        "evaluated. Use doc(db, 'workbooks', workbookId) so it matches /workbooks/{workbookId} in" \
        "firestore.rules and the path backend/app/services/real_time_sync.py writes."
fi
if ! grep -qE "doc\([[:space:]]*db[[:space:]]*,[[:space:]]*'workbooks'" "$COLLAB_CLIENT"; then
    fail "'$COLLAB_CLIENT' does not build a document reference under 'workbooks'." \
        "The deployed rules match /workbooks/{workbookId}; a client reading any other path is denied by default."
fi
echo "  browser reference resolves under /workbooks/{workbookId}."

echo ""
echo "Preflight passed. Beginning deployment."
echo ""

# ===========================================================================
# MUTATIONS - nothing above this line changes any state
# ===========================================================================

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
# SECURITY: the manifests are applied to the cluster Terraform provisioned, named explicitly,
# rather than to whichever cluster the ambient kubeconfig selected.
echo "Deploying backend to Google Kubernetes Engine..."
gcloud container clusters get-credentials "$GKE_CLUSTER" --region="$REGION" --project="$PROJECT_ID"
kubectl apply -f "${K8S_MANIFEST_DIR}/deployment.yaml"
kubectl apply -f "${K8S_MANIFEST_DIR}/service.yaml"

# The Cloud Function is deployed by infrastructure/terraform, which owns its name, region,
# runtime and source archive. This script deploys no function: two deployers of one function
# from two sources cannot agree on which artifact is running.
# SECURITY: any allUsers invoker binding an earlier deployment created is revoked here. The
# Terraform binding is additive, so it grants the intended caller without removing a public
# binding that already exists.
echo "Revoking any public invoker binding on ${FUNCTION_NAME}..."
INVOKER_MEMBERS="$(gcloud functions get-iam-policy "$FUNCTION_NAME" --region="$REGION" \
    --flatten='bindings[].members' \
    --filter='bindings.role:roles/cloudfunctions.invoker' \
    --format='value(bindings.members)' 2>/dev/null || true)"

if printf '%s\n' "$INVOKER_MEMBERS" | grep -qx "allUsers"; then
    echo "  allUsers holds the invoker role; removing it."
    gcloud functions remove-iam-policy-binding "$FUNCTION_NAME" \
        --region="$REGION" \
        --member="allUsers" \
        --role="roles/cloudfunctions.invoker" \
        --quiet
else
    echo "  no allUsers invoker binding is present."
fi

# SECURITY: the removal is confirmed by re-reading the policy, and the deployment aborts if
# allUsers still holds the invoker role - a failed removal was previously reported as "no
# binding present", so a publicly invocable function passed the check silently.
INVOKER_MEMBERS_AFTER="$(gcloud functions get-iam-policy "$FUNCTION_NAME" --region="$REGION" \
    --flatten='bindings[].members' \
    --filter='bindings.role:roles/cloudfunctions.invoker' \
    --format='value(bindings.members)' 2>/dev/null || true)"
if printf '%s\n' "$INVOKER_MEMBERS_AFTER" | grep -qx "allUsers"; then
    fail "allUsers still holds roles/cloudfunctions.invoker on ${FUNCTION_NAME}." \
        "The function is invocable by anyone who discovers its URL. Remove the binding before deploying:" \
        "  gcloud functions remove-iam-policy-binding $FUNCTION_NAME --region=$REGION --member=allUsers --role=roles/cloudfunctions.invoker"
fi

echo "Invoker identities for ${FUNCTION_NAME}:"
printf '%s\n' "${INVOKER_MEMBERS_AFTER:-  (none)}"

# Apply database migrations
echo "Applying database migrations..."
# HUMAN ASSISTANCE NEEDED
# Please specify the database migration tool and commands to be used

# Update Google Cloud Firestore security rules
echo "Updating Firestore security rules..."
# SECURITY: the document-level authorization rules are deployed to the same project the API
# verifies tokens for - the Firebase CLI selects its project from its own active project or
# --project, not from the gcloud configuration set above, and the repository declares no
# .firebaserc. Authenticate non-interactively with FIREBASE_TOKEN or
# GOOGLE_APPLICATION_CREDENTIALS before running this script.
firebase deploy --only firestore:rules --project "$PROJECT_ID" --non-interactive

# Run post-deployment tests
echo "Running post-deployment tests..."
# HUMAN ASSISTANCE NEEDED
# Please specify the test runner and commands to be used for post-deployment tests

# Print deployment status and any necessary manual steps
echo "Deployment completed successfully!"
echo ""
echo "The application is served ONLY at https://${DOMAIN_NAME}."
# SECURITY: the direct Cloud Storage URL is not advertised - custom response headers are
# added by the load balancer, so an object fetched straight from storage.googleapis.com
# carries none of them. It was previously the advertised entry point.
echo "Do not use a direct storage.googleapis.com URL: the security response headers are"
echo "added by the load balancer and are absent from a direct bucket request."
echo ""
echo "Please perform the following manual steps:"
echo "1. Confirm https://${DOMAIN_NAME} serves the application, including a deep link such as"
echo "   https://${DOMAIN_NAME}/workbooks reloaded directly in the browser"
if [ -n "$HTTP_REDIRECT_ONLY" ]; then
    echo "2. Confirm http://${DOMAIN_NAME} returns a 301 redirect to https"
else
    echo "2. Set https_cutover_enabled = true in Terraform and apply, now that the certificate is"
    echo "   ACTIVE, then confirm http://${DOMAIN_NAME} returns a 301 redirect to https"
fi
echo "3. Confirm the response from https://${DOMAIN_NAME} carries Content-Security-Policy,"
echo "   Strict-Transport-Security, X-Frame-Options, X-Content-Type-Options,"
echo "   Referrer-Policy and Permissions-Policy"
echo "4. Check the backend services are running correctly in GKE, and that the pods run as"
echo "   ${KUBERNETES_SERVICE_ACCOUNT} bound to ${RUNTIME_SERVICE_ACCOUNT} through Workload"
echo "   Identity, so signed-URL generation can call signBlob"
echo "5. Confirm ${FUNCTION_NAME} refuses an unauthenticated request and accepts one"
echo "   carrying an identity token for ${RUNTIME_SERVICE_ACCOUNT}"
echo "6. Verify database migrations were applied successfully"
echo "7. Confirm Firestore security rules are in effect"
echo "8. Confirm an uploaded object is NOT readable without a signed URL"
