# Security Policy

This is the security policy for the Excel Clone repository: a React and TypeScript
single-page application (SPA) backed by a Python FastAPI service, running on Google Cloud
over Cloud SQL for PostgreSQL, Firestore, Cloud Storage, Secret Manager and GKE.

It records four things:

- how to report a vulnerability (section 1);
- which versions are supported (section 2);
- which security controls exist and where they live (sections 3 and 4);
- which risks remain **accepted and unmitigated** (section 5).

Three notes on how to read it.

**Section 4 is authoritative.** It is the single canonical definition of the HTTP security
header set. Four separate artifacts emit those headers and no automated check compares them,
so section 4 is the definition all four are required to match.

**This file records what, not why.** The decisions behind these controls -- the alternatives
that existed, and the risk each choice carries -- are recorded in the
[Security Decision Log](<documentation/Security Decision Log.md>). Coverage from each finding
to its implementation and its verification is in the
[Security Traceability Matrix](<documentation/Security Traceability Matrix.md>), which asserts
that mapping in both directions with no gaps.

**One architectural fact shapes the whole model.** The browser reaches Firestore
**directly**, without traversing the FastAPI service. No server-side control sits on that
path. [`firestore.rules`](firestore.rules) is therefore the only available control there, and
it is not a duplicate of the API's authorization.

---

## 1. Reporting a Vulnerability

Report privately, and give us a chance to fix the issue before it is public.

**Where to send it.** Email `security@<your-domain>`.

> **Operators must replace this address.** `security@<your-domain>` is a placeholder. It is
> not a working mailbox. Substitute a monitored security contact for your deployment before
> publishing this repository or relying on this process.

**Do not** open a public GitHub issue, a discussion or a pull request for a suspected
vulnerability. A public report exposes the issue to everyone before a fix exists.

**What to include.** The more of this you can supply, the faster the triage:

| Item | Detail |
|---|---|
| Affected component | The tier and file or endpoint, for example `backend/app/core/security.py`, `PUT /workbooks/{id}/worksheets/{id}/cells`, the Firestore rules, or a Terraform resource |
| Version | The commit SHA you tested, and the branch. There are no tagged releases (see section 2) |
| Reproduction | Minimal, ordered steps. Include the exact request, the headers that matter, and any configuration the behaviour depends on |
| Observed impact | What an attacker gains: data read, data written, privilege obtained, availability lost |
| Environment | Whether you reproduced it locally, in staging or against a deployed instance |

**What to expect.** These are targets, not guarantees:

- **Acknowledgement** within 3 business days of receipt.
- **Initial assessment** -- whether we can reproduce it, and a provisional severity -- within
  10 business days of acknowledgement.
- **Progress updates** at least every 14 days while the report is open.
- **Coordinated disclosure** once a fix is available, or once we have agreed with you that no
  fix will be made. We will credit you if you want to be credited.

**Testing conduct.** While investigating, do not access, modify, delete or exfiltrate data
belonging to anyone else, and do not degrade service for other users. Test against your own
account and your own data. If a proof of concept unavoidably touches someone else's data,
stop and describe what you found instead of proving it.

Please also read section 5 before reporting. Nineteen risks are already known, documented and
accepted; a report against one of those is a duplicate, not a new finding.

---

## 2. Supported Versions

| Line | Supported | Notes |
|---|---|---|
| `main` | Yes | The only supported line |
| Tagged releases | None exist | The repository publishes no tags and no releases |

Security fixes land on `main`. There is no backport target, because there is no released
version to backport to. Report against a commit SHA.

### Runtime baselines

Both runtimes are past their upstream end of life. This is stated plainly because it bounds
what can be fixed here.

| Runtime | Version | Where it is pinned | Upstream status |
|---|---|---|---|
| Python (backend container) | 3.9 | `infrastructure/docker/Dockerfile.backend` (`FROM python:3.9-slim`), and the CI security job | **End of life 31 October 2025.** Receives no upstream security fixes |
| Node (frontend build toolchain) | 14 | `infrastructure/docker/Dockerfile.frontend` (`FROM node:14 as build`), and the CI build matrix (`node-version: [14.x]`) | **End of life 30 April 2023.** Receives no upstream security fixes |

The deployed Cloud Function runtime is **no longer pinned to Node 14**. It is supplied by the
operator through the Terraform `function_runtime` variable, which has no default and is
validated against a list of runtimes Google has decommissioned -- `nodejs14` among them,
decommissioned for creation and redeployment on 30 January 2025. A decommissioned runtime is
refused at plan time.

### The practical consequence

Auditing `backend/requirements.txt` reports **14 advisories across 9 packages**. Every one of
them has a published fix whose version requires **Python 3.10 or newer**, so none is
installable on the Python 3.9 runtime -- except one, which has no published fix on any runtime.

