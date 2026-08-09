#!/bin/bash
#
# Deploys the Excel Clone application.
#
# The HTTPS edge - address, certificate, backend bucket, URL maps, target proxies and
# forwarding rules - and the Cloud Function are owned exclusively by
# infrastructure/terraform. Apply that first; this script creates none of them and only
# verifies that they exist.
#
# The whole PREFLIGHT section runs before any application or cloud-resource deployment
# mutation: nothing is built, published, pushed or applied until it has passed. It is not
# the first thing the script does - `gcloud auth login` and the two `gcloud config set`
# calls above it authenticate and change local gcloud configuration.

# ---------------------------------------------------------------------------
# KNOWN BLOCKERS - this script cannot complete on the repository as it stands
# ---------------------------------------------------------------------------
# Read this before running it. Each item below is a defect in a file this change set is not
# permitted to edit, and each one stops the deployment. They are stated here rather than
# discovered one at a time, and none of them is worked around: a check that passed anyway would
# be reporting a control it had not verified.
#
# 1. The backend image builds but cannot start. infrastructure/docker/Dockerfile.backend ends
#    with `CMD ["uvicorn", "main:app", ...]`, and there is no main.py at the context root; the
#    module is backend/app/main.py, which additionally imports `backend.app.*` while no
#    directory under backend/ carries an __init__.py. The pods will CrashLoopBackOff. Required
#    changes, in files outside this change set: correct the CMD to the real module path and add
#    the missing __init__.py files.
#
# 2. k8s/deployment.yaml and k8s/service.yaml are not in this repository. Preflight 4 aborts
#    until they are supplied, or K8S_MANIFEST_DIR points at a directory that holds them.
#
# 3. DB_MIGRATION_COMMAND and POST_DEPLOY_TEST_COMMAND have no value this repository can supply:
#    it declares no migration tool and no post-deployment test suite. Both are required inputs
#    below, so the script stops in the Configuration section rather than printing a success
#    banner over two steps it silently skipped.
#
# CLOSED, and recorded here because earlier revisions of this header listed it as blocker 1:
# frontend/src/services/collaboration.ts no longer builds an even-segment
# `collection(db, 'workbooks', workbookId)` reference. It builds
# `doc(db, 'workbooks', workbookId)`, which is the path /workbooks/{workbookId} that
# firestore.rules matches and backend/app/services/real_time_sync.py writes, so the browser can
# exercise the deployed rules. Preflight 5 checks the file rather than trusting this note, and
# reports as a warning rather than an abort - see the Security Decision Log, D53.

# -e stops on the first failing command, -u refuses an unset variable rather than expanding it
# to the empty string, and -o pipefail makes a pipeline fail when any element of it fails.
# Without -u, a mistyped variable name silently became "" - so a check comparing an observed
# value against an empty expectation passed, and a gcloud invocation lost an argument, in both
# cases reporting a control it had not verified. Without -o pipefail, only the LAST command in a
# pipeline decided the exit status, so a failing read feeding a filter that succeeded on no input
# also passed. Every place that legitimately tolerates a non-matching filter says so explicitly.
set -euo pipefail

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

# A finding that must be seen but must not stop the deployment. Reserved for a defect in a
# module this change set is not permitted to modify, where aborting would make every
# deployment impossible without fixing the very file that may not be touched.
warn() {
    echo "" >&2
    echo "WARNING: $1" >&2
    shift
    while [ "$#" -gt 0 ]; do
        echo "  $1" >&2
        shift
    done
    echo "" >&2
}

# SECURITY: a read that fails is not evidence that the thing being read is absent.
# `... 2>/dev/null || true` made an expired credential, a missing permission, a disabled API
# and a wrong project indistinguishable from "not there", so a check whose safe answer is the
# empty string - most importantly the allUsers invoker lookup - passed on a failed read and
# reported a possibly-public function as private.
#
# Usage:
#     read_gcloud "<what is being read>" gcloud ...        # then use $READ_GCLOUD_VALUE
#   return 0  the read succeeded; the value is in READ_GCLOUD_VALUE and may legitimately be empty
#   return 2  the resource genuinely does not exist; the caller decides whether that is allowed
#   aborts    every other failure, quoting what gcloud reported
#
# The value is published in READ_GCLOUD_VALUE rather than written to stdout. A
# `VALUE="$(read_gcloud ...)"` capture would run the helper in a subshell, where fail()'s exit
# ends only that subshell and the caller carries on with an empty value.
#
# stdin is closed for the command, so a read inside a `while read` loop cannot consume the loop's
# input and no invocation can block waiting for a prompt.
READ_GCLOUD_VALUE=""
read_gcloud() {
    local description="$1"
    shift
    READ_GCLOUD_VALUE=""
    local stderr_file
    stderr_file="$(mktemp)"
    local output
    if output="$("$@" 2>"$stderr_file" </dev/null)"; then
        rm -f "$stderr_file"
        READ_GCLOUD_VALUE="$output"
        return 0
    fi
    local message
    message="$(cat "$stderr_file")"
    rm -f "$stderr_file"
    case "$message" in
        *"was not found"*|*"NOT_FOUND"*|*"not found"*|*"does not exist"*)
            return 2
            ;;
    esac
    fail "Could not read $description." \
        "gcloud reported: ${message:-no error output}" \
        "This script will not treat a failed read as a passing check, so it stops here." \
        "Confirm the active credentials, the enabled APIs and that PROJECT_ID names the project Terraform provisioned."
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# SECURITY: the project and region are supplied explicitly, with no default, so an unset
# environment aborts here rather than publishing the application, its image and its Firestore
# rules into whichever project a default name would resolve to.
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to the Google Cloud project Terraform provisioned. There is deliberately no default: a default silently targets the wrong project}"
REGION="${REGION:?Set REGION to the region Terraform provisioned, matching the region Terraform variable. There is deliberately no default}"

# The domain the managed SSL certificate is issued for and the load balancer serves. It must
# equal the domain_name Terraform variable. Required: the only supported way to reach the
# application is through the load balancer, so there is no default to fall back to.
DOMAIN_NAME="${DOMAIN_NAME:?Set DOMAIN_NAME to the domain served by the HTTPS load balancer, matching the domain_name Terraform variable}"

# Identity and placement. These must equal the Terraform variables named beside them, because
# the preflight compares the deployed pod spec and the live IAM policy against them.
#
# ONE service account. infrastructure/terraform creates a single google_service_account, the API
# runs as it and signs object URLs as itself, and every grant is made to it: signBlob on itself,
# object admin on the uploads bucket, Cloud SQL client, the three secret versions, Firestore, the
# Firebase Authentication read, and the Workload Identity binding checked below. There is
# deliberately no second "runtime" address to configure - a separate runtime identity would have
# to be named in a backend setting so the application knew to sign as something other than
# itself, and no such setting exists.
SIGNER_SERVICE_ACCOUNT="${SIGNER_SERVICE_ACCOUNT:?Set SIGNER_SERVICE_ACCOUNT to the signer_service_account Terraform variable: the one identity the pods authenticate as through Workload Identity and sign object URLs as}"
KUBERNETES_NAMESPACE="${KUBERNETES_NAMESPACE:-default}"
KUBERNETES_SERVICE_ACCOUNT="${KUBERNETES_SERVICE_ACCOUNT:-excel-app-backend}"
# The login the backend connects to Cloud SQL as. It is set here rather than read from
# Terraform because there is no db_user variable to copy it from: the login is provisioned by
# an operator, not by that configuration, which would otherwise hold its password in state.
# The authoritative sources are the DATABASE_URL secret, which carries the login the
# application actually uses, and the instance's user list - and the preflight compares this
# value against both.
DB_USER="${DB_USER:?Set DB_USER to the database login the backend connects to Cloud SQL as. It must equal the username in the DATABASE_URL secret and exist on the instance; the preflight checks both}"
# The database the backend connects to. Defaults to the same literal as the db_name
# Terraform variable, so an unset value checks what a default apply provisions; override it
# here whenever db_name was overridden there.
DB_NAME="${DB_NAME:-main-database}"

# Resources Terraform names. Overriding any of these means checking something other than what
# Terraform provisioned, so they track the Terraform resource names.
GKE_CLUSTER="${GKE_CLUSTER:-primary-cluster}"
SQL_INSTANCE="${SQL_INSTANCE:-main-instance}"
K8S_MANIFEST_DIR="${K8S_MANIFEST_DIR:-k8s}"

# The bucket infrastructure/terraform creates and the load-balancer backend bucket serves.
# SECURITY: the compiled SPA is published to this bucket and nowhere else, because it is the
# only origin the load balancer serves and therefore the only path that carries the security
# response headers.
STATIC_ASSETS_BUCKET="${PROJECT_ID}-static-assets"

FUNCTION_NAME="excel-app-function"

# TRUST BOUNDARY for the two values below. Both are shell command lines, and both are executed
# with `eval` further down, so whatever supplies them can run arbitrary commands with this
# script's privileges - which include the active gcloud credential and cluster access. They are
# specified as operator input, typed by the person running the deployment, and that person
# already has those privileges directly; `eval` grants nothing they did not have.
# The boundary is therefore: these two variables must come from a human operator or from a
# secret store only operators can write. Do NOT source either from a pull request, a webhook
# payload, a repository file a contributor can edit, or any other value an untrusted party can
# influence - doing so turns a deployment into remote command execution. There is deliberately no
# default: a shell string is not something this script may invent on an operator's behalf.
DB_MIGRATION_COMMAND="${DB_MIGRATION_COMMAND:?Set DB_MIGRATION_COMMAND to the command that migrates the database schema. This repository declares no migration tool, so there is nothing to default to - see KNOWN BLOCKERS at the top of this script}"
POST_DEPLOY_TEST_COMMAND="${POST_DEPLOY_TEST_COMMAND:?Set POST_DEPLOY_TEST_COMMAND to the command that exercises the deployed release. This repository declares no post-deployment suite, so there is nothing to default to - see KNOWN BLOCKERS at the top of this script}"

# Authenticate with Google Cloud.
# An interactive browser sign-in is attempted only when no credential is already active, so a
# service account activated beforehand - or an already-signed-in operator - is used as-is.
# `gcloud auth login` unconditionally opened a browser prompt and blocked, which made this
# script impossible to run unattended: the deployment either hung or the operator worked around
# it, and a preflight nobody can run verifies nothing.
if [ -z "$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)" ]; then
    gcloud auth login
fi

gcloud config set project "$PROJECT_ID"
gcloud config set compute/region "$REGION"

# ===========================================================================
# PREFLIGHT - reads state, mutates nothing, and aborts before any mutation
# ===========================================================================

# --- 1. The supplied project and region are the ones Terraform provisioned --
# SECURITY: the supplied values are confirmed against infrastructure that only Terraform
# creates, so a wrong project or region aborts before the image, the compiled application and
# the Firestore rules are published to it.
echo "Preflight: verifying PROJECT_ID and REGION against provisioned infrastructure..."

read_gcloud "Cloud SQL instance '$SQL_INSTANCE'" \
    gcloud sql instances describe "$SQL_INSTANCE" \
    --project="$PROJECT_ID" --format='value(region)' || true
OBSERVED_SQL_REGION="$READ_GCLOUD_VALUE"
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

# SECURITY: the cluster is confirmed to be registered in the project's workload identity pool.
# This is the half of Workload Identity that the pod spec cannot supply and that the node pool's
# GKE_METADATA mode does not imply: with no workload pool the metadata server has nothing to
# exchange a pod's Kubernetes token against, so the backend obtains no Google credentials at all -
# it cannot read its secrets, cannot open the Cloud SQL proxy connection and cannot sign an object
# URL. The symptom is every request failing after a deployment that otherwise looked correct, so it
# is worth one read here rather than a debugging session afterwards.
read_gcloud "the workload identity pool of '$GKE_CLUSTER'" \
    gcloud container clusters describe "$GKE_CLUSTER" \
    --region="$REGION" --project="$PROJECT_ID" \
    --format='value(workloadIdentityConfig.workloadPool)' || true
OBSERVED_WORKLOAD_POOL="$READ_GCLOUD_VALUE"
EXPECTED_WORKLOAD_POOL="${PROJECT_ID}.svc.id.goog"
if [ "$OBSERVED_WORKLOAD_POOL" != "$EXPECTED_WORKLOAD_POOL" ]; then
    fail "GKE cluster '$GKE_CLUSTER' reports workload pool '${OBSERVED_WORKLOAD_POOL:-none}', not '$EXPECTED_WORKLOAD_POOL'." \
        "It is the workload_identity_config block on google_container_cluster.primary in" \
        "infrastructure/terraform. Apply Terraform before deploying: without the pool the pods hold" \
        "no Google identity and every request fails, authentication included."
fi

# SECURITY: the Kubernetes service account the pods will request is confirmed to be authorized to
# act as the runtime identity. Enabling the pool above grants nothing on its own - this binding is
# what authorizes the exchange - and the pod annotation checked later is only a pointer, not the
# grant. Reading the live policy is what separates "Terraform declares it" from "it is applied".
read_gcloud "the workload identity members of '$SIGNER_SERVICE_ACCOUNT'" \
    gcloud iam service-accounts get-iam-policy "$SIGNER_SERVICE_ACCOUNT" \
    --project="$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter='bindings.role:roles/iam.workloadIdentityUser' \
    --format='value(bindings.members)' || true
WORKLOAD_IDENTITY_MEMBERS="$READ_GCLOUD_VALUE"
EXPECTED_WORKLOAD_IDENTITY_MEMBER="serviceAccount:${PROJECT_ID}.svc.id.goog[${KUBERNETES_NAMESPACE}/${KUBERNETES_SERVICE_ACCOUNT}]"
if ! printf '%s\n' "$WORKLOAD_IDENTITY_MEMBERS" | grep -qxF "$EXPECTED_WORKLOAD_IDENTITY_MEMBER"; then
    fail "'$EXPECTED_WORKLOAD_IDENTITY_MEMBER' does not hold roles/iam.workloadIdentityUser on '$SIGNER_SERVICE_ACCOUNT'." \
        "It is google_service_account_iam_member.api_runtime_workload_identity in" \
        "infrastructure/terraform, whose member is built from project_id, kubernetes_namespace and" \
        "kubernetes_service_account. KUBERNETES_NAMESPACE and KUBERNETES_SERVICE_ACCOUNT here must" \
        "equal those Terraform variables and the deployed pod spec, or the pods hold no Google identity."
fi
echo "  '$GKE_CLUSTER' is in pool '$EXPECTED_WORKLOAD_POOL' and '$KUBERNETES_NAMESPACE/$KUBERNETES_SERVICE_ACCOUNT' may act as '$SIGNER_SERVICE_ACCOUNT'."

if ! gsutil ls -b "gs://${STATIC_ASSETS_BUCKET}" >/dev/null 2>&1; then
    fail "Static assets bucket gs://${STATIC_ASSETS_BUCKET} does not exist." \
        "It is google_storage_bucket.static_assets and is the origin the load balancer serves." \
        "Publishing anywhere else would bypass the load balancer, and with it every security response header."
fi
echo "  project '$PROJECT_ID' and region '$REGION' match the provisioned infrastructure."

# --- 2. The HTTPS edge, before anything is published ------------------------
# SECURITY: TLS terminates at the edge and port 80 answers only with a redirect. This script
# creates no listener of either kind.
# SECURITY: the edge is verified before anything is published, so a still-provisioning
# certificate aborts the run rather than being discovered with the release already live and
# unreachable over TLS.
# infrastructure/terraform/main.tf is the sole creator of every edge resource checked below.
# Each check reads state and creates nothing; a mismatch aborts before any deployment
# mutation.
echo "Preflight: verifying the HTTPS edge..."

# resource|name|scope|field|expected substring
# The certificate's managed.status is NOT in this table: it is asserted exactly, immediately
# below, because it is the one value Terraform cannot check for itself and a substring match is
# too loose for a gate the whole deployment depends on.
#
# SECURITY: each forwarding rule's target is matched on the full resource path segment rather
# than on a bare name. Nothing previously verified which proxy the port-80 rule pointed AT - the
# table checked that excel-app-http-proxy carries the redirect URL map, and separately that a
# rule on port 80 existed, but never that the two were connected. A port-80 rule aimed at
# excel-app-https-proxy therefore passed every check while serving the application itself in
# cleartext, which is the exact exposure the redirect exists to remove.
# The path segment also disambiguates the two proxies: matching a bare "excel-app-http-proxy"
# against a target URL is safe only by the accident that the https proxy's name has an 's' in
# that position, and matching "/targetHttpProxies/" does not depend on that.
EDGE_EXPECTATIONS="\
backend-buckets|excel-app-backend-bucket||bucketName|$STATIC_ASSETS_BUCKET
url-maps|excel-app-url-map|--global|defaultService|excel-app-backend-bucket
url-maps|excel-app-url-map|--global|defaultCustomErrorResponsePolicy.errorResponseRules[0].path|/index.html
url-maps|excel-app-https-redirect-url-map|--global|defaultUrlRedirect.httpsRedirect|True
target-https-proxies|excel-app-https-proxy|--global|sslCertificates|excel-app-ssl-cert
target-https-proxies|excel-app-https-proxy|--global|urlMap|excel-app-url-map
target-http-proxies|excel-app-http-proxy|--global|urlMap|excel-app-https-redirect-url-map
forwarding-rules|excel-app-https-forwarding-rule|--global|loadBalancingScheme|EXTERNAL_MANAGED
forwarding-rules|excel-app-https-forwarding-rule|--global|portRange|443
forwarding-rules|excel-app-https-forwarding-rule|--global|target|/targetHttpsProxies/excel-app-https-proxy
forwarding-rules|excel-app-http-forwarding-rule|--global|portRange|80
forwarding-rules|excel-app-http-forwarding-rule|--global|target|/targetHttpProxies/excel-app-http-proxy"

while IFS='|' read -r resource name scope field expected; do
    if [ -n "$scope" ]; then
        read_gcloud "edge resource $resource/$name" \
            gcloud compute "$resource" describe "$name" "$scope" --format="value($field)" || true
    else
        read_gcloud "edge resource $resource/$name" \
            gcloud compute "$resource" describe "$name" --format="value($field)" || true
    fi
    observed="$READ_GCLOUD_VALUE"
    case "$observed" in
        *"$expected"*) ;;
        *)
            fail "Edge resource $resource/$name: expected $field to contain '$expected', observed '${observed:-nothing}'." \
                "Apply infrastructure/terraform before running this script; it is the sole creator of the edge."
            ;;
    esac
done <<EOF
$EDGE_EXPECTATIONS
EOF

# SECURITY: the managed certificate is confirmed to be serving before anything is published.
# Terraform cannot make this check: the managed certificate resource exposes no status
# attribute, and its subject_alternative_names are populated when it is created rather than
# when it starts serving. The status is read from the live resource here and compared exactly.
if ! read_gcloud "the managed certificate excel-app-ssl-cert" \
    gcloud compute ssl-certificates describe excel-app-ssl-cert --global \
    --format='value(managed.status)'; then
    fail "The managed certificate excel-app-ssl-cert does not exist." \
        "It is google_compute_managed_ssl_certificate.excel_app in infrastructure/terraform. Apply Terraform first."
fi
CERT_STATUS="$READ_GCLOUD_VALUE"
if [ "$CERT_STATUS" != "ACTIVE" ]; then
    fail "The managed certificate excel-app-ssl-cert is '${CERT_STATUS:-unknown}', not ACTIVE." \
        "TLS cannot be served until it is, so nothing is published." \
        "A managed certificate stays PROVISIONING until the DNS A record for its domain points at" \
        "excel-app-lb-ip and the 443 listener answers there; provisioning commonly takes up to an hour." \
        "Both listeners already exist - Terraform creates them in one apply - so there is nothing to" \
        "enable and no second apply to perform. Point DNS at excel-app-lb-ip, wait for this status to" \
        "become ACTIVE, then run this script again."
fi

# SECURITY: the certificate is confirmed to be issued for the domain this deployment
# advertises, so clients cannot be sent to a name the certificate does not cover.
read_gcloud "the domain excel-app-ssl-cert covers" \
    gcloud compute ssl-certificates describe excel-app-ssl-cert --global \
    --format='value(managed.domains[0])' || true
EDGE_DOMAIN="$READ_GCLOUD_VALUE"
if [ "$EDGE_DOMAIN" != "$DOMAIN_NAME" ]; then
    fail "Certificate domain mismatch: excel-app-ssl-cert covers '${EDGE_DOMAIN:-nothing}' but DOMAIN_NAME is '$DOMAIN_NAME'." \
        "DOMAIN_NAME must equal the domain_name Terraform variable, which is the single domain this certificate is issued for."
fi

# SECURITY: every forwarding rule is confirmed to answer on the reserved address. A rule
# holding some other address would serve the domain's traffic from an endpoint outside this
# configuration, where none of the security response headers is added.
read_gcloud "the global address excel-app-lb-ip" \
    gcloud compute addresses describe excel-app-lb-ip --global --format='value(address)' || true
LB_IP="$READ_GCLOUD_VALUE"
if [ -z "$LB_IP" ]; then
    fail "Global address excel-app-lb-ip was not found." \
        "It is google_compute_global_address.excel_app_lb and is the address both forwarding rules must answer on."
fi

read_gcloud "the 443 forwarding rule's address" \
    gcloud compute forwarding-rules describe excel-app-https-forwarding-rule \
    --global --format='value(IPAddress)' || true
HTTPS_RULE_IP="$READ_GCLOUD_VALUE"
if [ "$HTTPS_RULE_IP" != "$LB_IP" ]; then
    fail "excel-app-https-forwarding-rule answers on '${HTTPS_RULE_IP:-nothing}', not on excel-app-lb-ip '$LB_IP'."
fi

# SECURITY: the port-80 redirect rule is required, not optional - its absence leaves the
# plaintext port with no listener, which a client experiences as a connection failure on the
# http:// URL rather than as an upgrade to TLS, so a bookmarked or typed plaintext address
# simply breaks instead of being redirected.
# Terraform creates both forwarding rules in one apply and there is no toggle that omits
# either, so absence here means Terraform was not applied, not that a later stage is pending.
# Treated as a hard failure for that reason.
if ! read_gcloud "the port-80 forwarding rule's address" \
    gcloud compute forwarding-rules describe excel-app-http-forwarding-rule \
    --global --format='value(IPAddress)'; then
    fail "The port-80 forwarding rule excel-app-http-forwarding-rule does not exist." \
        "It is google_compute_global_forwarding_rule.excel_app_http in infrastructure/terraform," \
        "which creates it unconditionally alongside the 443 rule in a single apply." \
        "Without it port 80 has no listener at all, so a plaintext request fails to connect" \
        "instead of being redirected to https. Apply Terraform before running this script."
fi
HTTP_RULE_IP="$READ_GCLOUD_VALUE"
if [ "$HTTP_RULE_IP" != "$LB_IP" ]; then
    fail "excel-app-http-forwarding-rule answers on '${HTTP_RULE_IP:-nothing}', not on excel-app-lb-ip '$LB_IP'."
fi
read_gcloud "the port-80 forwarding rule's port range" \
    gcloud compute forwarding-rules describe excel-app-http-forwarding-rule \
    --global --format='value(portRange)' || true
HTTP_RULE_PORTS="$READ_GCLOUD_VALUE"
case "$HTTP_RULE_PORTS" in
    *80*) ;;
    *) fail "excel-app-http-forwarding-rule does not listen on port 80 (observed '${HTTP_RULE_PORTS:-nothing}')." ;;
esac

read_gcloud "the port-80 forwarding rule's target" \
    gcloud compute forwarding-rules describe excel-app-http-forwarding-rule \
    --global --format='value(target)' || true
HTTP_RULE_TARGET="$READ_GCLOUD_VALUE"
case "$HTTP_RULE_TARGET" in
    *excel-app-http-proxy*) ;;
    *)
        fail "excel-app-http-forwarding-rule targets '${HTTP_RULE_TARGET:-nothing}', not excel-app-http-proxy." \
            "Only that proxy carries the redirect URL map, so any other target means port 80 answers" \
            "with something other than a redirect to https."
        ;;
esac
# SECURITY: the port-80 listener is confirmed to be a redirect and nothing else - a rule
# pointing at the application's own url map would serve the whole application in cleartext on
# the same address, which is the exposure the redirect exists to remove.
read_gcloud "the port-80 proxy's url map" \
    gcloud compute target-http-proxies describe excel-app-http-proxy \
    --global --format='value(urlMap)' || true
HTTP_PROXY_URL_MAP="$READ_GCLOUD_VALUE"
case "$HTTP_PROXY_URL_MAP" in
    *excel-app-https-redirect-url-map*) ;;
    *)
        fail "excel-app-http-proxy serves url map '${HTTP_PROXY_URL_MAP:-nothing}', not excel-app-https-redirect-url-map." \
            "The plaintext listener must serve only the redirect map; serving the application's own map" \
            "would publish every response over http on the same address."
        ;;
esac
echo "  HTTPS edge verified for https://$EDGE_DOMAIN on $LB_IP, port 80 redirect-only"

# --- 3. The frontend build agrees with the project, domain and served policy -
# SECURITY: the compiled application's project and API origin are compared with the deployed
# infrastructure, so a bundle that authenticates against another Firebase project, or calls an
# API origin the served Content-Security-Policy does not admit, aborts here rather than
# failing in the browser.
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
    # A key this file does not carry yields the empty string, and the checks below name which
    # value is missing. grep exits 1 on no match, so the tolerance is confined to grep alone -
    # a failure of tail, cut or tr still aborts under `set -o pipefail`.
    { grep -E "^[[:space:]]*$1[[:space:]]*=" "$FRONTEND_ENV" 2>/dev/null || true; } |
        tail -n 1 | cut -d= -f2- | tr -d '"'"'"' \t\r'
}