The dependency-audit gate in `.github/workflows/ci.yml` therefore runs in delta mode: it
ignores a committed baseline of **20 advisory identifiers** and fails only on an advisory
outside it. The baseline is larger than the current report because it also covers build-time
packages the manifest does not declare.

The advisory position is **not clean**, and pinning did not make it clean. What pinning
achieved is that the set is now knowable, auditable and diff-able, and that `python-jose`
cannot silently resolve below the version that fixes CVE-2024-33663. See residual risks 5 and
6 in section 5; the `--ignore-vuln` list in `.github/workflows/ci.yml` is the authoritative
baseline.

---

## 3. Security Controls

What is enforced, and where it lives. Rationale for each is in the
[Security Decision Log](<documentation/Security Decision Log.md>).

| Control | What is enforced | Where it lives |
|---|---|---|
| **API authentication** | Every route resolves an authentication dependency before its handler body runs. An unauthenticated request, or one carrying an invalid token, receives HTTP 401 with a `WWW-Authenticate: Bearer` challenge and never reaches the handler. Applied to all five endpoints: `GET /workbooks`, `POST /workbooks`, `GET /workbooks/{id}/worksheets`, `PUT /workbooks/{id}/worksheets/{id}/cells`, `POST /workbooks/{id}/share` | `Depends(get_current_user)` on each handler in `backend/app/api/workbooks.py`, `worksheets.py`, `cells.py`, `collaboration.py` |
| **Server-side identity verification** | The bearer credential is a Firebase ID token. The Firebase Admin SDK verifies its signature against Google's published signing keys and checks format, expiry, revocation and account state; a revoked token and a disabled account are both refused. Application Default Credentials are used, so no service-account key file is present in the container. Every verification failure maps to the same 401, and only the exception type is logged -- no internal detail reaches the caller | `get_current_user` in `backend/app/core/security.py` |
| **Identity mapping** | The verified `email` claim resolves the local `User` record by `User.email`, which is unique and non-null. A token whose identity matches no local user is refused | `backend/app/core/security.py`, against `backend/app/db/models.py` |
| **Client credential attachment** | An axios request interceptor attaches `Authorization: Bearer <ID token>` to every outbound API request. The token is read from the Firebase SDK at call time, so it survives a page reload and is never held in Redux. A response interceptor strips the header from failed request objects | The axios instance in `frontend/src/services/api.ts` |
| **Object storage confidentiality** | Uploaded objects carry no public ACL and no public URL is produced. `upload_file` returns a time-limited V4 signed URL whose lifetime comes from the `signed_url_expiry_minutes` setting and is capped. The uploads bucket enforces uniform bucket-level access, so object ACLs are disabled, and public access prevention, which overrides any `allUsers` or `allAuthenticatedUsers` grant | `backend/app/services/file_storage.py`; `google_storage_bucket.user_uploads` in `infrastructure/terraform/main.tf` |
| **Signing identity separation** | Two service accounts exist and neither is issued a key. A dedicated signer account holds read access to the uploads bucket; the API runtime identity holds `roles/iam.serviceAccountTokenCreator` **on that signer account alone**, never at project level. Terraform refuses to plan if the two addresses are equal or name an account outside the project | `google_service_account.url_signer`, `google_service_account.api_runtime` and `google_service_account_iam_member.url_signer_token_creator` in `infrastructure/terraform/main.tf` |
| **Cross-origin policy** | An explicit origin allow-list replaces a field that previously did not resolve at all. Methods are limited to `GET`, `POST`, `PUT`, `OPTIONS` and request headers to `Authorization`, `Content-Type`; neither is a wildcard. Credentials remain enabled. A disallowed origin's preflight is rejected and receives no `Access-Control-Allow-Origin` header, and neither does a disallowed simple request | `configure_cors()` in `backend/app/main.py`, reading `ALLOWED_ORIGINS` from `backend/app/core/config.py` |
| **Database transport security** | The client requests TLS through SQLAlchemy connect arguments, using the `db_sslmode` setting. The Cloud SQL instance is set to `ENCRYPTED_ONLY`, so the server refuses an unencrypted connection instead of accepting it | `backend/app/db/database.py`; `google_sql_database_instance.main` in `infrastructure/terraform/main.tf` |
| **HTTP security headers** | The canonical set defined in section 4, emitted at the four delivery points listed there. The header middleware is registered **last**, making it outermost, so 401 responses, 429 responses and CORS preflight responses all carry the headers | `backend/app/core/security_headers.py`, registered in `backend/app/main.py`; `infrastructure/docker/nginx.conf`; `frontend/public/index.html`; `custom_response_headers` in `infrastructure/terraform/main.tf` |
| **Edge TLS** | TLS terminates at the external load balancer with a Google-managed certificate on a 443 listener. Port 80 carries no content: it has no listener at all until an operator enables the `https_cutover_enabled` flag, after which it answers only with a permanent redirect to HTTPS. Terraform refuses to create that redirect while the certificate is still provisioning. Terraform is the sole owner of every edge resource; `scripts/deploy.sh` creates none of them and can no longer stand up a plaintext listener | `google_compute_*` resources in `infrastructure/terraform/main.tf`; the edge assertions in `scripts/deploy.sh` |
| **Rate limiting** | A global per-client request ceiling applies to every route, and a tighter budget applies to the mutating methods `POST`, `PUT`, `PATCH` and `DELETE`. Exceeding either returns HTTP 429; the write tier carries a `Retry-After` header. Both thresholds and the on/off switch are configuration. It is middleware, so no route handler signature changed to accommodate it | `backend/app/core/rate_limit.py`, registered from `backend/app/main.py` |
| **Collaboration store authorization** | Reads of `workbooks/{workbookId}` require the caller's Firebase UID to equal the document's `ownerUid` or to appear in its `collaboratorUids`. A create must be signed in and must name the caller as owner. A collaborator's update cannot rewrite `ownerUid` or `collaboratorUids`; only the owner can delete. Any path no rule matches is denied by Firestore's default. This is the only control on the browser-to-Firestore path, which does not traverse the backend | [`firestore.rules`](firestore.rules), deployed by `scripts/deploy.sh` via the pointer in `firebase.json` |
| **Function invocation** | The Cloud Function grants `roles/cloudfunctions.invoker` to one named service account and no `allUsers` member. Invocation requires an identity token. The deployment script additionally revokes any `allUsers` invoker binding an earlier deployment created, and fails the deploy if that revocation does not take | `google_cloudfunctions_function_iam_member.invoker` in `infrastructure/terraform/main.tf`; `scripts/deploy.sh` |
| **Token lifetime** | The retained token-issuing helper takes its lifetime from an explicit `expires_delta` argument when one is supplied, and from the `ACCESS_TOKEN_EXPIRE_MINUTES` setting otherwise. It is no longer a hardcoded constant | `create_access_token` in `backend/app/core/security.py` |