BUILD_FIREBASE_PROJECT="$(read_build_value REACT_APP_FIREBASE_PROJECT_ID)"
BUILD_API_BASE_URL="$(read_build_value REACT_APP_API_BASE_URL)"

if [ "$BUILD_FIREBASE_PROJECT" != "$PROJECT_ID" ]; then
    fail "REACT_APP_FIREBASE_PROJECT_ID is '${BUILD_FIREBASE_PROJECT:-unset}' but PROJECT_ID is '$PROJECT_ID'." \
        "A Firebase ID token names its issuing project. PROJECT_ID is the one project this deployment" \
        "accepts tokens from: the backend firebase_project_id setting is refused at start-up unless it is" \
        "empty or equal to PROJECT_ID, so it can never widen the accepted issuer beyond this value." \
        "A bundle signing in against another project therefore produces tokens every API call rejects."
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

# SECURITY: the bundle is refused an API origin that this edge serves, because this edge routes
# no API path to the API service - the URL map has one backend, the static bucket, and rewrites
# an unmatched path to /index.html. A bundle calling its own origin would receive that document
# under status 200 and read it as API data.
if [ "$BUILD_API_ORIGIN" = "https://${DOMAIN_NAME}" ]; then
    fail "REACT_APP_API_BASE_URL origin '$BUILD_API_ORIGIN' is the origin this edge serves the application from." \
        "That origin serves static files only: google_compute_url_map.excel_app has one backend, the static" \
        "bucket, and rewrites an unmatched path to /index.html with status 200. API calls would receive the" \
        "application document instead of API responses. Point REACT_APP_API_BASE_URL at the API's own origin" \
        "and set the api_origin Terraform variable to it."
fi

# SECURITY: the origin the bundle calls is confirmed to be admitted by the policy the load
# balancer actually serves, read from the live configuration rather than assumed.
read_gcloud "the backend bucket's custom response headers" \
    gcloud compute backend-buckets describe excel-app-backend-bucket \
    --format='value(customResponseHeaders)' || true
SERVED_HEADERS="$READ_GCLOUD_VALUE"
case "$SERVED_HEADERS" in
    *Content-Security-Policy*) ;;
    *) fail "The load balancer's backend bucket serves no Content-Security-Policy header." \
        "It is set from local.security_response_headers in infrastructure/terraform/main.tf." ;;
esac

# SECURITY: the five fixed security response headers are compared against their CANONICAL
# VALUES, not merely for the presence of their names. A header present under the right name
# carrying a value that protects nothing - SAMEORIGIN where DENY is required, max-age=300 where
# two years is, unsafe-url where strict-origin-when-cross-origin is - is precisely what a
# name-only check cannot see, and it would have shipped reported as "confirmed".
# These are the literal entries of local.security_response_headers in
# infrastructure/terraform/main.tf, which is the single source they are configured from.
canonical_header_value() {
    case "$1" in
        Strict-Transport-Security)
            printf '%s' 'max-age=63072000; includeSubDomains; preload' ;;
        X-Frame-Options)
            printf '%s' 'DENY' ;;
        X-Content-Type-Options)
            printf '%s' 'nosniff' ;;
        Referrer-Policy)
            printf '%s' 'strict-origin-when-cross-origin' ;;
        Permissions-Policy)
            printf '%s' 'accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()' ;;
        *)
            fail "canonical_header_value called with the unknown header '$1'." \
                "This is a defect in scripts/deploy.sh, not a deployment fault." ;;
    esac
}

# The names, in the order infrastructure/terraform/main.tf lists them. Deliberately not
# including the policy header: its value varies with api_origin and csp_report_only, so it is
# checked directive by directive below instead of against one literal.
FIXED_SECURITY_HEADERS='Strict-Transport-Security X-Frame-Options X-Content-Type-Options Referrer-Policy Permissions-Policy'

# The configured set arrives as a comma-separated list of "Name: value" entries. An entry is
# accepted only where the canonical text is followed by the list separator or ends the list, so
# a value that merely STARTS with the canonical text - nosniff extended to nosniff-and-more - is
# refused rather than passing a substring test. Permissions-Policy's own value contains commas,
# which is why the whole entry is matched literally rather than the list being split first.
configured_header_is_canonical() {
    _entry="$1: $(canonical_header_value "$1")"
    case "$SERVED_HEADERS" in
        *"$_entry") unset _entry; return 0 ;;
        *"$_entry,"*) unset _entry; return 0 ;;
    esac
    unset _entry
    return 1
}

for expected_header in $FIXED_SECURITY_HEADERS; do
    if ! configured_header_is_canonical "$expected_header"; then
        fail "The load balancer's backend bucket does not serve '$expected_header' with its required value." \
            "Required entry: $expected_header: $(canonical_header_value "$expected_header")" \
            "Configured set: $SERVED_HEADERS" \
            "The values come from local.security_response_headers in infrastructure/terraform/main.tf." \
            "Re-apply that configuration; an out-of-band edit to the backend bucket is reverted by the next apply."
    fi
done

# SECURITY: the policy's connect-src sources are compared as WHOLE TOKENS. Substring matching
# against the entire header approved an origin the policy does not admit: 'api.example.com'
# occurs inside the unrelated host 'api.example.com.evil', inside a path, and inside any other
# directive, so a blocked API origin could satisfy the gate and ship.
# The header set is a comma-separated list of "Name: value" entries, so the policy entry is
# isolated first, then its directives, then a directive's space-separated sources. Both policy
# header names are recognised, because csp_report_only selects between them and a gate that only
# understood the enforcing name would abort every report-only deployment.
csp_directives() {
    printf '%s\n' "$SERVED_HEADERS" |
        tr ',' '\n' |
        sed -n 's/^[[:space:]]*Content-Security-Policy\(-Report-Only\)\{0,1\}[[:space:]]*:[[:space:]]*//p' |
        tr ';' '\n' |
        sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

csp_admits_origin() {
    csp_directives |
        sed -n 's/^connect-src[[:space:]]\{1,\}//p' |
        tr ' \t' '\n\n' |
        grep -Fxq "$1"
}

# SECURITY: the policy directives that carry protection rather than configuration are compared
# as WHOLE DIRECTIVES, so frame-ancestors 'none' relaxed to 'self', or script-src 'self' widened
# with 'unsafe-inline', is refused. A policy stripped of frame-ancestors was one of the
# tamperings a name-only check published as verified.
csp_has_directive() {
    csp_directives | grep -Fxq "$1"
}

for required_directive in \
    "default-src 'self'" \
    "base-uri 'self'" \
    "object-src 'none'" \
    "frame-ancestors 'none'" \
    "form-action 'self'" \
    "script-src 'self'"; do
    if ! csp_has_directive "$required_directive"; then
        fail "The served Content-Security-Policy does not carry the directive \"$required_directive\"." \
            "It is assembled by local.content_security_policy in infrastructure/terraform/main.tf." \
            "A policy missing or widening this directive withdraws the protection it delivers -" \
            "frame-ancestors 'none' is what refuses framing, and script-src 'self' is what refuses" \
            "injected and inline script. Re-apply that configuration."
    fi
done

if ! csp_admits_origin "$BUILD_API_ORIGIN"; then
    fail "The served Content-Security-Policy's connect-src does not list '$BUILD_API_ORIGIN' as a source." \
        "Set the api_origin Terraform variable to exactly that origin and re-apply, so connect-src admits it." \
        "A source that merely contains the origin as a substring does not admit it: a browser matches whole" \
        "sources, so it would block every API call the application makes."
fi
echo "  frontend build targets project '$PROJECT_ID' and API origin '$BUILD_API_ORIGIN', both admitted."
echo "  the backend bucket serves all six security response headers with their required values."

# SECURITY: the API origin is proven to terminate TLS, present a certificate that covers its
# host, and refuse to serve the API in cleartext. Every check above this one reads TEXT - that
# the configured origin begins with https:// - which says what the deployment intends, not what
# the endpoint does. The API is not published through the edge this script verifies:
# google_compute_url_map.excel_app declares a default_service and no host rule, so every path
# on the edge origin resolves to the static bucket. The API's HTTPS ingress is therefore an
# EXTERNAL PREREQUISITE that no committed resource creates, and a live handshake is the only
# thing that can show it is there. Without this, a bundle configured with an https:// origin
# that in fact answers plaintext, or answers with a certificate for another name, published
# successfully and sent every bearer token into the open.
echo "Preflight: verifying the API origin terminates TLS..."
API_HOST_PORT="${BUILD_API_ORIGIN#https://}"
API_HOST="${API_HOST_PORT%%:*}"
case "$API_HOST_PORT" in
    *:*) API_PORT="${API_HOST_PORT##*:}" ;;
    *)   API_PORT="443" ;;
esac
if [ -z "$API_HOST" ]; then
    fail "Could not determine a host from the API origin '$BUILD_API_ORIGIN'."
fi

# Fail closed on a missing tool. Skipping the check because the tool is absent would report a
# verified endpoint on the strength of having verified nothing.
for TOOL in openssl curl; do
    if ! command -v "$TOOL" >/dev/null 2>&1; then
        fail "'$TOOL' is required to verify that the API origin terminates TLS, and it is not installed." \
            "This check cannot be skipped: the API's HTTPS ingress is outside Terraform, so a live" \
            "handshake is the only evidence that it exists and is correct. Install '$TOOL' and re-run."
    fi
done

# -verify_hostname makes the handshake itself fail when the presented certificate does not
# cover the name, so the handshake and the name check are one operation and neither can pass
# on the strength of the other. -verify_return_error turns a chain failure into a non-zero
# exit rather than a warning printed on a successful connection.
if ! echo | openssl s_client -connect "${API_HOST}:${API_PORT}" -servername "$API_HOST" \
    -verify_hostname "$API_HOST" -verify_return_error -brief >/dev/null 2>&1; then
    fail "The API origin '$BUILD_API_ORIGIN' did not complete a verified TLS handshake on ${API_HOST}:${API_PORT}." \
        "Either nothing is listening there, TLS is not terminated, the certificate chain does not verify," \
        "or the certificate does not cover '$API_HOST'." \
        "The API's HTTPS ingress is an external prerequisite - the Kubernetes Service or Ingress in front" \
        "of the pods - and this script cannot create it. Provision it, point DNS at it, and re-run." \
        "Publishing now would send every Firebase ID token to an endpoint that cannot protect it."
fi

# SECURITY: plaintext must not be an alternative way in. A redirect is acceptable; a served
# response is not, because a client that reaches the API over http has already transmitted its
# bearer token before any redirect is read.
API_PLAINTEXT_STATUS="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
    "http://${API_HOST}/" 2>/dev/null || echo "000")"
case "$API_PLAINTEXT_STATUS" in
    000)
        # Nothing answers on port 80. The strongest of the acceptable outcomes.
        ;;
    30[12378])
        API_PLAINTEXT_LOCATION="$(curl -s -o /dev/null -w '%{redirect_url}' --max-time 15 \
            "http://${API_HOST}/" 2>/dev/null || echo "")"
        case "$API_PLAINTEXT_LOCATION" in
            https://*) ;;
            *)
                fail "http://${API_HOST}/ redirects to '${API_PLAINTEXT_LOCATION:-nothing}', which is not https." \
                    "A redirect that does not upgrade the scheme leaves the caller on plaintext."
                ;;
        esac
        ;;
    *)
        fail "http://${API_HOST}/ answered HTTP $API_PLAINTEXT_STATUS instead of refusing the connection or redirecting to https." \
            "The API is reachable in cleartext, so a caller - or anything that can rewrite a URL in front of" \
            "one - can transmit a Firebase ID token in the open and have it accepted." \
            "Configure the API's ingress to serve https only, or to redirect http to https, and re-run."
        ;;
esac
echo "  API origin '$BUILD_API_ORIGIN' terminates TLS for '$API_HOST' and serves no plaintext."

# --- 4. The Kubernetes manifests carry the identity and database path -------
# SECURITY: credentials are fetched for the cluster Terraform provisioned, named explicitly
# rather than taken from the ambient kubeconfig, and the manifests are checked before they are
# applied, so the pods are confirmed to request the identity that authorizes signing.
echo "Preflight: verifying the Kubernetes manifests..."
if [ ! -f "${K8S_MANIFEST_DIR}/deployment.yaml" ] || [ ! -f "${K8S_MANIFEST_DIR}/service.yaml" ]; then
    fail "Kubernetes manifests not found: '${K8S_MANIFEST_DIR}/deployment.yaml' and '${K8S_MANIFEST_DIR}/service.yaml' are both required." \
        "They are not present in this repository and are not created by this script or by Terraform." \
        "Supply them, or point K8S_MANIFEST_DIR at the directory that holds them. They must:" \
        "  - set spec.template.spec.serviceAccountName to '$KUBERNETES_SERVICE_ACCOUNT'" \
        "  - declare that ServiceAccount in namespace '$KUBERNETES_NAMESPACE' annotated" \
        "    iam.gke.io/gcp-service-account: $SIGNER_SERVICE_ACCOUNT" \
        "  - run a cloud-sql-proxy sidecar for '$SQL_INSTANCE', which is the only Cloud SQL" \
        "    connectivity path this infrastructure provisions"