### The two storage buckets are treated differently, deliberately

This asymmetry is intentional. Presenting both buckets as uniformly hardened would be
inaccurate.

| Bucket | Uniform bucket-level access | Public access prevention |
|---|---|---|
| `user_uploads` | Enabled | **Enforced** |
| `static_assets` | Enabled | **Not enforced** |

`static_assets` serves the public single-page application, and carries an explicit
bucket-level `roles/storage.objectViewer` grant to `allUsers` for that purpose. Public access
prevention would break it. Uniform bucket-level access is still applied there, which disables
object ACLs. Full rationale: see `D5` in the
[Security Decision Log](<documentation/Security Decision Log.md>).

Configuration keys are not listed here. [`.env.example`](.env.example) holds the complete set
with a description of each.

---

## 4. Canonical HTTP Security Header Set

**This section is the authoritative definition.** Four artifacts emit this header set and no
automated check compares them. Any artifact that emits a value differing from the one below is
wrong, and should be corrected to match this table.

Values are given exactly. Copy them verbatim; no judgement call is required and no alternative
is left open.

### 4.1 The header set

| Header | Value | Notes |
|---|---|---|
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` | A two-year maximum age. `includeSubDomains` extends the policy to every subdomain; `preload` permits inclusion in browser preload lists, which protects a first visit. **HTTP response header only** -- there is no `<meta>` equivalent, so this header cannot be delivered by the SPA document |
| `Content-Security-Policy` | `default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://firestore.googleapis.com https://firebaseinstallations.googleapis.com; upgrade-insecure-requests` | Broken out directive by directive in section 4.2. The header **name** varies with the report-only posture (section 4.3). When a separate origin serves the API, exactly one further source is appended to `connect-src` (section 4.2) |
| `X-Frame-Options` | `DENY` | Emitted alongside the CSP `frame-ancestors` directive for browsers that do not support `frame-ancestors`; CSP supersedes it where both are understood. It provides **no** protection on a JSON API response, so on the API tier it is present for uniformity and carries no security weight |
| `X-Content-Type-Options` | `nosniff` | Stops the browser guessing a response's type in place of its declared `Content-Type` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | The canonical value. Sends the origin only on a cross-origin request, and nothing at all when leaving HTTPS for HTTP |
| `Permissions-Policy` | `accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()` | An empty allow-list per feature disables it for the document and for every frame. The application uses none of these |

For copy-paste, the Content-Security-Policy value on a single line:

```text
default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://firestore.googleapis.com https://firebaseinstallations.googleapis.com; upgrade-insecure-requests
```

### 4.2 The Content-Security-Policy, directive by directive