fi

MANIFESTS="$(cat "${K8S_MANIFEST_DIR}/deployment.yaml" "${K8S_MANIFEST_DIR}/service.yaml")"

if ! printf '%s\n' "$MANIFESTS" | grep -qE "serviceAccountName:[[:space:]]*[\"']?${KUBERNETES_SERVICE_ACCOUNT}[\"']?[[:space:]]*$"; then
    fail "The manifests do not set serviceAccountName to '$KUBERNETES_SERVICE_ACCOUNT'." \
        "Without it the pods run as the namespace default service account, which holds no Workload Identity" \
        "binding, so the backend cannot read its secrets and cannot sign object URLs."
fi

if ! printf '%s\n' "$MANIFESTS" | grep -q "iam.gke.io/gcp-service-account:[[:space:]]*${SIGNER_SERVICE_ACCOUNT}"; then
    fail "No ServiceAccount in the manifests is annotated iam.gke.io/gcp-service-account: $SIGNER_SERVICE_ACCOUNT." \
        "Workload Identity binds the Kubernetes service account to the Google service account through that" \
        "annotation, and google_service_account_iam_member.api_runtime_workload_identity authorizes exactly" \
        "'$KUBERNETES_NAMESPACE/$KUBERNETES_SERVICE_ACCOUNT'. Both halves must agree or the pods hold no Google identity."
fi

# SECURITY: the pod spec is confirmed to carry the one Cloud SQL connectivity path this
# infrastructure provisions, the runtime identity is confirmed to be authorized to use it, and
# the login is confirmed to exist. Without all three the API starts and then fails every
# database call, authentication included.
#
# There is one supported topology. The instance has no private_network block, so it is
# reachable only over its public address, and the Cloud SQL Auth Proxy is what turns that into
# an authenticated, mutually-verified TLS session. The proxy authenticates as the runtime
# identity and needs roles/cloudsql.client, which
# google_project_iam_member.api_runtime_cloudsql_client grants.
read_gcloud "the connection name of '$SQL_INSTANCE'" \
    gcloud sql instances describe "$SQL_INSTANCE" \
    --project="$PROJECT_ID" --format='value(connectionName)' || true
SQL_CONNECTION_NAME="$READ_GCLOUD_VALUE"
if [ -z "$SQL_CONNECTION_NAME" ]; then
    fail "Could not determine the connection name of '$SQL_INSTANCE'." \
        "Without it the proxy check below cannot be made at all, so this stops rather than accepting" \
        "manifests whose Cloud SQL path was never actually verified."
fi

# SECURITY: BOTH halves are required, and each is asserted separately.
# This was one condition of the form `if ! A && ! B`, which is `NOT (A OR B)` and therefore
# aborted only when BOTH were absent. Either half alone satisfied it, so two broken manifests
# passed: one naming a cloud-sql-proxy container for some OTHER instance, and one carrying this
# instance's connection name with no proxy container to dial it. The first connects the backend
# to the wrong database, the second cannot reach any database at all - and because the instance
# is ENCRYPTED_ONLY with no private network, the second has no fallback route to fail over to.
#
# grep -F on the connection name, so its colons and dots cannot act as regular-expression
# metacharacters, and an empty value can no longer widen the match to everything.
# SECURITY: BOTH markers are required, and each is asserted on its own so that the message an
# operator reads names the half that is actually missing. Combining them into one condition
# reports "no proxy" for manifests that run a proxy pointed at the wrong instance.
if ! printf '%s\n' "$MANIFESTS" | grep -q "cloud-sql-proxy"; then
    fail "The manifests run no cloud-sql-proxy container." \
        "A container image named cloud-sql-proxy is required: the pods reach '$SQL_INSTANCE'" \
        "through the Auth Proxy and by no other route." \
        "Add a cloud-sql-proxy sidecar for connection name '$SQL_CONNECTION_NAME'." \
        "A direct route is not an alternative here: the instance is provisioned with no private" \
        "network, so there is no private IP to route to, and connecting to its public address" \
        "without the proxy would need an authorized network and client certificates that this" \
        "infrastructure does not create."
fi

# grep -F on the connection name, so its colons and dots cannot act as regular-expression
# metacharacters, and an empty value can no longer widen the match to everything.
if ! printf '%s\n' "$MANIFESTS" | grep -qF "$SQL_CONNECTION_NAME"; then
    fail "The manifests name no cloud-sql-proxy target matching '$SQL_CONNECTION_NAME'." \
        "A proxy container that dials some other instance connects the backend to the wrong" \
        "database, which no later check would notice." \
        "Instruct the sidecar to open connection name '$SQL_CONNECTION_NAME'."
fi

# role|terraform resource|consequence if it is missing
REQUIRED_PROJECT_ROLES="\
roles/cloudsql.client|google_project_iam_member.api_runtime_cloudsql_client|the Cloud SQL Auth Proxy cannot connect, so the API rejects every request
roles/firebaseauth.viewer|google_project_iam_member.api_runtime_firebaseauth_viewer|the token revocation check cannot read the user record, so every authenticated request is refused
roles/datastore.user|google_project_iam_member.api_runtime_datastore_user|every collaboration write to Firestore fails"

while IFS='|' read -r required_role terraform_resource consequence; do
    read_gcloud "the project's $required_role members" \
        gcloud projects get-iam-policy "$PROJECT_ID" \
        --flatten='bindings[].members' \
        --filter="bindings.role:$required_role" \
        --format='value(bindings.members)' || true
    if ! printf '%s\n' "$READ_GCLOUD_VALUE" | grep -qxF "serviceAccount:${SIGNER_SERVICE_ACCOUNT}"; then
        fail "'$SIGNER_SERVICE_ACCOUNT' does not hold $required_role on project '$PROJECT_ID'." \
            "It is $terraform_resource in infrastructure/terraform." \
            "Without it, $consequence - apply Terraform before deploying."
    fi
done <<EOF
$REQUIRED_PROJECT_ROLES
EOF

# SECURITY: the signBlob grant is confirmed to exist on the one account, with that same account
# as its member. file_storage.py reads the signer address from the credentials attached to its
# runtime, so the workload signs as ITSELF and this account is both the member and the resource
# of the grant. Without it generate_signed_url has no way to sign - the pod holds an access token
# and no private key - so every upload is stored and then fails to return a URL.
read_gcloud "the service account's IAM policy" \
    gcloud iam service-accounts get-iam-policy "$SIGNER_SERVICE_ACCOUNT" \
    --project="$PROJECT_ID" \
# The proxy accepts the instance either as a bare positional argument or through
# --instances=CONNECTION_NAME=tcp:PORT, depending on its major version, so the line is matched
# rather than the flag. Container arguments are the lines of a YAML args/command sequence, which
# are `- value` entries, plus the inline `["a","b"]` form; a comment line is excluded explicitly so
# that documenting the connection name cannot satisfy the check.
PROXY_ARGUMENT_LINES="$(printf '%s\n' "$MANIFESTS" |
    grep -v '^[[:space:]]*#' |
    grep -E '^[[:space:]]*(-[[:space:]]|args:|command:|\[)' || true)"
if ! printf '%s\n' "$PROXY_ARGUMENT_LINES" | grep -qF "$SQL_CONNECTION_NAME"; then
    fail "No container argument in the manifests names the connection '$SQL_CONNECTION_NAME'." \
        "The proxy container must be given this instance explicitly - as a positional argument or" \
        "as --instances=${SQL_CONNECTION_NAME}=tcp:5432 - or it starts and proxies nothing, or" \
        "proxies a different instance. A mention in a comment, label or annotation connects nothing," \
        "so only argument lines are searched." \
        "'$SQL_INSTANCE' in project '$PROJECT_ID' has connection name '$SQL_CONNECTION_NAME'."
fi

# SECURITY: the runtime identity is confirmed to hold roles/cloudsql.client. The proxy
# authenticates as this account, so without the role it cannot open a connection at all and
# every request fails, authentication included: that queries the users table before any route
# body runs.
read_gcloud "the project's roles/cloudsql.client members" \
    gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter='bindings.role:roles/iam.serviceAccountTokenCreator' \
    --format='value(bindings.members)' || true
SIGNER_TOKEN_CREATORS="$READ_GCLOUD_VALUE"
if ! printf '%s\n' "$SIGNER_TOKEN_CREATORS" | grep -qxF "serviceAccount:${SIGNER_SERVICE_ACCOUNT}"; then
    fail "'$SIGNER_SERVICE_ACCOUNT' does not hold roles/iam.serviceAccountTokenCreator on itself." \
        "It is google_service_account_iam_member.url_signer_token_creator in infrastructure/terraform," \
        "granted ON this account WITH this account as its member, because the runtime signs as itself." \
        "That grant is the iam.serviceAccounts.signBlob permission a V4 signature needs when the caller" \
        "holds an access token and no private key, so without it every upload stores an object it cannot" \
        "return a URL for. Apply Terraform before deploying."
fi

# SECURITY: the Google-side half of Workload Identity is confirmed to exist, not merely requested
# by the manifests. The annotation above is a claim the cluster makes; this is the grant that
# honours it. With the annotation present and the binding absent, the pods start, hold no Google
# identity, and fail every Secret Manager read, database connection, Firestore write and signing
# call - a failure that surfaces as a running deployment serving errors rather than as a refused
# deploy.
read_gcloud "the Workload Identity bindings on '$SIGNER_SERVICE_ACCOUNT'" \
    gcloud iam service-accounts get-iam-policy "$SIGNER_SERVICE_ACCOUNT" \
    --project="$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter='bindings.role:roles/iam.workloadIdentityUser' \
    --format='value(bindings.members)' || true
WORKLOAD_IDENTITY_MEMBERS="$READ_GCLOUD_VALUE"
EXPECTED_WORKLOAD_MEMBER="serviceAccount:${PROJECT_ID}.svc.id.goog[${KUBERNETES_NAMESPACE}/${KUBERNETES_SERVICE_ACCOUNT}]"
if ! printf '%s\n' "$WORKLOAD_IDENTITY_MEMBERS" | grep -qxF "$EXPECTED_WORKLOAD_MEMBER"; then
    fail "'$EXPECTED_WORKLOAD_MEMBER' does not hold roles/iam.workloadIdentityUser on '$SIGNER_SERVICE_ACCOUNT'." \
        "It is google_service_account_iam_member.api_runtime_workload_identity in infrastructure/terraform." \
        "Apply Terraform with kubernetes_namespace='$KUBERNETES_NAMESPACE' and" \
        "kubernetes_service_account='$KUBERNETES_SERVICE_ACCOUNT' so the binding names the pods this" \
        "deployment actually runs, and confirm the cluster carries workload_identity_config - without" \
        "the workload pool the binding names a subject that cannot exist."
fi

# SECURITY: the cluster's Workload Identity pool is confirmed to be enabled. The node pool
# requests GKE_METADATA, which cannot serve a pod's own identity unless the cluster carries the
# pool; on a cluster without it the pods either hold no Google identity or fall back to the node
# service account's credentials.
read_gcloud "the cluster's Workload Identity pool" \
    gcloud container clusters describe "$GKE_CLUSTER" \
    --region="$REGION" --project="$PROJECT_ID" \
    --format='value(workloadIdentityConfig.workloadPool)' || true
CLUSTER_WORKLOAD_POOL="$READ_GCLOUD_VALUE"
if [ "$CLUSTER_WORKLOAD_POOL" != "${PROJECT_ID}.svc.id.goog" ]; then
    fail "Cluster '$GKE_CLUSTER' reports Workload Identity pool '${CLUSTER_WORKLOAD_POOL:-none}', expected '${PROJECT_ID}.svc.id.goog'." \
        "It is workload_identity_config on google_container_cluster.primary in infrastructure/terraform." \
        "Without the pool the node pool's GKE_METADATA mode cannot serve the pod's own identity, so the" \
        "annotation the manifests carry binds nothing. Apply Terraform before deploying."
fi

# SECURITY: the transport mode the pods pass to the driver is confirmed to be the one this
# topology can actually complete, and the endpoint it describes is confirmed to be local.
#
# The proxy presents a PLAIN TCP listener on loopback inside the pod and provides the
# encryption on its own leg to the instance. That listener offers no TLS and carries no
# certificate, so a client asking for TLS on it cannot connect at all: "require" fails the
# handshake, and "verify-ca" and "verify-full" additionally try to validate the local listener
# against the instance certificate. The mode that matches is "disable", and it is safe here for
# exactly one reason - the connection never leaves the pod. Both halves are therefore checked
# together: the mode AND the locality of the host it applies to. This check previously demanded
# "require", which is the mode the proxy cannot answer.
MANIFEST_SSLMODE="$(printf '%s\n' "$MANIFESTS" |
    grep -i -E "db_sslmode" -A2 |
    grep -o -i -E "disable|require|verify-ca|verify-full|allow|prefer" |
    head -n 1 || true)"
if [ -z "$MANIFEST_SSLMODE" ]; then
    fail "The manifests set no db_sslmode for the backend container." \
        "The application's default is 'require', which the Cloud SQL Auth Proxy's plain TCP loopback" \
        "listener cannot answer, so the backend would fail every connection at start-up." \
        "Set db_sslmode=disable in the deployment's environment. That is not a downgrade in this" \
        "topology: the proxy dials the instance over its own mutually-authenticated TLS session, which" \
        "is what satisfies the instance's ENCRYPTED_ONLY mode, and the plaintext leg is a loopback" \
        "socket inside the pod. See .env.example and infrastructure/terraform/main.tf."
fi
if [ "$(printf '%s' "$MANIFEST_SSLMODE" | tr '[:upper:]' '[:lower:]')" != "disable" ]; then
    fail "The manifests set db_sslmode to '$MANIFEST_SSLMODE', which the Cloud SQL Auth Proxy cannot satisfy." \
        "The proxy's local listener is plain TCP inside the pod and carries no certificate, so any mode" \
        "that negotiates or verifies TLS against it fails and the backend cannot connect." \
        "Set db_sslmode=disable. Encryption is not lost: the proxy dials the instance over its own" \
        "mutually-authenticated TLS session, which is what satisfies the instance's ENCRYPTED_ONLY mode." \
        "The application refuses 'disable' for any DATABASE_URL that is not loopback, so it cannot be" \
        "used to reach a remote endpoint in cleartext."
fi

# SECURITY: the resolved DATABASE_URL is confirmed to name a LOOPBACK host and the expected
# login. Without this, db_sslmode=disable is verified against a URL nobody looked at: a secret
# pointing at the instance's public address would have the pods open an unencrypted connection
# across the network, and the application's own loopback gate would refuse to start - a failure
# discovered after publication rather than here.
#
# Only the username and the host are extracted, and only they are ever printed. The password is
# in the same string and is never assigned to a variable of its own, never echoed, and never
# passed to another command.
#
# The username is compared with DB_USER, which the operator sets here because there is no
# db_user variable to copy it from. The authoritative comparison is against the login the
# secret actually carries and the users the instance actually has.
if ! read_gcloud "the DATABASE_URL secret" \
    gcloud secrets versions access latest --secret=DATABASE_URL --project="$PROJECT_ID"; then
    fail "Could not read the DATABASE_URL secret from project '$PROJECT_ID'." \
        "It is provisioned by an operator, not by Terraform, and the backend reads it on every" \
        "Settings construction. Create it before deploying; see .env.example section D."
fi
# The proxy offers two local endpoints, a TCP listener on loopback and a Unix socket under
# /cloudsql, and this gate recognises both so that a socket URL is reported as local rather than as
# a network endpoint. Only the loopback form is DEPLOYABLE: backend/app/core/config.py's
# validate_database_url requires the URL to name a host, so a socket URL is refused at start-up and
# the second DATABASE_URL gate below refuses it here. Anything else is a network endpoint.
case "$READ_GCLOUD_VALUE" in
    */cloudsql/*) DATABASE_URL_UNIX_SOCKET="1" ;;
    *)            DATABASE_URL_UNIX_SOCKET="" ;;
esac
DATABASE_URL_AUTHORITY="$(printf '%s' "$READ_GCLOUD_VALUE" | sed -E 's#^[^:]+://##; s#[/?].*$##')"
DATABASE_URL_HOSTPORT="${DATABASE_URL_AUTHORITY##*@}"
# A bracketed IPv6 literal keeps its brackets; stripping at the first colon would leave "[".
case "$DATABASE_URL_HOSTPORT" in
    \[*\]*) DATABASE_URL_HOST="${DATABASE_URL_HOSTPORT%%\]*}]" ;;
    *)      DATABASE_URL_HOST="${DATABASE_URL_HOSTPORT%%:*}" ;;
esac
case "$DATABASE_URL_AUTHORITY" in
    *@*) DATABASE_URL_USER="$(printf '%s' "${DATABASE_URL_AUTHORITY%%@*}" | cut -d: -f1)" ;;
    *)   DATABASE_URL_USER="" ;;
esac
# The password was in READ_GCLOUD_VALUE and in nothing else. Cleared here so it is not carried
# in the environment of any command this script runs afterwards.
unset READ_GCLOUD_VALUE DATABASE_URL_AUTHORITY DATABASE_URL_HOSTPORT

case "$DATABASE_URL_HOST" in
    127.0.0.1|localhost|"[::1]")
        DATABASE_URL_ENDPOINT="loopback '$DATABASE_URL_HOST'"
        ;;
    "")
        if [ -z "$DATABASE_URL_UNIX_SOCKET" ]; then
            fail "The DATABASE_URL secret names no host and no Cloud SQL Unix socket." \
                "A URL with neither cannot be shown to reach a local endpoint, and db_sslmode=disable is" \
                "admissible only for one. Point it at 127.0.0.1 with the proxy's TCP port, as" \
                "postgresql+psycopg2://${DB_USER}:PASSWORD@127.0.0.1:5432/${DB_NAME}. A socket URL under" \
                "/cloudsql/$SQL_CONNECTION_NAME is local but is not deployable: the application requires" \
                "the URL to name a host, so it is refused at start-up and by the gate below."
        fi
        DATABASE_URL_ENDPOINT="the Cloud SQL Unix socket"
        ;;
    *)
        fail "The DATABASE_URL secret names host '$DATABASE_URL_HOST', which is not a local endpoint." \
            "The supported topology is the Cloud SQL Auth Proxy running as a sidecar, reached on its" \
            "loopback listener; the proxy is what encrypts the leg to the instance." \
            "A network host means the pods would connect across the network with db_sslmode=disable, in" \
            "cleartext. The application refuses that combination at start-up, so this would abort the" \
            "rollout after publication instead of here." \
            "Point the secret at 127.0.0.1 with the proxy's port. Reaching the instance directly is not" \
            "an alternative here: db_sslmode accepts only disable and require, so the verifying modes" \
            "cannot be selected, and require without verification buys no server authentication." \
            "Changing that means changing the topology - see residual 3 in the Security Decision Log."
        ;;
esac
if [ "$DATABASE_URL_USER" != "$DB_USER" ]; then
    fail "The DATABASE_URL secret connects as '${DATABASE_URL_USER:-no user}' but DB_USER is '$DB_USER'." \
        "The login checked against the instance below is DB_USER, so a secret naming a different user" \
        "would pass a check for a login it never uses and then fail every database call."
fi
echo "  DATABASE_URL connects as '$DATABASE_URL_USER' over $DATABASE_URL_ENDPOINT with db_sslmode=disable."

read_gcloud "the database users on '$SQL_INSTANCE'" \
    gcloud sql users list --instance="$SQL_INSTANCE" --project="$PROJECT_ID" \
    --format='value(name)' || true
if ! printf '%s\n' "$READ_GCLOUD_VALUE" | grep -qxF "$DB_USER"; then
    fail "Database user '$DB_USER' does not exist on '$SQL_INSTANCE'." \
        "It is provisioned by an operator rather than by Terraform, because managing the password there would" \
        "persist it in Terraform state. Create it with:" \
        "  gcloud sql users create $DB_USER --instance=$SQL_INSTANCE --project=$PROJECT_ID --prompt-for-password" \
        "The DATABASE_URL secret names this login, so without it every database call fails."
fi
echo "  manifests request '$KUBERNETES_SERVICE_ACCOUNT', annotate '$SIGNER_SERVICE_ACCOUNT', and proxy to '$SQL_CONNECTION_NAME'."
echo "  '$SIGNER_SERVICE_ACCOUNT' holds roles/cloudsql.client."

# SECURITY: the database exists under the name every plane is supposed to agree on.
read_gcloud "the databases on '$SQL_INSTANCE'" \
    gcloud sql databases list --instance="$SQL_INSTANCE" --project="$PROJECT_ID" \
    --format='value(name)' || true
if ! printf '%s\n' "$READ_GCLOUD_VALUE" | grep -qxF "$DB_NAME"; then
    fail "Database '$DB_NAME' does not exist on '$SQL_INSTANCE'." \
        "It is google_sql_database.database in infrastructure/terraform, named by the db_name variable," \
        "which defaults to 'main-database'. If db_name was overridden, set DB_NAME here to the same value." \
        "Apply Terraform before deploying."
fi

# SECURITY: the DATABASE_URL secret is confirmed to describe the connection this infrastructure
# actually provides, before anything is deployed against it. Terraform creates the database and the
# proxy path, and an operator writes this secret by hand - the two were never compared, so a secret
# naming another host, another database or another login deployed cleanly and then failed every
# request, authentication included, with no indication that the URL was the cause.
#
# The value is a credential. It is read into a variable and never echoed, never written to a file
# and never included in a failure message; only the structural parts extracted from it are named,
# and the password is stripped before any comparison. What is checked is exactly what the
# application requires: backend/app/core/config.py accepts only a synchronous
# postgresql+psycopg2 URL naming a host and one database, so the same grammar is enforced here where
# the failure is a preflight abort rather than a crash-looping pod.
if ! read_gcloud "the DATABASE_URL secret" \
    gcloud secrets versions access latest --secret=DATABASE_URL --project="$PROJECT_ID"; then
    fail "Secret DATABASE_URL has no accessible 'latest' version in project '$PROJECT_ID'." \
        "It is provisioned by an operator, not by Terraform, and Settings reads it on every construction." \
        "Create it with: printf '%s' \"\$URL\" | gcloud secrets create DATABASE_URL --data-file=- --project=$PROJECT_ID"
fi
DATABASE_URL_SECRET="$READ_GCLOUD_VALUE"

# Structural parts only. The scheme is everything before "://"; the authority is what follows, up to
# the first "/"; the path is the remainder. The userinfo is split on "@" and only the portion before
# ":" - the login - is retained, so the password is never held in a variable of its own.
URL_SCHEME="${DATABASE_URL_SECRET%%://*}"
URL_REMAINDER="${DATABASE_URL_SECRET#*://}"
URL_AUTHORITY="${URL_REMAINDER%%/*}"
URL_PATH="${URL_REMAINDER#*/}"
URL_DATABASE="${URL_PATH%%\?*}"
URL_HOSTPORT="${URL_AUTHORITY##*@}"
URL_HOST="${URL_HOSTPORT%%:*}"
URL_USERINFO="${URL_AUTHORITY%@*}"
if [ "$URL_USERINFO" = "$URL_AUTHORITY" ]; then
    URL_LOGIN=""
else
    URL_LOGIN="${URL_USERINFO%%:*}"
fi

case "$URL_SCHEME" in
    postgresql | postgresql+psycopg2) ;;
    *)
        fail "The DATABASE_URL secret selects the driver '${URL_SCHEME:-none}'." \
            "backend/app/core/config.py accepts only postgresql or postgresql+psycopg2: the engine is" \
            "built with psycopg2-only connection arguments, so any other dialect - and any async" \
            "driver - is refused at start-up. Rewrite the secret with that driver."
        ;;
esac

# The proxy listens on loopback inside the pod, so the URL must address the proxy rather than the
# instance. A public Cloud SQL address in the secret would bypass the proxy entirely, which the
# instance's ENCRYPTED_ONLY mode then refuses because no client certificate is presented.
case "$URL_HOST" in
    127.0.0.1 | localhost | "[::1]" | "::1") ;;
    "")
        fail "The DATABASE_URL secret names no host." \
            "It must address the Cloud SQL Auth Proxy's listener inside the pod, as" \
            "postgresql+psycopg2://${DB_USER}:PASSWORD@127.0.0.1:5432/${DB_NAME}."
        ;;
    *)
        fail "The DATABASE_URL secret connects to host '$URL_HOST', not to the proxy's loopback listener." \
            "The Cloud SQL Auth Proxy runs as a sidecar in the same pod and listens on 127.0.0.1, and it" \
            "is the only connectivity path this infrastructure provisions: the instance has no private" \
            "network, and reaching its public address directly would need an authorized network and client" \
            "certificates that nothing here creates. Use 127.0.0.1 as the host."
        ;;
esac

if [ "$URL_DATABASE" != "$DB_NAME" ]; then
    fail "The DATABASE_URL secret names database '${URL_DATABASE:-none}', but this deployment provisions '$DB_NAME'." \
        "google_sql_database.database creates '$DB_NAME'. A URL naming anything else connects to a" \
        "database that does not exist, so every request fails once the pods start."
fi

if [ "$URL_LOGIN" != "$DB_USER" ]; then
    fail "The DATABASE_URL secret connects as login '${URL_LOGIN:-none}', but DB_USER is '$DB_USER'." \
        "DB_USER is the login confirmed to exist on '$SQL_INSTANCE' above. The secret must name that same" \
        "login, or the connection is refused by the instance."