Directive order is as listed. The directives are joined with a semicolon followed by a single
space (`"; "`), and the value carries no trailing semicolon.

| Directive | Value | What it covers |
|---|---|---|
| `default-src` | `'self'` | The fallback for every fetch directive not named below |
| `base-uri` | `'self'` | Stops injected markup repointing relative URL resolution |
| `object-src` | `'none'` | No plugin content |
| `frame-ancestors` | `'none'` | Nothing may frame the document. **Ignored inside a `<meta>` tag** (see section 4.4) |
| `form-action` | `'self'` | Form submission targets |
| `script-src` | `'self'` | The compiled SPA bundle, served from the same origin |
| `style-src` | `'self'` | Stylesheet sources |
| `style-src-elem` | `'self'` | Stylesheet elements |
| `style-src-attr` | `'unsafe-inline'` | Inline `style` attributes, which the styling toolchain emits. This applies to style attributes only and grants nothing to script |
| `img-src` | `'self' data: blob:` | Same-origin images, plus the `data:` and `blob:` URLs the build and the export paths produce |
| `font-src` | `'self' data:` | Same-origin fonts, plus `data:` fonts from the build |
| `connect-src` | `'self'` plus the four Google endpoints below | The API and the endpoints the SPA calls directly |
| `upgrade-insecure-requests` | (no value) | Rewrites an `http:` subresource URL to `https:` before fetching |

The four `connect-src` sources beyond `'self'` are required. Sign-in and real-time
synchronisation fail with an enforced policy that omits them:

| Source | Used for |
|---|---|
| `https://identitytoolkit.googleapis.com` | Identity Platform sign-in |
| `https://securetoken.googleapis.com` | ID token refresh |
| `https://firestore.googleapis.com` | The direct browser-to-Firestore collaboration path |
| `https://firebaseinstallations.googleapis.com` | Firebase SDK installation registration |

**A separate API origin adds exactly one more source.** `'self'` covers the API only when one
origin serves both the SPA and the API. Otherwise the API's origin -- scheme, host and
optional port, nothing more -- is appended to `connect-src` through the Terraform `api_origin`
variable and the frontend image's `CSP_CONNECT_SRC_API` variable. Both static delivery paths
then publish one identical policy. With `api_origin` left empty while the API is in fact
cross-origin, an enforcing browser blocks every API call.

**Analytics is currently commented out.** `frontend/public/index.html` contains a
commented-out third-party tag-manager block. Enabling it requires adding that origin to
`script-src` and `connect-src` at every delivery point; the policy above does not permit it.

### 4.3 Report-only posture

The policy value never changes. Only the header **name** changes:

| Posture | Header name emitted |
|---|---|
| Enforcing | `Content-Security-Policy` |
| Report-only | `Content-Security-Policy-Report-Only` |

Exactly one of the two is emitted. The selector is the backend `csp_report_only` setting, the
Terraform `csp_report_only` variable and the frontend image's `CSP_HEADER_NAME` variable;
these must agree. All three default to enforcing. The backend setting is read once, when the
middleware is constructed, so changing it needs a pod restart.

**The toggle does not reach delivery point 2.** The `<meta>` policy in
`frontend/public/index.html` is static and always enforcing; it has no report-only form. A
policy that breaks the compiled application cannot be defanged by the setting there -- it has
to be corrected by rebuilding and republishing the bundle. Section 4.4 of the
[Developer Onboarding guide](<documentation/Developer Onboarding.md>) covers moving from
report-only to enforcing.

### 4.4 Delivery points

| # | Artifact | Covers | Effective |
|---|---|---|---|
| 1 | `backend/app/core/security_headers.py`, registered in `backend/app/main.py` | Every API response, **including 401, 429 and CORS preflight responses**. The middleware is added last, which makes it outermost; registered any deeper, error and preflight responses would escape it | On backend deploy |
| 2 | `frontend/public/index.html` meta tags | The live SPA, which is served as static files from a Cloud Storage bucket. **Fetch directives and the referrer policy only** -- see the limitation below | Immediately, with no infrastructure change |
| 3 | `infrastructure/docker/nginx.conf` | The container-served SPA, with the full header set. This file is an nginx template; the container entrypoint expands the two `CSP_` variables before nginx starts | Only if and when the frontend image is deployed |
| 4 | `custom_response_headers` on the Terraform backend bucket | The load-balancer edge in front of the static bucket. The authoritative delivery point for the static path | On `terraform apply` |

Headers at delivery point 4 are attached only to requests that traverse the load balancer.
A direct `storage.googleapis.com` request for the same object does not receive them.

#### The meta-tag limitation, stated explicitly

Delivery point 2 **cannot** carry the full set. Two directives are unavailable in a `<meta>`
tag, and both are load-bearing:

- **`frame-ancestors` is ignored when specified in a `<meta>` tag.** It does not fall back to
  `default-src`. Clickjacking protection for the SPA document therefore comes from the
  `Content-Security-Policy` and `X-Frame-Options` **HTTP response headers** at delivery points
  3 and 4, never from the meta tag.
- **`Strict-Transport-Security` has no meta-tag form at all.** Transport protection comes from
  delivery points 3 and 4 only.

The meta tag in `frontend/public/index.html` accordingly carries the CSP fetch directives
without `frame-ancestors`, plus a `referrer` meta tag. It is defence in depth for the fetch
directives; it is not a substitute for the headers.

#### Why there are four delivery points and not one

This is a fact about the deployment topology. Only the backend container image is built and
pushed; the compiled SPA is published to a Cloud Storage bucket by `gsutil rsync`, so the
frontend nginx image is not the artifact serving live users. Headers placed only in
`nginx.conf` would reach nobody today. See `D9` in the
[Security Decision Log](<documentation/Security Decision Log.md>).

---

## 5. Accepted Residual Risks

**Read this section before treating the application as secured.** The controls in section 3
close a specific set of gaps. They do not produce a complete security posture, and the
following risks are known, accepted and currently unmitigated. They are stated without
softening.

`F<n>` references name the follow-up items in section 5 of the
[Security Decision Log](<documentation/Security Decision Log.md>).