fi
unset DATABASE_URL_SECRET URL_REMAINDER URL_AUTHORITY URL_PATH URL_USERINFO URL_HOSTPORT

echo "  manifests request '$KUBERNETES_SERVICE_ACCOUNT', annotate '$SIGNER_SERVICE_ACCOUNT', and proxy to '$SQL_CONNECTION_NAME'."
echo "  '$SIGNER_SERVICE_ACCOUNT' holds roles/cloudsql.client."
echo "  the DATABASE_URL secret is a $URL_SCHEME URL for '$DB_USER'@loopback/'$DB_NAME'."

# --- 5. Whether the browser can use the Firestore rules about to be deployed -
# SECURITY: the rules deploy regardless, because they are fail-closed and deploying them can
# only narrow access. What is reported here is whether the browser can exercise them at all.
# A reference that Firestore rejects on path arity never reaches a rule, so a broken client
# would otherwise be mistaken for a working control.
echo "Preflight: checking the browser Firestore reference..."
COLLAB_CLIENT="frontend/src/services/collaboration.ts"
COLLAB_REFERENCE_OK="1"
if [ ! -f "$COLLAB_CLIENT" ]; then
    COLLAB_REFERENCE_OK=""
    warn "'$COLLAB_CLIENT' not found; it is the only browser reader of the collaboration store." \
        "Real-time collaboration will not work in the browser. This does not block the deployment" \
        "and exposes nothing: the Firestore rules deployed below deny any path they do not match."
else
    if grep -qE "collection\([[:space:]]*db[[:space:]]*,[[:space:]]*'workbooks'[[:space:]]*," "$COLLAB_CLIENT"; then
        COLLAB_REFERENCE_OK=""
        warn "'$COLLAB_CLIENT' builds a collection reference with an even number of path segments." \
            "workbooks/{workbookId} names a document, so Firestore rejects the reference before any rule" \
            "is evaluated: real-time collaboration is broken in the browser and the rules protecting it" \
            "are never exercised. The fix is doc(db, 'workbooks', workbookId), matching" \
            "/workbooks/{workbookId} in firestore.rules and the path" \
            "backend/app/services/real_time_sync.py writes." \
            "Reported rather than enforced: this module is reference-only and is not modified by the" \
            "change set that added this check. It is a functional defect, not an exposure."
    fi
    if ! grep -qE "doc\([[:space:]]*db[[:space:]]*,[[:space:]]*'workbooks'" "$COLLAB_CLIENT"; then
        COLLAB_REFERENCE_OK=""
        warn "'$COLLAB_CLIENT' does not build a document reference under 'workbooks'." \
            "The deployed rules match /workbooks/{workbookId}; a client reading any other path is" \
            "denied by default, so collaboration reads fail closed. Reported rather than enforced," \
            "for the same reason as above."
    fi
fi
# The verdict is read from the flag the three checks above set, rather than by repeating one of
# their greps. Repeating a single grep restated only the "no document reference" case, so a client
# that had BOTH a document reference and a rejected even-segment collection reference was reported
# as resolving - and a missing file was reported as resolving too. It also emitted a second
# warning for a condition already warned about.
if [ -z "$COLLAB_REFERENCE_OK" ]; then
    echo "  WARNING: '$COLLAB_CLIENT' does not resolve a usable document reference under 'workbooks'." >&2
    echo "  workbooks/{workbookId} names a document; a collection(db, 'workbooks', workbookId)" >&2
    echo "  reference has an even segment count and Firestore rejects it before any rule is" >&2
    echo "  evaluated. The browser collaboration path is therefore inert on this revision and" >&2
    echo "  the rules below, while correct, are not exercised from it. This is a known" >&2
    echo "  follow-up in a module the security change set does not own; it does not block the" >&2
    echo "  deployment, because the rules are fail-closed and every server write reaches" >&2
    echo "  Firestore through the server client library, which bypasses rules entirely." >&2
else
    echo "  browser reference resolves under /workbooks/{workbookId}."
fi

# --- 6. The Cloud Function runs on a runtime Google still deploys -----------
# SECURITY: the runtime is verified against the list Google publishes at the time of the run,
# not against a list of names kept in this repository. A decommissioned runtime receives no
# platform security update, cannot be redeployed, and leaves any deployed function frozen on
# unpatched software.
# 1st-gen support is checked specifically because google_cloudfunctions_function deploys through
# the 1st-gen API, which does not always offer a runtime that is current elsewhere - so the check
# reads the generation from the live list rather than from an assumption about which runtimes the
# 1st gen carries. nodejs22 is the worked example of why an assumption is the wrong instrument:
# it was refused by the 1st-gen API with INVALID_RUNTIME when this check was written, and it is
# now offered for 1st gen at General Availability. A statement here about which generation
# supports it would have been wrong on that value twice, in opposite directions, while the live
# lookup below was correct on both occasions without being edited.
echo "Preflight: verifying the Cloud Function runtime and source..."

if ! read_gcloud "the runtime of ${FUNCTION_NAME}" \
    gcloud functions describe "$FUNCTION_NAME" --region="$REGION" \
    --project="$PROJECT_ID" --format='value(runtime)'; then
    fail "Cloud Function '${FUNCTION_NAME}' does not exist in project '$PROJECT_ID' region '$REGION'." \
        "It is google_cloudfunctions_function.excel_app_function in infrastructure/terraform, which is its" \
        "sole deployer. Apply Terraform first; this script deploys no function."
fi
DEPLOYED_RUNTIME="$READ_GCLOUD_VALUE"
if [ -z "$DEPLOYED_RUNTIME" ]; then
    fail "Cloud Function '${FUNCTION_NAME}' reports no runtime." \
        "The runtime cannot be verified against the list Google currently offers, so this stops rather" \
        "than continuing with it unchecked."
fi

read_gcloud "the Cloud Functions runtimes Google currently offers" \
    gcloud functions runtimes list --region="$REGION" --project="$PROJECT_ID" \
    --format='value(name,environments)' || true
OFFERED_RUNTIMES="$READ_GCLOUD_VALUE"
if [ -z "$OFFERED_RUNTIMES" ]; then
    fail "Could not list the Cloud Functions runtimes Google currently offers." \
        "Enable cloudfunctions.googleapis.com for '$PROJECT_ID' and confirm the active credentials can" \
        "read it. The runtime is not assumed to be valid just because the list could not be fetched."
fi

RUNTIME_ROW="$(printf '%s\n' "$OFFERED_RUNTIMES" |
    grep -E "^${DEPLOYED_RUNTIME}([[:space:]]|\$)" | head -n 1 || true)"
if [ -z "$RUNTIME_ROW" ]; then
    fail "'${FUNCTION_NAME}' runs on '$DEPLOYED_RUNTIME', which Google no longer offers." \
        "A decommissioned runtime cannot be redeployed and receives no platform security updates, so the" \
        "function is frozen on unpatched software. Set the function_runtime Terraform variable to a runtime" \
        "listed by 'gcloud functions runtimes list --region=$REGION' whose environments include 1st gen," \
        "then apply Terraform before deploying again."
fi
case "$RUNTIME_ROW" in
    *GEN_1*|*"1st gen"*) ;;
    *)
        fail "'$DEPLOYED_RUNTIME' is offered by Google, but not for 1st-generation functions." \
            "google_cloudfunctions_function deploys through the 1st-gen API, which refuses it with" \
            "INVALID_RUNTIME, so the next apply of this configuration will fail. Choose a runtime whose" \
            "environments include 1st gen, or move the function to the 2nd-gen resource type deliberately."
        ;;
esac
echo "  '${FUNCTION_NAME}' runs on '$DEPLOYED_RUNTIME', which Google still offers for 1st-gen functions."

# --- 7. The tools the post-deployment verification needs are present --------
# SECURITY: checked in the preflight, before any mutation, so a missing tool cannot cause the
# verification to be skipped after the release is already live. The verification asserts the
# controls this script claims - the response headers, the plaintext redirect and the API's
# refusal of an unauthenticated request - so it is not optional, and a run that cannot perform
# it must stop while stopping is still free.
# SECURITY: the deployed function's source, entry point, trigger and transport are confirmed
# against the live resource. Terraform can refuse a source bucket it knows the name of - it
# rejects the static-assets and user-uploads buckets as preconditions - but it cannot see whether
# the bucket an operator supplied is private, whether the archive object is the one it names, or
# what the platform actually deployed. Each check below is fail-closed: an unreadable value
# aborts rather than being taken as absent.
read_gcloud "the source archive of ${FUNCTION_NAME}" \
    gcloud functions describe "$FUNCTION_NAME" --region="$REGION" \
    --project="$PROJECT_ID" --format='value(sourceArchiveUrl)' || true
FUNCTION_SOURCE_URL="$READ_GCLOUD_VALUE"
if [ -z "$FUNCTION_SOURCE_URL" ]; then
    fail "'${FUNCTION_NAME}' reports no source archive URL." \
        "Its deployable code cannot be located, so neither the privacy of the bucket holding it nor the" \
        "identity of the archive can be checked. This stops rather than accepting an unverified source."
fi

# gs://BUCKET/OBJECT
FUNCTION_SOURCE_PATH="${FUNCTION_SOURCE_URL#gs://}"
FUNCTION_SOURCE_BUCKET="${FUNCTION_SOURCE_PATH%%/*}"
FUNCTION_SOURCE_OBJECT="${FUNCTION_SOURCE_PATH#*/}"
if [ -z "$FUNCTION_SOURCE_BUCKET" ] || [ "$FUNCTION_SOURCE_OBJECT" = "$FUNCTION_SOURCE_PATH" ]; then
    fail "Could not read a bucket and object from the function's source archive URL '$FUNCTION_SOURCE_URL'." \
        "Expected the form gs://BUCKET/OBJECT."
fi

# SECURITY: the source bucket must not be readable by everyone. The function's deployable code is
# what the platform executes, so a world-readable archive publishes the application's server-side
# logic and any identifier embedded in it, and its object path is guessable.
if [ "$FUNCTION_SOURCE_BUCKET" = "$STATIC_ASSETS_BUCKET" ]; then
    fail "'${FUNCTION_NAME}' takes its source from gs://${FUNCTION_SOURCE_BUCKET}, which the load balancer serves to the internet." \
        "That bucket grants allUsers read so the SPA can be served, so the function's source archive is" \
        "world-readable. Move the archive to a private bucket, set function_source_bucket to it, and apply" \
        "Terraform. Terraform refuses this bucket as a precondition, so this state means the function was" \
        "deployed by something other than this configuration."
fi

# The read is required to succeed. An unreadable policy leaves the member list empty, which the
# case below would treat as "no public binding" - the one answer that lets a world-readable
# source archive through - so absence of evidence is not accepted as evidence here.
if ! read_gcloud "the IAM policy of gs://${FUNCTION_SOURCE_BUCKET}" \
    gcloud storage buckets get-iam-policy "gs://${FUNCTION_SOURCE_BUCKET}" \
    --project="$PROJECT_ID" --format='value(bindings.members)'; then
    fail "The bucket gs://${FUNCTION_SOURCE_BUCKET} holding the function's source archive does not exist or its policy could not be read." \
        "Whether the function's deployable code is world-readable therefore cannot be determined, and an" \
        "unreadable policy is not treated as a private one."
fi
FUNCTION_SOURCE_BUCKET_MEMBERS="$READ_GCLOUD_VALUE"
case "$FUNCTION_SOURCE_BUCKET_MEMBERS" in
    *allUsers*|*allAuthenticatedUsers*)
        fail "gs://${FUNCTION_SOURCE_BUCKET} grants access to allUsers or allAuthenticatedUsers." \
            "It holds the function's deployable source archive, so that binding publishes the code the" \
            "platform executes. Remove the public binding, or move the archive to a private bucket and" \
            "set function_source_bucket to it." \
            "Observed members: $FUNCTION_SOURCE_BUCKET_MEMBERS"
        ;;
esac

# SECURITY: the archive object is confirmed to exist where the function says it does. A function
# whose source object has been deleted or renamed cannot be redeployed, so it is frozen on
# whatever was last built - including through a platform security update it can never take.
if ! read_gcloud "the function's source archive object" \
    gcloud storage objects describe "gs://${FUNCTION_SOURCE_BUCKET}/${FUNCTION_SOURCE_OBJECT}" \
    --project="$PROJECT_ID" --format='value(name)'; then
    fail "The function's source archive gs://${FUNCTION_SOURCE_BUCKET}/${FUNCTION_SOURCE_OBJECT} does not exist." \
        "The deployed function references it, so the function cannot be redeployed or updated." \
        "Upload the archive and name it through the function_source_bucket and function_source_object" \
        "Terraform variables."
fi