| # | Risk | Impact | Status / planned remediation |
|---|---|---|---|
| 1 | **Authenticated cross-tenant access is still possible.** No route handler filters by owner. Any authenticated caller can address another user's `workbook_id`, worksheet or cell range | Confidentiality and integrity, across tenants. Enforcing authentication converted an unauthenticated data leak into an **authenticated, attributable** cross-tenant read and write. That is an improvement; it is not authorization | **Open. Highest-priority outstanding item.** Owner scoping needs the domain service layer that is out of scope here. `F2` |
| 2 | **The Firestore rules currently deny all browser access.** No writer populates `ownerUid` or `collaboratorUids` on workbook documents, so no document satisfies the owner or collaborator test | Browser-side collaboration reads and writes are refused. This is the **intended fail-closed state**, not a misconfiguration. Nothing measurable regresses: the browser collaboration path is already non-functional for an unrelated reason (risk 12), and backend writes are unaffected because server client libraries bypass Security Rules entirely | **Open by design.** Populating the two fields makes the rules admit the owner and collaborators. `F4` |
| 3 | **The database server's identity is not verified.** `sslmode=require` encrypts the channel and proves nothing about who is on the other end of it | Defeats passive interception of database traffic. Does **not** defeat a determined active machine-in-the-middle | **Open.** `verify-full` needs CA material distributed to the client; the Cloud SQL Connector needs a new dependency and new connection code. `F8`, `F9` |
| 4 | **ID-token verification depends on a reachable identity provider, and costs a lookup per request.** Revocation and account state *are* checked, so a signed-out, password-reset or disabled user's token is refused -- but that check performs a user-record lookup on every request, and a credential or provider failure is refused as a 401 | A Firebase outage becomes an authentication outage. Added per-request latency on every API call | **Accepted.** The `auth_token_verifier` setting selects the local verification path per request with no restart if the provider path must be abandoned |
| 5 | **The known dependency advisories cannot be remediated on the current runtime.** Auditing the pinned manifest reports 14 advisories across 9 packages; every published fix version requires Python 3.10 or newer and will not install on Python 3.9 | Most consequential by far: `starlette`, the ASGI layer **every request traverses**, carries five of the fourteen. The rest fall in `urllib3` (two), `requests`, `click`, `pytest`, `msgpack`, `h2`, `python-dotenv` and `ecdsa` | **Blocked on the runtime upgrade.** The CI gate ignores a committed 20-identifier baseline and fails on anything outside it, so a *new* advisory is still caught. `F1` to upgrade the runtime, then `F15` to make the gate strict |
| 6 | **One transitive advisory has no published fix on any runtime.** `ecdsa` 0.19.2 arrives through the retained `python-jose` JWT library | Bounded: after the identity bridge, the live authentication path uses the Firebase Admin SDK. `python-jose` remains only for the retained token-issuing helper, which no route calls | **Accepted.** No fix exists to apply. Retaining `python-jose` is a recorded decision (`D14`) |
| 7 | **Signed-URL generation needs a permission that IAM Conditions cannot constrain.** A holder of `roles/iam.serviceAccountTokenCreator` can sign data on behalf of any service account in the project, and the underlying `signBlob` method cannot be limited conditionally | If the runtime identity were compromised, that permission is a privilege-escalation path within the project | **Mitigated, not eliminated.** The grant is bound to a single dedicated signer service account and is never made at project level; no key file is distributed. `F12` re-evaluates the mechanism |
| 8 | **Relational identity is keyed on a mutable claim.** The bridge resolves the verified `email` claim against `User.email`. The `User` model has no immutable-UID column and the repository has no migration tooling | A user who changes their Firebase email address loses their relational identity and the data attached to it. Note the deliberate asymmetry: the Firestore rules key on the **immutable** Firebase UID, while the relational bridge keys on email | **Open.** Adding a `firebase_uid` column requires migration tooling that does not exist. `F5` |
| 9 | **Control of the email address is not proven.** Verification accepts the `email` claim without requiring `email_verified`. A Firebase account that never demonstrated control of an address can authenticate as the local user holding it | An attacker able to create an account in the Firebase project with a chosen email address impersonates the local user with that address | **Open.** The remedy is at the identity provider: restrict who may create accounts in the project, or require verification in the sign-in flow. `F21` |
| 10 | **A break-glass toggle can disable route authentication.** Setting `auth_enforcement_enabled` false serves requests on unverified token claims | **Exercising it reintroduces unauthenticated access in full**, reopening the original gap on all five endpoints | **Guarded, never to be set in production.** It defaults to the secure position; each bypassed request emits a warning log line and the response carries the `X-Auth-Enforcement-Bypassed` marker header. For a lockout, the preferred remedy is reverting the client interceptor, which restores the previous behaviour without reopening the API. `F20` asserts the middleware order the marker depends on |
| 11 | **Disabling throttling is silent.** With `rate_limit_enabled` false there is no marker header and no per-request record | A deployment with throttling off is indistinguishable on the wire from one with it on | **Open.** `F19` |
| 12 | **The frontend cannot install, build, test or audit.** `firebase` and `react-scripts` are imported but not declared in `frontend/package.json`, and no `package-lock.json` is committed even though the CI workflow runs `npm ci`, which requires one | The client half of the identity bridge is not installable as the manifest stands, so **the client-side token attachment cannot ship** until this is fixed. `npm audit` cannot run either, so the frontend has no advisory feed. Pre-existing; neither caused nor fixed by this work | **Open, and a prerequisite for shipping the client credential attachment.** `F3` |
| 13 | **Infrastructure changes cannot be machine-validated in-repo.** `terraform validate` fails on the current commit: `infrastructure/terraform/outputs.tf` references six resources that do not exist | The TLS, bucket, IAM and edge changes cannot be checked by tooling here. They require human inspection of the plan output before apply | **Open.** `outputs.tf` is out of scope for this work |
| 14 | **Continuous deployment does not fire automatically.** `cd.yml` waits on a workflow named `Continuous Integration`; `ci.yml` is named `CI` | Promotion is a manual operation. A deploy assumed to be automatic ships nothing | **Open, flagged not fixed** at the request of the change authorisation |
| 15 | **Image rollback is not possible.** Container images are tagged `:latest` only and overwritten on each deploy | There is no previous image to return to. Rollback rests on configuration toggles plus a tagged `git revert`, promoted through staging | **Accepted.** The tagging strategy is a deployment concern outside this work (`D17`) |
| 16 | **Internal exception text is returned to callers.** `backend/app/api/cells.py` and `backend/app/api/collaboration.py` place `str(e)` in response details (CWE-209) | Internal detail -- potentially including schema or driver information -- reaches an authenticated caller | **Open.** Handler bodies are out of scope here; **new code does not replicate the pattern**, and the identity-verification path deliberately logs the cause server-side only. `F7` |
| 17 | **No network segmentation, no customer-managed encryption keys, and no audit logging.** No VPC, subnet, firewall rule or Kubernetes network policy is codified; no CMEK, no key-rotation policy, no security audit trail | There is no compensating control behind any trust boundary, and no record of security-relevant events. See section 7 for the compliance consequence | **Open.** None of these is implemented, and none should be read as a control. `F16` |
| 18 | **`WWW-Authenticate` is not readable by cross-origin JavaScript.** The header is present on every 401 on the wire, but the CORS policy does not expose it | A cross-origin client cannot distinguish an authentication challenge from another 401 by reading the header | **Accepted.** Exposing it means changing the CORS configuration in an application entry point scoped narrowly here |
| 19 | **Nothing tracked prevents a populated `.env` being staged.** The repository has no root ignore file | A committed `.env` would publish `SECRET_KEY` and the database password | **Open.** [`.env.example`](.env.example) warns explicitly. Use a clone-local exclude in the meantime. `F17` |

Section 4 of the [Security Decision Log](<documentation/Security Decision Log.md>) is the
complete accepted-residual register and records why each was accepted. For the full prioritised
inventory of follow-up work, see section 5 of the
[Developer Onboarding guide](<documentation/Developer Onboarding.md>).

---

## 6. Verification

A reference list of how each control is checked. Setup is in section 1 of the
[Developer Onboarding guide](<documentation/Developer Onboarding.md>).

### 6.1 Checks that run in the build environment

Run from the repository root, with the virtual environment active and `PYTHONPATH` set to the
repository root.

| Control | Command | Pass condition |
|---|---|---|
| Authentication, CORS, headers, throttling, token lifetime, signed URLs | `python -m pytest backend/tests/test_security.py -q --no-header -p no:cacheprovider` | All tests pass. This module covers the 401 on each of the five routes, the `WWW-Authenticate` challenge, forged-token rejection, the absence of a public-ACL call, CORS allow and deny, the `sslmode` connect argument, header presence on success, 401, 429 and preflight responses, the 429 threshold with `Retry-After`, and token lifetime in both directions |
| Security module imports | Covered inside the run above by `test_v2_security_module_imports` and `test_v2_optional_annotation_resolves` | Both pass. **A bare `python -c "import backend.app.core.security"` is not a local check:** importing any backend module constructs `Settings`, which requires the six secret-backed variables *and* performs live Secret Manager reads, and `backend/app/db/database.py` binds the SQLAlchemy engine at import. The test fixtures neutralise all three; a bare interpreter does not. See section 3 of the [Developer Onboarding guide](<documentation/Developer Onboarding.md>) |
| No regression in the existing suite | `python -m pytest backend/tests/ -q --no-header` | The **same three collection errors** present before this work, and no new one. The pre-existing suite collects zero tests; a green suite is not the standard here |
| Dependency advisories | `python -m pip_audit -r backend/requirements.txt --progress-spinner off` with the `--ignore-vuln` baseline from `.github/workflows/ci.yml` | No advisory outside the committed baseline. The baseline itself is expected to report (residual risks 5 and 6). The audit tool is deliberately absent from `backend/requirements.txt`; install it where it runs |
| Syntax of the changed Python modules | `python -m py_compile backend/app/core/security.py backend/app/core/config.py backend/app/main.py backend/app/db/database.py backend/app/services/file_storage.py backend/app/core/rate_limit.py backend/app/core/security_headers.py` | Exits 0 |
| Deployment script syntax | `bash -n scripts/deploy.sh` | Exits 0. On Windows use Git Bash; WSL Bash misreads the working tree |
| Terraform formatting | `terraform -chdir=infrastructure/terraform fmt -check main.tf variables.tf` | Exits 0. Checking the **whole directory** exits non-zero and names `outputs.tf`, which is unformatted and untouched by this work. `terraform validate` also **fails on this commit**, for the same file's pre-existing references (residual risk 13) |

### 6.2 Checks that require a deployment

> **These need cloud tooling that is not installed in the build environment.** `gcloud`,
> `gsutil`, `psql` and the `firebase` CLI are all absent locally. Every check below is a
> deployment-time step. Replace each `<PLACEHOLDER>` with a value for your deployment.

| Control | Command | Pass condition |
|---|---|---|
| API authentication | `curl -si https://<API_HOST>/workbooks` | HTTP 401 with `WWW-Authenticate: Bearer`. Then repeat with `-H "Authorization: Bearer <ID_TOKEN>"` and expect a 2xx |
| Object confidentiality | `curl -I https://storage.googleapis.com/<BUCKET>/<OBJECT>` | HTTP 401 or 403. **A 200 or a 302 means the object is still public.** A valid signed URL succeeds before its expiry and fails after |
| Bucket policy | `gcloud storage buckets describe gs://<BUCKET> --format='value(iamConfiguration.publicAccessPrevention,iamConfiguration.uniformBucketLevelAccess.enabled)'` | `enforced` and `True` for the uploads bucket. The static-assets bucket reports uniform access enabled and public access prevention **not** enforced, by design (section 3) |
| Cross-origin policy | `curl -si -X OPTIONS -H "Origin: https://disallowed.example" -H "Access-Control-Request-Method: GET" https://<API_HOST>/workbooks` | Rejected, and **no `Access-Control-Allow-Origin` header in the response**. An allowed origin receives its own origin echoed exactly, with credentials enabled |
| Database TLS, server side | `gcloud sql instances describe <INSTANCE> --format="json(settings.ipConfiguration.sslMode)"` | `ENCRYPTED_ONLY` |
| Database TLS, live session | `SHOW ssl;` or `SELECT * FROM pg_stat_ssl;` in a session | SSL in use. **Inspect the session, not the engine:** SQLAlchemy merges `connect_args` at connect time, so `sslmode` never appears on the engine object |
| Security headers | `curl -sI https://<DOMAIN>/` and `curl -sI https://<API_HOST>/workbooks` | All six headers from section 4.1 present on both origins, with values matching that table exactly |
| Edge TLS | `openssl s_client -connect <DOMAIN>:443 -tls1_3 </dev/null` | A successful handshake against the managed certificate |
| HTTP redirect | `curl -sI http://<DOMAIN>/` | HTTP 301 to `https://`. **Applies only after the cutover:** before `https_cutover_enabled` is set, port 80 has no listener and the connection is refused, which is also a pass |
| Rate limiting | `for i in $(seq 1 <N>); do curl -s -o /dev/null -w "%{http_code} " -X POST https://<API_HOST>/workbooks; done` | HTTP 429 past the configured write threshold, carrying `Retry-After`. Validate the threshold against a real cell-editing and autosave cadence, not only a synthetic loop |
| Firestore rules | `firebase emulators:exec --only firestore "<assertions>"` | Owner admitted; non-owner denied; unauthenticated denied. A document lacking `ownerUid` is denied to everyone (residual risk 2) |
| Function invoker | `curl -si <FUNCTION_URL>` | HTTP 403. Then repeat with `-H "Authorization: Bearer $(gcloud auth print-identity-token)"` and expect success |