# SECURITY: the entry point and the trigger are confirmed to be the ones this configuration
# declares. A function deployed with a different entry point runs code this repository does not
# describe, and one carrying an event trigger instead of an HTTP trigger is invoked by a source
# the invoker IAM binding does not govern.
read_gcloud "the entry point of ${FUNCTION_NAME}" \
    gcloud functions describe "$FUNCTION_NAME" --region="$REGION" \
    --project="$PROJECT_ID" --format='value(entryPoint)' || true
FUNCTION_ENTRY_POINT="$READ_GCLOUD_VALUE"
if [ "$FUNCTION_ENTRY_POINT" != "helloWorld" ]; then
    fail "'${FUNCTION_NAME}' has entry point '${FUNCTION_ENTRY_POINT:-none}', not 'helloWorld'." \
        "infrastructure/terraform declares helloWorld, so a different value means the deployed function" \
        "runs code this configuration does not describe."
fi

read_gcloud "the HTTPS trigger URL of ${FUNCTION_NAME}" \
    gcloud functions describe "$FUNCTION_NAME" --region="$REGION" \
    --project="$PROJECT_ID" --format='value(httpsTrigger.url)' || true
FUNCTION_TRIGGER_URL="$READ_GCLOUD_VALUE"
if [ -z "$FUNCTION_TRIGGER_URL" ]; then
    fail "'${FUNCTION_NAME}' exposes no HTTPS trigger." \
        "infrastructure/terraform declares trigger_http = true. A function carrying an event trigger" \
        "instead is invoked by an event source rather than by a caller, so the invoker binding that" \
        "restricts who may call it does not govern how it is reached."
fi

# SECURITY: the trigger is confirmed to refuse plaintext. The 1st-gen default is SECURE_OPTIONAL,
# which serves the function on both schemes, so a caller's identity token could travel in
# cleartext to any network position on the path.
read_gcloud "the HTTPS security level of ${FUNCTION_NAME}" \
    gcloud functions describe "$FUNCTION_NAME" --region="$REGION" \
    --project="$PROJECT_ID" --format='value(httpsTrigger.securityLevel)' || true
FUNCTION_SECURITY_LEVEL="$READ_GCLOUD_VALUE"
if [ "$FUNCTION_SECURITY_LEVEL" != "SECURE_ALWAYS" ]; then
    fail "'${FUNCTION_NAME}' serves its trigger at security level '${FUNCTION_SECURITY_LEVEL:-unset}', not SECURE_ALWAYS." \
        "SECURE_OPTIONAL - the platform default - answers on http as well as https, so an identity token" \
        "presented over http reaches the function after crossing the network in the open." \
        "It is https_trigger_security_level on google_cloudfunctions_function.excel_app_function in" \
        "infrastructure/terraform. Apply Terraform before deploying."
fi
echo "  '${FUNCTION_NAME}' runs helloWorld from a private gs://${FUNCTION_SOURCE_BUCKET}, https-only."

# SECURITY: the invoker policy is ASSERTED here, in the read-only preflight, before anything is
# published. It used to be revoked and then re-read near the end of the script - after the
# frontend had been built and published, the image built and pushed, and the manifests applied -
# so a function that stayed publicly invocable was discovered with the release already live.
#
# This asserts rather than revokes, and the change is deliberate. Terraform now declares the
# invoker role with an AUTHORITATIVE binding, so applying it removes any allUsers binding left
# behind by an earlier deployment that permitted unauthenticated invocation; there is nothing
# left for this script to clean up. And a script that revokes a binding and then checks its own
# revocation can never report the finding - it reports only whether its own command worked.
# Asserting keeps the preflight read-only and makes this a real gate on the state Terraform
# produced.
#
# The read must succeed before its emptiness means anything: a missing permission, a disabled
# API, a wrong region or an undeployed function all produce an empty member list, which is
# indistinguishable from "allUsers is not an invoker" - the one answer that lets a publicly
# invocable function through. read_gcloud aborts on a failed read.
if ! read_gcloud "the invoker policy of ${FUNCTION_NAME}" \
    gcloud functions get-iam-policy "$FUNCTION_NAME" --region="$REGION" \
    --project="$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter='bindings.role:roles/cloudfunctions.invoker' \
    --format='value(bindings.members)'; then
    fail "The invoker policy of '${FUNCTION_NAME}' could not be read." \
        "This script will not report an unreadable invoker policy as proof that no allUsers binding" \
        "exists, so it stops here."
fi
INVOKER_MEMBERS="$READ_GCLOUD_VALUE"

if printf '%s\n' "$INVOKER_MEMBERS" | grep -qx "allUsers" ||
    printf '%s\n' "$INVOKER_MEMBERS" | grep -qx "allAuthenticatedUsers"; then
    fail "A public principal holds roles/cloudfunctions.invoker on '${FUNCTION_NAME}'." \
        "The function is invocable by anyone who discovers its URL, so nothing is published." \
        "google_cloudfunctions_function_iam_binding.invoker in infrastructure/terraform is authoritative:" \
        "applying it declares the complete member list and removes this binding. Apply Terraform." \
        "To remove it directly instead:" \
        "  gcloud functions remove-iam-policy-binding $FUNCTION_NAME --region=$REGION --member=allUsers --role=roles/cloudfunctions.invoker" \
        "Observed invoker members: ${INVOKER_MEMBERS:-none}"
fi

# SECURITY: the intended caller is confirmed to hold the role. An authoritative binding that
# named no reachable member would leave the function callable by nobody, which is a safe failure
# but still a failure, and one worth catching before the release rather than in production.
if ! printf '%s\n' "$INVOKER_MEMBERS" | grep -qxF "serviceAccount:${SIGNER_SERVICE_ACCOUNT}"; then
    fail "'$SIGNER_SERVICE_ACCOUNT' does not hold roles/cloudfunctions.invoker on '${FUNCTION_NAME}'." \
        "It is the sole member of google_cloudfunctions_function_iam_binding.invoker in" \
        "infrastructure/terraform, and it is the identity the API mints an identity token as." \
        "Without it no caller in this system can invoke the function. Apply Terraform." \
        "Observed invoker members: ${INVOKER_MEMBERS:-none}"
fi
echo "  invoker of '${FUNCTION_NAME}' is '$SIGNER_SERVICE_ACCOUNT' and no public principal."

echo ""
# --- 7. The backend image can be built at all --------------------------------
# The build used to be `cd backend && docker build .`, and backend/ holds only app/, tests/ and
# requirements.txt - there is no Dockerfile there, so the build failed after the frontend had
# already been published. The Dockerfile lives under infrastructure/docker/ and expects backend/
# as its context, because it copies requirements.txt from the context root.
#
# This confirms the build can start. It cannot confirm the resulting image starts: see KNOWN
# BLOCKER 2 - the Dockerfile's CMD names a module that does not exist at the context root, and no
# directory under backend/ carries an __init__.py, so the container exits and the pods
# CrashLoopBackOff. That is a defect in files outside this change set and is not worked around
# here.
echo "Preflight: verifying the backend image build inputs..."
BACKEND_DOCKERFILE="${BACKEND_DOCKERFILE:-infrastructure/docker/Dockerfile.backend}"
BACKEND_BUILD_CONTEXT="${BACKEND_BUILD_CONTEXT:-backend}"
if [ ! -f "$BACKEND_DOCKERFILE" ]; then
    fail "Backend Dockerfile '$BACKEND_DOCKERFILE' not found." \
        "Set BACKEND_DOCKERFILE to its path. There is no Dockerfile inside '$BACKEND_BUILD_CONTEXT'."
fi
if [ ! -f "${BACKEND_BUILD_CONTEXT}/requirements.txt" ]; then
    fail "'${BACKEND_BUILD_CONTEXT}/requirements.txt' not found." \
        "'$BACKEND_DOCKERFILE' copies requirements.txt from the context root, so the build would fail" \
        "on its first COPY. Set BACKEND_BUILD_CONTEXT to the directory that holds the manifest."
fi
echo "  building '$BACKEND_DOCKERFILE' with context '$BACKEND_BUILD_CONTEXT'."

echo ""
echo "Preflight: verifying the tools the post-deployment checks need..."
if ! command -v curl >/dev/null 2>&1; then
    fail "'curl' is not on PATH." \
        "The post-deployment verification uses it to confirm the security response headers, the" \
        "HTTP-to-HTTPS redirect and that the API refuses an unauthenticated request. Those checks" \
        "are the only evidence that this deployment's controls are actually in effect, so this stops" \
        "here rather than publishing a release it cannot verify."
fi
echo "  curl is available."

echo ""
echo "Preflight passed. Beginning deployment."
echo ""

# ===========================================================================
# MUTATIONS - nothing above this line changes any state
#
# ORDER: the Firestore authorization rules are deployed FIRST, before any client or server
# code is published. The store they govern is reached DIRECTLY by the browser, without
# traversing the API, so they are the only control on that path and no server-side change can
# stand in for them while they are stale.
# Publishing the application first opened a window - however brief - in which a freshly
# published client was live against whatever rules the project happened to be carrying: on a
# first deployment, none at all, which is unrestricted access to every workbook document.
# Deploying rules first cannot break the running application either, because they are strictly
# more restrictive than their absence and because the backend reaches Firestore through the
# server client library, which bypasses rules entirely.
# ===========================================================================

# Update Google Cloud Firestore security rules
echo "Updating Firestore security rules..."
# SECURITY: the document-level authorization rules are deployed to the same project the API
# verifies tokens for - the Firebase CLI selects its project from its own active project or
# --project, not from the gcloud configuration set above, and the repository declares no
# .firebaserc. Authenticate non-interactively with FIREBASE_TOKEN or
# GOOGLE_APPLICATION_CREDENTIALS before running this script.
firebase deploy --only firestore:rules --project "$PROJECT_ID" --non-interactive

# Build frontend assets
# SECURITY: the compiled document must carry no inline script. Create React App inlines its
# webpack runtime into index.html by default, and the Content-Security-Policy the load balancer
# serves says script-src 'self' with no nonce and no hash, so a browser enforcing that policy
# refuses the inlined runtime and the application does not start. INLINE_RUNTIME_CHUNK=false
# emits the runtime as a separate file, which 'self' covers. infrastructure/docker/
# Dockerfile.frontend sets the same variable for the container build.
#
# SECURITY: no source map is emitted. Create React App defaults GENERATE_SOURCEMAP to true, and
# the publish step below is a whole-directory rsync into a bucket that grants read to allUsers -
# so every *.js.map went to the public internet, and a source map republishes the original
# TypeScript, its comments and every identifier the compiler renamed. Nothing in the repository
# set this variable, so the default applied. infrastructure/docker/Dockerfile.frontend sets it
# too, and infrastructure/docker/nginx.conf refuses *.map at the edge, so no single omission
# republishes the source.
echo "Building frontend assets..."
cd frontend
INLINE_RUNTIME_CHUNK=false GENERATE_SOURCEMAP=false npm run build
cd ..

echo "Publishing frontend to gs://${STATIC_ASSETS_BUCKET}..."
gsutil -m rsync -r frontend/build "gs://${STATIC_ASSETS_BUCKET}"

# SECURITY and correctness: publish the cache lifetime as object metadata.
# Cloud Storage serves Cache-Control from the object, not from the load balancer, so without
# this every object was served with no Cache-Control at all - the entry document included. The
# document is the object that names which content-hashed bundles to load AND carries the meta
# Content-Security-Policy, so a cached copy pins a browser to a superseded bundle set and a
# superseded policy. These are the same two lifetimes infrastructure/docker/nginx.conf renders
# from its $excel_app_cache_control map, so both delivery paths agree.
# Ordered AFTER the rsync: rsync uploads new objects without this metadata, so setting it first
# would leave every freshly uploaded object bare.
echo "Publishing cache metadata..."
# Content-hashed assets: a change produces a new file name, so the old name never needs
# revalidating.
gsutil -m setmeta -h "Cache-Control:public, max-age=31536000, immutable" \
    "gs://${STATIC_ASSETS_BUCKET}/static/**"
# Everything at the root revalidates on every request, the entry document above all.
gsutil -m setmeta -h "Cache-Control:no-cache" \
    "gs://${STATIC_ASSETS_BUCKET}/index.html" \
    "gs://${STATIC_ASSETS_BUCKET}/asset-manifest.json" \
    "gs://${STATIC_ASSETS_BUCKET}/manifest.json"

echo "Building and pushing backend Docker image..."
docker build -f "$BACKEND_DOCKERFILE" -t "gcr.io/${PROJECT_ID}/excel-app-backend:latest" "$BACKEND_BUILD_CONTEXT"
docker push "gcr.io/${PROJECT_ID}/excel-app-backend:latest"


# SECURITY: the manifests are applied to the cluster Terraform provisioned, named explicitly
# rather than taken from the ambient kubeconfig.
echo "Deploying backend to Google Kubernetes Engine..."
gcloud container clusters get-credentials "$GKE_CLUSTER" --region="$REGION" --project="$PROJECT_ID"
kubectl apply -f "${K8S_MANIFEST_DIR}/deployment.yaml"
kubectl apply -f "${K8S_MANIFEST_DIR}/service.yaml"