Adversarial checks worth running in staging: enumerate object names anonymously against the
uploads bucket; issue a credentialed `fetch` from an unlisted origin; replay an expired signed
URL; present a token signed by a different Firebase project; read a workbook the signed-in user
does not own directly from a browser console.

---

## 7. Compliance Posture

The repository states compliance intent in two documents. Both are cited here by their real
in-repo location.

**Requirement identifiers** are defined in the
[Software Requirements Specification](<documentation/Software Requirements Specifications (SRS).md>),
around lines 476 to 487. `SECU-001` is "Security Features", priority High, and its
sub-requirements include:

| ID | Requirement |
|---|---|
| `SECU-001-01` | Workbook and worksheet password protection |
| `SECU-001-02` | Data encryption using Google Cloud KMS |
| `SECU-001-03` | Information Rights Management |
| `SECU-001-04` | Secure external data connections |
| `SECU-001-05` | Audit logging for security events |

**Encryption and regulatory intent** is recorded in the
[Technical Specifications](<documentation/Technical Specifications.md>), under the unnumbered
`# SECURITY CONSIDERATIONS` heading: at-rest encryption using Google Cloud KMS with AES-256 and
in-transit encryption using TLS 1.3 (around lines 650 to 652), GDPR and CCPA (around line 109),
and ISO 27001, SOC 2 and GDPR (around line 731).

### Where this work lands against that intent

| Intent | Position |
|---|---|
| `SECU-001` (Security Features) | **Measurable progress.** Authentication is enforced on every route and verified server-side; document-level authorization is enforced on the collaboration store. Authorization is **not complete** -- see residual risk 1 |
| In-transit encryption (TLS) | **Measurable progress.** TLS terminates at the edge with a managed certificate, and the database connection is encrypted with the server refusing cleartext. The database server's identity is **not** verified -- see residual risk 3 |
| `SECU-001-02` (Cloud KMS encryption) | **No progress.** No customer-managed key, no key-rotation policy. Out of scope for this work |
| `SECU-001-05` (Audit logging) | **No progress.** No security audit trail exists |
| `SECU-001-01`, `-03`, `-04` | **No progress.** Not addressed by this work |
| GDPR, CCPA, SOC 2, ISO 27001 | **Not assessed.** No control mapping, no evidence collection and no audit has been performed |

> **No claim of compliance attainment is made or implied.** The statements above describe
> engineering work against requirements recorded in this repository. They are not an assessment
> against any regulation, framework or certification, and they are not a substitute for one.

---

## 8. Related Documents

| Document | What it holds |
|---|---|
| [Security Decision Log](<documentation/Security Decision Log.md>) | The rationale for every non-trivial decision: what was decided, what alternatives existed, why, and what risk it carries. **The single source of truth for "why".** Also holds the deviation register, the complete accepted-residual register and the follow-up (`F<n>`) list |
| [Security Traceability Matrix](<documentation/Security Traceability Matrix.md>) | Bidirectional coverage: every finding to its implementation and verification, and every changed artifact back to its justification |
| [Developer Onboarding](<documentation/Developer Onboarding.md>) | Clean machine to running application: setup, domain context, common pitfalls, how to extend, and the prioritised suggested-next-tasks inventory |
| [`.env.example`](.env.example) | The complete configuration key set, with a description and a safe placeholder for each |
| [`firestore.rules`](firestore.rules) | The collaboration-store authorization policy, and the only control on the direct browser-to-Firestore path |
| [`README.md`](README.md) | Top-level project orientation |
| [Software Requirements Specification](<documentation/Software Requirements Specifications (SRS).md>) | The `SECU-001` requirement identifiers |
| [Technical Specifications](<documentation/Technical Specifications.md>) | The encryption and regulatory intent, and the security architecture |