# The Cloud Function is deployed by infrastructure/terraform, which owns its name, region,
# runtime, source archive, HTTPS security level and invoker policy. This script deploys no
# function: two deployers of one function from two sources cannot agree on which artifact is
# running.
# Its invoker policy is asserted in the preflight, before anything here has run, so there is
# deliberately no invoker work in this section. A revocation performed at this point would come
# after the frontend had been published, the image pushed and the manifests applied - which is
# to say, after the release the check was supposed to gate.

echo "Applying database migrations..."
echo "  running: $DB_MIGRATION_COMMAND"
# The value is a shell command line and is evaluated as one. See the TRUST BOUNDARY note at its
# assignment: it must originate from an operator, never from an untrusted input.
eval "$DB_MIGRATION_COMMAND"

# ===========================================================================
# POST-DEPLOYMENT VERIFICATION
#
# Every control this script claims is confirmed against the running deployment. Until this
# existed the script printed "Deployment completed successfully!" on the strength of the
# preflight alone - it had verified that the edge resources were CONFIGURED correctly and never
# that the deployed system BEHAVED correctly, so a release that served no security headers,
# answered plaintext without redirecting, or returned workbook data to an unauthenticated
# caller was reported as a success and the operator was asked to check it by hand.
#
# Each check records its finding and continues, so one run reports every problem rather than
# only the first. Any finding at all fails the deployment and prints the rollback procedure.
# ===========================================================================
echo ""
echo "Verifying the deployed controls..."

echo "Running post-deployment tests..."
echo "  running: $POST_DEPLOY_TEST_COMMAND"
# The value is a shell command line and is evaluated as one. See the TRUST BOUNDARY note at its
# assignment: it must originate from an operator, never from an untrusted input.
eval "$POST_DEPLOY_TEST_COMMAND"
POST_DEPLOY_FINDINGS=""
record_finding() {
    POST_DEPLOY_FINDINGS="${POST_DEPLOY_FINDINGS}${POST_DEPLOY_FINDINGS:+
}  - $1"
}

# Response headers only; the body is discarded. Redirects are deliberately NOT followed, because
# whether the first response redirects is itself one of the assertions.
RESPONSE_HEADERS=""
read_response_headers() {
    RESPONSE_HEADERS=""
    local output
    if output="$(curl -sS -o /dev/null -D - --max-time 30 "$1" 2>/dev/null)"; then
        RESPONSE_HEADERS="$output"
        return 0
    fi
    return 1
}

# --- The security response headers reach the browser ------------------------
# The preflight confirmed the backend bucket is CONFIGURED with them. This confirms they are
# present on what a client actually receives, which is the only form of the claim that matters.
#
# SECURITY: each header's VALUE is compared, not just its name. A response carrying all six
# names with values that protect nothing - X-Frame-Options: ALLOWALL, max-age=1,
# Referrer-Policy: unsafe-url - satisfies a name-only check, and this step's own summary line
# then reports the headers as confirmed. Comparison is case-insensitive on both sides: header
# names are case-insensitive on the wire, and every canonical value here is composed of
# case-insensitive tokens, so folding case costs nothing and avoids a false finding.
response_header_value() {
    printf '%s\n' "$RESPONSE_HEADERS" |
        tr -d '\r' |
        tr '[:upper:]' '[:lower:]' |
        { grep -E "^$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]'):" || true; } |
        tail -n 1 |
        cut -d: -f2- |
        sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

lower_case() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

if read_response_headers "https://${DOMAIN_NAME}/"; then
    SERVED_POLICY_HEADER=""
    for policy_header_name in Content-Security-Policy Content-Security-Policy-Report-Only; do
        if [ -n "$(response_header_value "$policy_header_name")" ]; then
            SERVED_POLICY_HEADER="$policy_header_name"
        fi
    done
    if [ -z "$SERVED_POLICY_HEADER" ]; then
        record_finding "https://${DOMAIN_NAME}/ carries no Content-Security-Policy response header."
    else
        # The policy's value varies with api_origin and csp_report_only, so the directives that
        # carry protection are checked individually rather than against one literal.
        SERVED_POLICY_VALUE="$(response_header_value "$SERVED_POLICY_HEADER")"
        for required_directive in \
            "default-src 'self'" \
            "base-uri 'self'" \
            "object-src 'none'" \
            "frame-ancestors 'none'" \
            "form-action 'self'" \
            "script-src 'self'"; do
            if ! printf '%s\n' "$SERVED_POLICY_VALUE" |
                tr ';' '\n' |
                sed 's/^[[:space:]]*//; s/[[:space:]]*$//' |
                grep -Fxq "$(lower_case "$required_directive")"; then
                record_finding "https://${DOMAIN_NAME}/ serves a ${SERVED_POLICY_HEADER} that does not carry the directive \"${required_directive}\". Served: ${SERVED_POLICY_VALUE}"
            fi
        done
    fi

    for expected_header in $FIXED_SECURITY_HEADERS; do
        served_value="$(response_header_value "$expected_header")"
        if [ -z "$served_value" ]; then
            record_finding "https://${DOMAIN_NAME}/ carries no ${expected_header} response header."
        elif [ "$served_value" != "$(lower_case "$(canonical_header_value "$expected_header")")" ]; then
            record_finding "https://${DOMAIN_NAME}/ serves ${expected_header}: ${served_value}, but the required value is ${expected_header}: $(canonical_header_value "$expected_header"). A header present under the right name with a value that grants what it is meant to refuse protects nothing."
        fi
    done
    unset served_value
else
    record_finding "https://${DOMAIN_NAME}/ could not be reached over TLS at all."
fi

# --- Plaintext is redirected, not served ------------------------------------
if read_response_headers "http://${DOMAIN_NAME}/"; then
    HTTP_STATUS_LINE="$(printf '%s\n' "$RESPONSE_HEADERS" | head -n 1 | tr -d '\r')"
    case "$HTTP_STATUS_LINE" in
        *" 301"*|*" 308"*) ;;
        *)
            record_finding "http://${DOMAIN_NAME}/ answered '${HTTP_STATUS_LINE:-nothing}' instead of a permanent redirect. Plaintext traffic is being served, not upgraded."
            ;;
    esac
    # A response with no Location header is recorded as a finding immediately below, not a reason
    # to stop the verification. grep exits 1 on no match, so the tolerance is confined to grep.
    REDIRECT_TARGET="$(printf '%s\n' "$RESPONSE_HEADERS" |
        { grep -i '^location:' || true; } | head -n 1 | cut -d: -f2- | tr -d '\r' | tr -d ' ')"
    case "$REDIRECT_TARGET" in
        https://*) ;;
        *)
            record_finding "http://${DOMAIN_NAME}/ redirects to '${REDIRECT_TARGET:-nothing}', which is not an https URL."
            ;;
    esac
else
    record_finding "http://${DOMAIN_NAME}/ could not be reached, so the redirect could not be confirmed."
fi

# --- The API refuses an unauthenticated request -----------------------------
# SECURITY: this is the assertion the whole authentication change exists to make true, and it is
# the one an operator is least able to make by eye.
# 401 is required specifically. A 200 means the route is open; a 403 would mean the request was
# authenticated and then refused, which is not what an anonymous caller should produce; a 404
# would mean the route is not the one this deployment serves.
# The rollout is given a bounded chance to become ready first: an unavailable backend answers
# 502 or 503 through the load balancer, and reporting that as "authentication is not enforced"
# would be wrong.
API_PROBE_URL="${BUILD_API_ORIGIN}/workbooks"
API_PROBE_STATUS=""
API_PROBE_ATTEMPT=1
while [ "$API_PROBE_ATTEMPT" -le 10 ]; do
    API_PROBE_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$API_PROBE_URL" 2>/dev/null || true)"
    case "$API_PROBE_STATUS" in
        000|502|503|504)
            echo "  the API is not ready yet (${API_PROBE_STATUS}); retrying in 15s (${API_PROBE_ATTEMPT}/10)."
            sleep 15
            ;;
        *)
            break
            ;;
    esac
    API_PROBE_ATTEMPT=$((API_PROBE_ATTEMPT + 1))
done
if [ "$API_PROBE_STATUS" != "401" ]; then
    record_finding "An unauthenticated GET ${API_PROBE_URL} answered '${API_PROBE_STATUS:-nothing}', not 401. Every protected route must refuse a request that carries no verified token."
fi

if [ -n "$POST_DEPLOY_FINDINGS" ]; then
    echo "" >&2
    echo "POST-DEPLOYMENT VERIFICATION FAILED. The release is live and one or more of the" >&2
    echo "controls it claims is not in effect:" >&2
    printf '%s\n' "$POST_DEPLOY_FINDINGS" >&2
    echo "" >&2
    echo "ROLLBACK" >&2
    echo "  There is no image rollback: the backend image is tagged :latest only and this run" >&2
    echo "  overwrote the previous one, so there is no prior tag to return to. Roll back through" >&2
    echo "  configuration and source instead, in this order." >&2
    echo "" >&2
    echo "  1. If the API is refusing legitimate traffic rather than admitting anonymous traffic," >&2
    echo "     widen the configuration rather than disabling a control. ALLOWED_ORIGINS," >&2
    echo "     rate_limit_default, rate_limit_write and csp_report_only are all read at start-up:" >&2
    echo "       kubectl rollout restart deployment -n ${KUBERNETES_NAMESPACE}" >&2
    echo "     auth_token_verifier is read per request and needs no restart." >&2
    echo "  2. To revert the code, tag the current revision first so this state stays" >&2
    echo "     reproducible, then revert and re-run:" >&2
    echo "       git tag failed-deploy-\$(date -u +%Y%m%dT%H%M%SZ) && git revert --no-edit HEAD" >&2
    echo "       ./scripts/deploy.sh" >&2
    echo "  3. Do NOT roll back the Firestore rules to close a client-side symptom. They are" >&2
    echo "     strictly more restrictive than their absence, and removing them reopens every" >&2
    echo "     workbook document to any signed-in caller." >&2
    echo "  4. Do NOT revert file_storage.py on its own. The uploads bucket now enforces uniform" >&2
    echo "     bucket-level access, so the make_public() call the older revision makes fails with" >&2
    echo "     HTTP 400. Revert the Terraform bucket change in the same step or not at all." >&2
    echo "" >&2
    exit 1
fi
echo "  headers, redirect and unauthenticated refusal all confirmed."

# Print deployment status and any necessary manual steps
echo ""
echo "Deployment completed successfully!"
echo ""
echo "https://${DOMAIN_NAME} is the only supported entry point for the application."
# SECURITY: not the only REACHABLE one. google_storage_bucket_iam_member
# .static_assets_public_read grants allUsers read so the load balancer can serve the SPA,
# which also leaves every object fetchable directly from storage.googleapis.com.
# SECURITY: the direct Cloud Storage URL is not advertised. Custom response headers are added
# by the load balancer, so an object fetched straight from storage.googleapis.com carries none
# of them.
echo "Do not use a direct storage.googleapis.com URL: the security response headers are"
echo "added by the load balancer and are absent from a direct bucket request."
echo ""
echo "Already verified automatically, above: the six security response headers on"
echo "https://${DOMAIN_NAME} - each compared against its required value, and the policy against"
echo "each protective directive, not merely for the presence of its name - the permanent redirect"
echo "from http:// to https://, and that an unauthenticated request to ${API_PROBE_URL} is"
echo "refused with 401."
echo ""
echo "Please perform the following manual steps, which the automated checks cannot make:"
echo "1. Confirm a deep link reloaded directly in the browser serves the application, such as"
echo "   https://${DOMAIN_NAME}/workbooks - this exercises the load balancer's 404-to-/index.html"
echo "   rewrite rather than a response header"
echo "2. Sign in through the browser and confirm an authenticated API call succeeds, then reload"
echo "   the page and repeat it. The Redux store does not persist, so this is what confirms the"
echo "   request interceptor takes a fresh Firebase ID token from the SDK rather than from state"
echo "3. Perform a normal cell-editing session and confirm no request is answered 429, so the"
echo "   write-tier rate limit is not tighter than the application's own autosave cadence"
echo "4. Check the backend services are running correctly in GKE, and that the pods run as"
echo "   ${KUBERNETES_SERVICE_ACCOUNT} bound to ${SIGNER_SERVICE_ACCOUNT} through Workload"
echo "   Identity, so signed-URL generation can call signBlob"
echo "5. Confirm ${FUNCTION_NAME} refuses an unauthenticated request and accepts one"
echo "   carrying an identity token for ${SIGNER_SERVICE_ACCOUNT}"
echo "6. Verify database migrations were applied successfully"
echo "7. Confirm the Firestore rules deny a workbook read for a signed-in caller who is neither"
echo "   its owner nor a collaborator, using the emulator procedure in SECURITY.md"
echo "8. Confirm an uploaded object is NOT readable without a signed URL:"
echo "   curl -I https://storage.googleapis.com/${PROJECT_ID}-user-uploads/OBJECT should be 401 or 403"
