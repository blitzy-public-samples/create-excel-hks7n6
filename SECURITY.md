# Security Policy

This document records the security controls that exist in this repository, the exact values
they emit, and the risks that remain open. It is deliberately specific: a control described
here can be checked against the file named beside it.

## Reporting a vulnerability

**Do not open a public issue for a security report.**

No security contact address is published for this project yet, and configuring one is an
outstanding task. Until it exists:

- Report privately to the repository owner through whatever private channel you already have.
- If the repository is hosted on GitHub, enable **private vulnerability reporting** in the
  repository's security settings and use it; that is the intended long-term channel.
- Include the affected file and line, what an attacker can do, and how you established it.

Expect an acknowledgement before any fix is promised. There is no funded bounty.

## Supported versions

One line of development is supported: the current default branch. There are no tagged releases
and no maintained older versions.

Container images are tagged `:latest` and overwritten on each deployment, so **there is no
previous image to roll back to**. Rollback rests on the configuration switches listed under
[Operational switches](#operational-switches) plus reverting the commit.

## Security controls in place

**Read this table as a statement about the code, not about a running system.** Every control below
is implemented, and every one is verified by an automated test against the module that implements
it. None has run inside the application entry point, because `backend/app/main.py` cannot currently
be imported: it imports an `init_db` that is not defined, and the route modules import service
classes that do not exist. Separately, no table-creating DDL exists, so even a startable process
would find no `users` table for the identity lookup to query.

That gap is deliberate — those pieces are excluded from this remediation's authorised scope, and
building them is a feature rather than a security fix — but it must not be read past. It is residual
risk 3 below, and item 1 of the prioritised list in
[Developer Onboarding](./documentation/Developer%20Onboarding.md). Until it closes, these controls
are enforceable rather than enforced.

| Control | Implemented in | Effect |
|---------|---------------|--------|
| Authentication on every endpoint | `backend/app/api/*.py`, `backend/app/core/security.py` | All five routes resolve `Depends(get_current_user)`, which verifies a Firebase ID token. A request without a valid token is refused with `401` and a `WWW-Authenticate: Bearer` challenge. |
| Server-side identity verification | `backend/app/core/security.py` | The ID token is verified against Google's published signing keys with the project pinned explicitly, so identity is not taken from a client assertion. Verification passes `check_revoked=True`, so a token revoked before its expiry — by sign-out, password reset or account disablement — is refused rather than honoured for the rest of its lifetime. |
| Verified email address required | `backend/app/core/security.py` | Identity resolves through the token's `email` claim, so the token must also carry `email_verified` exactly `true`. Without that check, anyone able to self-register a Firebase account naming an existing user's address could authenticate as that user. A truthy string or `1` does not satisfy it. |
| Client credential attachment | `frontend/src/services/api.ts` | An axios request interceptor reads the token from the Firebase SDK per request, so it survives a page reload. It is removed from a failed request before any retry. |
| No public object ACLs | `backend/app/services/file_storage.py`, `infrastructure/terraform/main.tf` | Uploads are returned as expiring V4 signed URLs. The uploads bucket has uniform bucket-level access and `public_access_prevention = "enforced"`, so a public ACL cannot be granted even by code. |
| Explicit CORS allow-list | `backend/app/main.py`, `backend/app/core/config.py` | `allow_methods` is `GET, POST, PUT, OPTIONS` and `allow_headers` is `Authorization, Content-Type` — no wildcards. Origins come from `ALLOWED_ORIGINS`, validated against a single grammar. |
| Database transport encryption | `backend/app/db/database.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf` | The client passes `sslmode`, and the setting accepts exactly two values: `db_sslmode` is `Literal["disable", "require"]`. Cloud SQL is `ENCRYPTED_ONLY`, so the server refuses an unencrypted connection. `allow` and `prefer` are refused because each negotiates plaintext silently whenever the server offers it; `verify-ca` and `verify-full` are refused because the deployment reaches the instance through the Cloud SQL Auth Proxy on a loopback listener, which presents the client no server certificate to verify — the deployment script rejects them for the same reason. `disable` is accepted but is not freely selectable: `Settings` refuses it unless the resolved `DATABASE_URL` names a loopback host — `127.0.0.1`, `localhost`, `::1` or `[::1]` — so it can only ever describe a connection that never leaves the machine, which is what the proxy's listener is. A socket-based URL is not an alternative: the URL grammar requires a host, so `postgresql+psycopg2://user:password@/db?host=/cloudsql/…` is refused one step earlier. Exactly one of the two modes is deployable on any given topology: `disable` on GKE behind the proxy, which is what the pod manifests set, and `require` on a direct connection. |
| Database driver contract | `backend/app/core/config.py`, `backend/app/db/database.py` | `sslmode` is a psycopg2 argument, so `DATABASE_URL` is validated as a synchronous PostgreSQL psycopg2 URL both as an environment value and where the Secret Manager payload overwrites it, and the engine refuses to build on any other dialect. Without this an `asyncpg` or `sqlite` URL would silently discard the transport argument. Rejections never quote the URL, which carries the database password. |
| Security response headers | `backend/app/core/security_headers.py` and three further delivery points | Six header names on every response, including errors and CORS preflight. See below. |
| HTTPS at the edge | `infrastructure/terraform/main.tf`, `scripts/deploy.sh` | A managed certificate and a 443 listener, with port 80 serving only a redirect. |
| Request throttling | `backend/app/core/rate_limit.py` | One ceiling of `600/minute` per client shared **across every route** — an application limit, not a per-endpoint default, so enumerating five endpoints does not yield five quotas — plus a tighter `300/minute` for `POST`/`PUT`/`PATCH`/`DELETE`, returning `429` with `Retry-After`. Counting uses a shared store so the quota does not multiply per worker. The identity is the transport peer, and a peer the server has replaced with an address from a forwarded header is metered against one shared bucket instead — see the row below. |
| Unforgeable throttling identity | `backend/app/core/rate_limit.py` | uvicorn trusts `X-Forwarded-For` from a loopback peer **by default** and rewrites the connection's peer from it before any application middleware runs, so metering the reported peer meant metering an address the caller chose: eight requests rotating that header drew no `429` at all. A rewritten peer is now detected — a forwarded chain has no port, so the rewriting layer reports zero, which an accepted connection never does — and every such request counts against a single fixed bucket, so rotating the header moves a caller between no buckets. A peer that is not an IP address is metered against the shared unknown-client budget rather than becoming a bucket of its own, which bounds what one caller can put in the window store. |
| Bounded failure when the throttling store is unreachable | `backend/app/core/rate_limit.py` | Errors are not swallowed. The ceiling falls back to an in-memory limiter and the write tier re-charges the same window against a process-local limiter, so an outage degrades the quota from fleet-wide to per-worker instead of admitting every request unmetered. |
| Firestore authorization | `firestore.rules` | Document-level rules on `/workbooks/{workbookId}`, keyed on the immutable Firebase UID. A collaborator may read and update the content fields; **`delete` is owner-only**, because deletion is irreversible and removes the workbook from the owner and from every other collaborator. Any unmatched path is denied by the platform default. |
| Cloud Function invoker identity | `scripts/deploy.sh`, `infrastructure/terraform/main.tf` | `--allow-unauthenticated` is not used. Terraform declares the invoker membership **authoritatively**, so any member not listed is removed on every apply. The deployment additionally revokes `allUsers` *and* `allAuthenticatedUsers`, confirms the revocation by re-reading, and then compares every surviving member against an allow-list — so an unexpected principal aborts the deployment rather than passing unremarked. The function also runs as its own service account holding no role, instead of the App Engine default account that carries project Editor. |
| Workload Identity, with signing split from running | `infrastructure/terraform/main.tf` | The pods authenticate as a dedicated runtime service account bound to their Kubernetes service account; a **separate** account exists only to be the resource signed URLs are minted on behalf of, and holds nothing but read access to the uploads bucket. `roles/iam.serviceAccountTokenCreator` is granted from the runtime **on** the signer — a delegation, never a self-grant — and a Terraform precondition fails the plan if the two addresses are equal. |
| Configuration-governed token lifetime | `backend/app/core/security.py` | Lifetime comes from an explicit argument if given, otherwise `ACCESS_TOKEN_EXPIRE_MINUTES`. |
| Pinned dependency manifest | `backend/requirements.txt` | Every direct and transitive version is exact-pinned, holding `python-jose` above the CVE-2024-33663 fix boundary. |
| Dependency audit in CI | `.github/workflows/ci.yml` | The `security-checks` job audits the manifest on every push and pull request and fails on any advisory outside the recorded baseline. Each of the 14 exceptions carries its own justification naming the package, its pinned version, the published fix version and why that fix cannot be taken. `pip-audit` is version-pinned so the gate's meaning cannot change between two runs of the same commit. |
| Immutable CI action references | `.github/workflows/ci.yml` | Every action the `security-checks` job uses is pinned to a commit SHA rather than a tag. A tag is a mutable pointer its own maintainer can move, and this job holds the repository contents and the audit verdict. Each pin names the version it corresponds to, so it is verifiable with `git ls-remote --tags`. |
| Deployment verifies what it claims | `scripts/deploy.sh` | After publishing, the script confirms the six response headers on the live origin, that `http://` answers with a permanent redirect to an `https://` location, and that an unauthenticated API request is refused with `401`. Any failure aborts and prints a rollback procedure. Authorization rules are deployed **before** any code, so no client is ever live against stale rules. |
| Bounded page windows | `backend/app/core/pagination.py`, `backend/app/api/workbooks.py`, `backend/app/api/worksheets.py` | Both list routes cap a page at 100 rows and refuse an offset outside `int64`, so no caller can make the server serialize the whole table and no query-string value reaches SQL as a negative or overflowing bound. A value outside the window answers `422` naming the parameter. The worksheets route previously accepted no page parameter at all, so its response was bounded only by the size of the workbook. |
| Failures reported without their internals | `backend/app/api/cells.py`, `backend/app/api/collaboration.py` | A failed cell update or share answers a fixed message at its unchanged status. The driver message, constraint name, statement and bound values that used to reach the caller are recorded server-side with the exception instead. A refusal the service raised deliberately is re-raised untouched, so a `404` is not flattened into the catch-all. |
| Bounded response size on the wire | `backend/app/main.py` | Responses of 500 bytes or more are compressed when the caller accepts it, measured as 2,742,333 bytes down to 36,648 on the default page for an added 1.1 ms. Placed inside the cross-origin policy and the throttling tiers, so a response either of those produces itself is never rewritten. |
| Bounded request concurrency | `backend/app/core/rate_limit.py`, `backend/app/db/database.py` | Requests in flight are capped at the database pool's capacity, and excess is shed with a `503` and `Retry-After` after a short wait. Throttling bounds arrival *rate*; this bounds *concurrency*, which is the thing that exhausted the pool. Without it a burst inside the configured ceiling waited the full checkout timeout and received a `500`. |
| Connections replaced rather than served stale | `backend/app/db/database.py` | Every checkout is pre-pinged and connections are recycled on a finite interval, so a failover, a maintenance restart or an idle-connection reaper is invisible to the caller instead of producing a burst of `500`s. |
| Security records that reach the log, can be filtered, and can be trusted | `backend/app/core/logging_config.py` | A handler is attached at bootstrap and the application tree is raised to `INFO` where no level was chosen, so the record naming the throttling window store — the only runtime signal that distinguishes the configured quota from a multiple of it — is actually emitted, along with the warning a shed request writes. Every record carries an ISO timestamp, its level and its logger name. Without this the records reached `logging.lastResort`, which writes the bare message and discards anything below `WARNING`: a throttling degradation was indistinguishable from a provider outage and severity-based alerting had nothing to filter on. Control characters in a message are escaped, so a value quoted from a request header cannot forge a second log entry (CWE-117); the escaping is confined to the message, so an appended traceback keeps the newlines that make it readable, and it is applied to the server's own loggers as well, which record the peer address and the request line and do not propagate to the root handler. An explicitly chosen level is never lowered. |
| No secret can be committed by accident | `.gitignore` | `.env`, service-account keys, certificates and Terraform state are ignored; `.env.example` is deliberately not. There was previously no `.gitignore` at all, and a credential that reaches history stays reachable after it is deleted from the working tree — the remedy then is rotating it, not another commit. |

Middleware registration order is itself a control, and three separate properties depend on it. In
`backend/app/main.py` the header middleware is added **last**, making it outermost, which is what
puts the headers on `401`, `429` and CORS preflight responses rather than only on successful ones.
The cross-origin policy is registered **inside** the throttling tiers, because `CORSMiddleware`
answers a preflight itself and never calls the application inside it — registered the other way
round, a preflight was never counted at all, and a browser sends one for every authenticated call
because `Authorization` is not a CORS-safelisted request header. Compression is registered inside
both, so neither the cross-origin policy's own preflight answer nor a throttling refusal is
rewritten by it. Each of those three positions is asserted by a test, so a reordering fails rather
than silently changing what is covered.

## HTTP response headers

Six header names. **Three producers emit them, and the three are not interchangeable.** The five
fixed values below are byte-for-byte identical in all three; the sixth, `Content-Security-Policy`,
is not — see [Content-Security-Policy](#content-security-policy) for which producer differs and
why. A fourth artifact, the compiled document's `<meta>` element, carries a policy but no headers
at all.

| Header | Value |
|--------|-------|
| `Content-Security-Policy` | the policy below (or `Content-Security-Policy-Report-Only` when `csp_report_only` is true) |
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()` |

`X-Frame-Options` duplicates the CSP `frame-ancestors` directive for browsers that do not
support the latter.

### Content-Security-Policy

Thirteen directives, in this order — shown as the API-response copy emits them, which is the
canonical text the static copies extend by adding the API origin to `connect-src`:

```
default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none';
form-action 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self';
style-src-attr 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:;
connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com
https://firestore.googleapis.com https://firebaseinstallations.googleapis.com;
upgrade-insecure-requests
```

`connect-src` carries the four Google endpoints the single-page application calls directly, plus
`'self'`. The API is served from a **separate** origin, which is admitted as one further source.

That is not a deployment choice this configuration leaves open. `var.api_origin` is required,
`variables.tf` rejects an empty value, and a precondition on `google_compute_url_map.excel_app`
rejects `https://<domain_name>` — so the API may not share the edge origin. The reason is
structural: the URL map has exactly one backend, the static bucket, and rewrites an unmatched
path to `/index.html`, so an API base URL on the edge domain would return the SPA document under
`200` where the client expected JSON. A same-origin deployment would not merely need a shorter
`connect-src`; it would need a URL map that routes API paths at all.

The policy reaches a browser from four artifacts, and they do **not** all render the same string.
The distinction is not cosmetic: it is what decides whether adding an API origin in one place is
enough.

| Artifact | Kind | Renders from | Differs how |
|----------|------|--------------|-------------|
| API responses | header producer | `backend/app/core/security_headers.py` | Takes **no API-origin input, and needs none** — this producer serves the API's own origin, where `'self'` already is the API. It renders from `csp_report_only` alone. |
| Frontend container | header producer | `infrastructure/docker/nginx.conf`, substituting `CSP_HEADER_NAME` and `CSP_CONNECT_SRC_API` | The module's policy **extended** by the configured API origin appended to `connect-src`. `CSP_CONNECT_SRC_API` is **required**: the image refuses to start when it is empty or does not begin with a single space, so a container cannot serve a policy that admits no API origin. |
| Load-balancer edge | header producer | the `security_response_headers` local in `infrastructure/terraform/main.tf` | The same extension, rendered from `var.api_origin`. This is the authoritative producer for the live static path, because the compiled bundle is published to Cloud Storage rather than served by the container. |
| Compiled document | **not a header producer** | `frontend/public/index.html` | A `<meta>` element carrying the loading directives only, naming neither `connect-src`, `default-src` nor `frame-ancestors`, so it governs loading and cannot constrain the API. It is a subset of the served policy in every directive **except one**: `style-src-elem` additionally admits `'unsafe-inline'`. See below. |

Because a separate API origin is mandatory, the steady state is the *asymmetric* one: the two
static producers emit the longer `connect-src` and the API producer does not. That is correct
rather than drift — the API producer serves the API's own origin, where `'self'` already **is**
the API, so appending the API origin there would add a source the policy already covers.

The symmetric case is therefore a test fixture rather than a deployment: substituting an empty
`CSP_CONNECT_SRC_API` makes the container render the API module's policy byte for byte, which is
how `test_the_container_policy_is_the_api_policy_plus_the_api_origin` proves the difference
between the producers is exactly that one appended source and nothing else.

**Three consequences worth knowing before changing any of them.** A `<meta>` element cannot carry
`Content-Security-Policy-Report-Only`, so the document's policy is *always enforced* and
`csp_report_only` does not reach it. `frame-ancestors` is ignored in a `<meta>` element entirely,
which is why `X-Frame-Options` is emitted alongside it. And a browser applies every policy it
receives, so the effective policy is the **intersection** of those delivered — widening one
artifact alone does not widen the result.

### The one directive where the document policy is wider

`style-src-elem` in the `<meta>` element admits `'unsafe-inline'`; all three **header** producers
keep it at `'self'`. That is deliberate, and the intersection rule above is what makes it safe.

In both delivered paths the document arrives with a response header beside it — from the load
balancer for the bucket-served site, from Nginx for the container — and that header still says
`style-src-elem 'self'`, so a browser continues to refuse an injected `<style>` element in
production. The development server sends no such header, and there the `<meta>` element is the
only policy. That is exactly where Create React App's `style-loader` injects the application's own
stylesheet as a `<style>` element, which `'self'` refuses — so the application would render
unstyled with a violation, and neither a nonce nor report-only mode is available to a `<meta>`
element. A production build has no such need: `react-scripts build` extracts CSS to a file that
`<link rel="stylesheet">` loads from `'self'`.

The consequence to be aware of: if the compiled bundle were published somewhere that serves **no**
security headers, the `<meta>` policy would be the only one and inline styles would be admitted
there. Both delivery points this repository configures emit the header set.
`test_the_document_policy_diverges_in_exactly_one_place` pins this to one directive and one extra
source, so a second relaxation cannot be added under the same justification. The reasoning is D99.

## Operational switches

Each is a configuration value, documented in [`.env.example`](./.env.example). Those read per
request take effect on the next request; those read when middleware is constructed need a
process restart.

| Switch | Default | Read |
|--------|---------|------|
| `auth_enforcement_enabled` | `true` | per request |
| `auth_token_verifier` | `firebase` | per request |
| `rate_limit_enabled` | `true` | at construction |
| `rate_limit_default` / `rate_limit_write` | `600/minute` / `300/minute` | at construction |
| `csp_report_only` | `false` | at construction |

Every default is the secure position, so a missing value fails closed.

`rate_limit_enabled` governs the two request-**count** tiers and nothing else. The 10 MiB
request-body ceiling has no switch: it is registered whatever that value is, so `false` still
refuses an oversized body with `413`. The two used to be coupled, and `false` withdrew the ceiling
along with the tiers — a control the switch does not name, and one that bounds a different thing
(how much work one request may demand, rather than how many requests a client may make).

The write budget is shared by every mutating request one client makes, so `rate_limit_write` is an
aggregate rather than one route's rate. `300/minute` is set from what the client can produce: it
coalesces cell writes into at most one request per worksheet per 1000 ms, which is 60 a minute per
edited worksheet, so 300 clears five worksheets edited at once plus the workbook-creation and share
routes. Shortening that client window without raising this value would throttle ordinary editing.

`db_sslmode` was listed here as a switch. It is not one in any operational sense. It has exactly two
legal values, `disable` and `require`, and which of them is legal is decided by the connection
topology rather than by preference: `Settings` refuses `disable` unless the resolved `DATABASE_URL`
names a loopback host, so on GKE behind the Cloud SQL Auth Proxy `disable` is the only value that
connects, and on a direct connection `require` is. There is therefore nothing to switch *between* on
a given deployment. It is read at engine construction, so changing it needs a process restart, and
changing the database transport itself means changing the connection topology — see
[Residual risks](#residual-risks) item 2.

**`auth_enforcement_enabled=false` is break-glass only and must never be set in production.**

Its precise effect: **a presented token stops being verified.** A credential is still
required, and exactly one admission path opens.

- **A request carrying no `Authorization` header is refused, whatever the switch says.**
  `oauth2_scheme` carries its default `auto_error`, so the bearer scheme answers `401` with a
  `WWW-Authenticate: Bearer` challenge before any application code runs. An empty
  `Bearer` value is refused by `_resolve_current_user` for the same reason. There is no
  placeholder caller in the module and no configuration value that admits one.
- **A request carrying a token is admitted on its unverified claims.** Signature, expiry,
  revocation and account state are all skipped, so anyone able to mint arbitrary claims for a
  known user's email address is admitted as that user. The identity lookup still runs, so the
  address has to name a row this database holds.

An earlier version of this section stated that the switch also opened a credential-less path,
admitting an identity-less placeholder caller. That is no longer the case and the difference
matters: setting this switch false weakens the credential check to nothing, but it does not
remove the requirement to present one, and no admitted request is anonymous — each one is
attributable to a stored user.

Every admitted bypass is logged at warning level and its response carries
`X-Auth-Enforcement-Bypassed: true`, so a bypass is visible in the logs and in the traffic rather
than silent. That is auditability, not mitigation.

**It is also not the remedy for a client-side lockout.** If the browser stops attaching a token,
revert the interceptor in `frontend/src/services/api.ts` — that restores the previous behaviour
without reopening the API to the internet. `auth_token_verifier=legacy_jwt` is not a remedy
either; see [`.env.example`](./.env.example) for its caller contract.

Two response facts follow from that credential requirement, and are worth knowing before writing a
client:

- A request carrying **no** credential is refused by the OAuth2 scheme with
  `{"detail": "Not authenticated"}`; a credential that is presented and then refused answers
  `{"detail": "Could not validate credentials"}`. Both carry `WWW-Authenticate: Bearer` and
  neither carries a cause.
- A credential that could not be **checked** — unresolvable Application Default Credentials, an
  Admin SDK that will not initialise, unreachable signing certificates, a provider that does not
  answer — answers the same `401`, deliberately: the API returns no `503` and no `Retry-After`,
  because those shapes are not in its response contract. That fault is distinguished in the server
  log instead, at `error` level with the exception context, where an ordinary refusal is logged at
  `warning` with the exception type alone. Alerting belongs on that log level, not on a status
  code.

## Residual risks

Open, and stated rather than implied.

1. **An authenticated user can read another user's workbook.** No route handler filters by
   owner. `Workbook.owner_id` exists but is never consulted, and adding an ownership check needs
   a domain service layer that does not exist. Enforcing authentication turned an anonymous leak
   into an authenticated, attributable cross-tenant read — an improvement, not a complete access
   control. **This is the highest-priority open item.**
2. **No `db_sslmode` value this deployment can use verifies the database server's identity.** On
   GKE the pods reach Cloud SQL through the Auth Proxy and the application's own mode is
   `disable` over a loopback listener, so the application authenticates nothing itself.
   Encryption is not what is lost: the proxy's own leg to the instance is mutually authenticated,
   which is what satisfies `ENCRYPTED_ONLY`, and the unencrypted leg never leaves the pod. On a
   direct connection `require` encrypts the channel but does not authenticate the server, so it
   defeats passive interception and not an active machine-in-the-middle. The two stronger modes
   are **not accepted by `Settings` at all** — `db_sslmode` is `Literal["disable", "require"]`, so
   `verify-ca` and `verify-full` are refused at construction rather than failing later, and
   `scripts/deploy.sh` refuses manifests that set either for the same reason: the proxy's local
   listener carries no certificate for the instance's name. Widening this means changing the
   connection topology — a private IP with the CA distributed to the client, or the Cloud SQL
   Python Connector in place of the proxy.
3. **The application cannot currently start, so nothing above is enforced in a deployment.**
   `backend/app/main.py` imports an `init_db` that is not defined; the four route modules import
   service classes that do not exist; there is no `__init__.py` under `backend/`; and no
   table-creating DDL or migration tool exists, so the identity lookup would query a `users` table
   that has not been created. Each of those is excluded from this remediation's authorised scope and
   each predates it — every affected module is byte-identical from the pre-remediation baseline
   through the current commit. Closing it is item 1 of the prioritised list in
   [Developer Onboarding](./documentation/Developer%20Onboarding.md), and it gates every other item
   on that list. What that means for every claim in the control table above: each is evidenced by
   `backend/tests/test_security.py` and by assertions against the configuration and infrastructure
   files, not by observed production traffic. Do not read the control table above as describing a
   protected running service.
4. **No dependency advisory is currently fixable.** The audit reports 14 advisories across 9
   packages. Every published fix requires Python 3.10 or newer, and `ecdsa` PYSEC-2026-1325 has
   no published fix at all. `infrastructure/docker/Dockerfile.backend` pins `python:3.9-slim`, so
   **upgrading the Python runtime is the single highest-value security task in this repository**;
   until it happens the pins deliver reproducibility, not remediation.
5. **The Firestore rules deny all browser access until documents carry `ownerUid`.**
   `backend/app/services/real_time_sync.py` writes workbook documents without owner or
   collaborator fields. This is the correct fail-closed outcome and it regresses nothing
   measurable, because the browser Firestore path does not currently function. Populating
   `ownerUid` is required before real-time collaboration can work.
6. **Signed URLs require a privilege that cannot be narrowed by IAM conditions.** Signing through
   the IAM `signBlob` API needs `roles/iam.serviceAccountTokenCreator`, and a holder of it can sign
   on behalf of other service accounts in the project. The deployment has **one** service account,
   which is both the workload's runtime identity and the URL signer, and the role is bound on that
   account with the account itself as member — the correct form for a workload signing as itself.
   That means the capability belongs to the identity that serves traffic. The narrowing available is
   what has been done: bound on one account rather than at project level, reached only through GKE
   Workload Identity, and no key file in any image. The escalation surface is still real. Serving
   private objects through an authenticated backend proxy would avoid it entirely.
7. **Revocation checking has an IAM dependency that will fail the whole API if it is missing.**
   Revocation *is* checked — `verify_id_token` is called with `check_revoked=True`, and
   `RevokedIdTokenError` and `UserDisabledError` are both refused — so a signed-out,
   password-reset or disabled user's outstanding token is rejected rather than honoured for its
   full lifetime. An earlier version of this document said revocation was not checked; that was
   wrong. The residual is what the check costs and what it needs. It performs a user-record read
   through the Identity Toolkit API on **every** request, which the runtime is only permitted to
   make because Terraform grants it `roles/firebaseauth.viewer`. Remove that role and the read is
   refused, the verifier reports the provider unavailable, and **every authenticated request is
   refused with `401`** — the API rejects all traffic rather than degrading, and because the
   contract is 401-only that refusal is indistinguishable to the caller from a bad credential,
   separated only by the `error`-level entry in the server log. `scripts/deploy.sh` asserts the
   role in preflight for exactly that reason: the failure mode is a total outage that looks like a
   fleet of bad passwords. The per-request read is also a latency and quota cost on the
   authentication path. Two further points an operator should know before planning around this.
   It is a **deliberate departure from the remediation plan**, which specified default
   verification and named revocation checking a tunable rather than adopting it; the departure
   and its reasoning are recorded as decision D106. And it is **not configurable** — there is no
   switch to turn it off, because the authorised configuration surface is exactly the keys
   `.env.example` documents. Removing the cost therefore means changing the call in
   `backend/app/core/security.py` and accepting the replay window it closes, which is a code
   change and a security decision rather than a deployment setting. A token for a **deleted**
   account is refused by this same read, and is treated as the ordinary stale credential it is:
   `401`, a `warning` naming only the exception type, and no traceback, so no account identifier
   reaches the log.
8. **Identity is keyed on a mutable email address.** The verified token's `email` claim is matched
   against `users.email`, so a user who changes their email address in Firebase no longer resolves
   to their own row — they are refused, and the row is orphaned. Keying on the immutable Firebase
   UID is the fix, and it needs a `firebase_uid` column, which needs migration tooling this
   repository does not have. The registration hole this residual used to carry is **closed**: the
   token's `email_verified` claim must be exactly the boolean `true` before the `email` claim
   selects a row, so registering a Firebase account with an existing local user's address no longer
   admits anybody as them. What replaces it is an operator obligation rather than a risk — a user
   whose Firebase address is unconfirmed is refused *after* a successful sign-in, so either the
   sign-in flow must require verification or account creation in the project must be restricted.
9. **Both runtimes are past end of life.** `infrastructure/docker/Dockerfile.backend` pins
   `python:3.9-slim`, which is the blocker described in item 4. Node 14 is pinned in four places
   — `infrastructure/docker/Dockerfile.frontend`, the `build` job matrix in
   `.github/workflows/ci.yml`, `scripts/setup_dev_environment.sh` and `README.md` — and left
   support on 30 April 2023, so it receives no security fixes and cannot run the `react-scripts` 5
   toolchain the sources need. The `security-checks` CI job therefore pins Node 22 for itself
   rather than inheriting that matrix; the four pins are unchanged because moving them is a
   runtime upgrade outside this change set.
10. **The static assets bucket is directly reachable, and that path is framable.** It grants read
    to `allUsers` so the load balancer can serve it, which also leaves every published object
    readable at `https://storage.googleapis.com/<bucket>/<object>`. That path bypasses the load
    balancer, so it carries none of the response headers above and is not redirected to HTTPS.
    This was measured rather than inferred: the load-balancer path returns six of six headers and
    the direct path zero of six. Two consequences follow, both demonstrated against an unheadered
    origin serving the same document root. The document **loads inside a cross-origin iframe**:
    the request completes with HTTP 200, the whole body is delivered and retained, the frame
    paints, and the console stays silent. The header-protected origin, asked for by an identically
    shaped request from the same embedder, is instead refused after its 200 with
    `net::ERR_BLOCKED_BY_RESPONSE` and the console message *Framing … violates the following
    Content Security Policy directive: "frame-ancestors 'none'"*. Because the two responses carry
    byte-identical bodies — same digest, same `ETag` — and because the document itself contains no
    frame-busting script and no `frame-ancestors` in its `<meta>` policy, framing protection comes
    from the response header alone and nothing in the document can substitute for it. Two
    precisions worth keeping, both of which cut against a careless reading of this evidence.
    `frame-ancestors` is the control actually enforced: no `X-Frame-Options` message was emitted
    for either origin, so the `X-Frame-Options: DENY` in the table above is legacy defence in
    depth for engines predating CSP Level 2 rather than the operative rule. And reading
    `iframe.contentDocument` does **not** distinguish the two: it is `null` from any cross-origin
    embedder for the protected origin *and* the exposed one, so frameability has to be read from
    the network record, the console and the pixels. UI redress does not require reading the framed
    document, only that it renders — which on the direct path it does. That becomes exploitable the
    moment a compiled bundle ships. `X-Content-Type-Options: nosniff` is absent
    here too, so the type confusion it prevents on the load-balancer path is unprevented on this
    one. Cloud Storage cannot close either: an object serves only `Content-Type`,
    `Content-Encoding`, `Content-Disposition`, `Content-Language` and `Cache-Control` from its
    metadata, and no security header is expressible there. Closing it needs the bucket to stop
    granting `allUsers` and the load balancer to read it as an authorized principal — the
    private-bucket-behind-Cloud-CDN arrangement carried as follow-up F12. Until then: nothing
    secret may be published there, and the HTTPS edge is the supported entry point rather than
    the only reachable one. The uploads bucket is the opposite and deliberately so: public access
    prevention is enforced there and access requires a signed URL.
11. **No network segmentation, key management or audit logging.** No VPC, subnet, firewall rule or
    Kubernetes NetworkPolicy is declared; there is no customer-managed encryption key, no key
    rotation policy and no audit logging.
12. **Continuous deployment does not trigger.** `.github/workflows/cd.yml` waits on a workflow
    named `Continuous Integration` while `.github/workflows/ci.yml` is named `CI`, so promotion
    is manual. A deployment assumed to be automatic would silently ship nothing.
13. **`terraform validate` fails on a pre-existing defect.** `infrastructure/terraform/outputs.tf`
    references seven resources that no configuration declares, so the infrastructure changes
    cannot be validated end to end without first correcting that file. `main.tf` and
    `variables.tf` validate clean on their own — verified with `terraform validate` against the
    two files alone, which reports success, while adding `outputs.tf` reports exactly those seven
    errors. This is no longer a one-time measurement: CI runs both halves on every push, so a
    change that stops the configuration loading fails the build. That matters more than it
    sounds, because every cloud-side control here is delivered only by `terraform apply` — a
    configuration full of correct arguments that Terraform refuses to *load* delivers none of
    them while satisfying every text assertion made about it.
14. **Both write ceilings are judgements, not measurements.** The client coalesces cell writes per
    worksheet over a 1000 ms window, which caps it at 60 requests a minute *per worksheet*, while
    `rate_limit_write` defaults to `300/minute` shared across every write route and every
    worksheet — so five worksheets edited continuously at once fill the budget and a sixth is
    refused with `429`. The two were raised together, from 500 ms and `120/minute`, precisely
    because at those values a *single* continuously edited worksheet could consume everything.
    Neither number is measured; both need setting from observed cell-editing and autosave cadence,
    which needs a running application to observe.
15. **One service account holds both the runtime permissions and the signing authority.** That
    single-account topology is what lets signing work from a runtime holding no private key, and
    it is the one address the application reads — but it also means a compromise of the runtime is
    a compromise of the signer. Splitting them requires the runtime to impersonate a separate
    signer identity.
16. **`python-jose` is a thinly maintained component in an authentication path.** It is pinned at
    `3.5.0`, above the `CVE-2024-33663` fix floor, so it carries no known unpatched flaw of its
    own — this is a maintenance-signal risk (CWE-1104), not an open vulnerability. Two concrete
    consequences: it is the sole reason `ecdsa` is in the dependency tree, and `ecdsa`
    PYSEC-2026-1325 has **no published fix on any runtime**, so it is the one advisory the Python
    upgrade cannot clear. The exposure is bounded — the live verification path uses
    `firebase-admin`, and `python-jose` is reached only by `create_access_token` and the
    `legacy_jwt` verifier, neither of which any deployed client uses. It is retained by explicit
    instruction; replacing it with `PyJWT` is about six lines.
17. **A 401 or 429 is classified for the user but not announced to assistive technology.** The
    request seam maps `401`, `403`, `429` and `503` to specific user-facing messages, and the
    workbook screen renders them — but the containers holding those strings carry no
    `role="alert"` or `aria-live`, so a screen-reader user is not told the session ended. Adding
    the attribute requires editing page markup, which this change set may not do.
18. **The dashboard classifies failures and displays none of them.** `frontend/src/pages/Dashboard.tsx`
    only logs to the console, so a caller whose credential expired while on that screen sees an
    empty list rather than an instruction to sign in again. Giving it a visible error surface needs
    new markup, which is outside this change set.
19. **One workbook is loaded by fetching the collection.** No `GET /workbooks/{id}` route exists,
    so the workbook screen reads the list and filters client-side — an O(N) read bounded at 100
    items. Past that boundary the screen now says the workbook "was not in the first 100" rather
    than claiming it does not exist, so the limit is disclosed rather than misreported, but the
    read is still the wrong shape.
20. **CLOSED — the pagination parameters on `GET /workbooks` are now bounded.** This entry described
    `skip` and `limit` accepting any integer and reaching SQL as the `OFFSET` and `LIMIT`, so a
    negative value or one beyond int64 answered an unhandled `500`. Both parameters now carry a
    floor and a ceiling on both list routes, and every out-of-range value answers a `422` naming the
    parameter. The kept-in-place wording is deliberate: this list is cited by number from several
    documents, so a closed entry stays where it is rather than shifting the ones after it. See
    "Bounded page windows" under *Security controls in place*.
21. **Request bodies are validated permissively.** No schema sets `extra`, so an unknown field is
    silently dropped, and Pydantic v1 coerces a value to the declared type rather than refusing it
    — `id=12345` is accepted and becomes `"12345"`. There is no injection consequence, because a
    coerced value reaches SQL only as a bound parameter; the cost is that a client bug surfaces as
    wrong data instead of a `422`. Setting `extra = "forbid"` would change a frozen request
    contract, and change it in the refusing direction.
22. **The create request asks for fields the server assigns.** `POST /workbooks` requires `id`,
    `owner_id` and both timestamps, and refuses a body omitting `id` with a `422` naming it — so a
    caller must invent a primary key for a row that does not exist yet. The route signature reads
    `workbook: WorkbookSchema` and reveals none of that. Splitting the request from the response
    means a second schema, which the frozen contract forbids here.
23. **A repeated create is not idempotent.** The `workbooks` table declares no unique constraint,
    no idempotency key exists on the route, and an identical body submitted twice produces two
    workbooks. The write throttling tier bounds the **rate** at which duplicates can be created and
    nothing bounds the effect, so do not read the tier as an idempotency guarantee.
24. **The specification and documentation endpoints are public.** `/openapi.json`, `/docs`,
    `/redoc` and `/docs/oauth2-redirect` answer `200` with no credential — FastAPI's default, which
    no application router declares. The document carries schema shape only: it holds no account
    address, no database password and no signing key, and the responses still carry all six
    security headers. What an unauthenticated reader learns is the route inventory. Closing it
    means arguments on the application construction, which is in scope here for the CORS, header
    and throttling wiring only.
25. **The default workbook page costs 452 SQL statements and roughly 5.4 seconds to build.** Each
    workbook's worksheets and each worksheet's cells are fetched per row rather than eagerly, and a
    pooled connection is held for the whole of it — which is what made pool exhaustion reachable.
    Two of the three consequences are now closed: the response no longer grows without bound
    (a page is capped at 100 rows) and it no longer costs megabytes of egress (98.66% of the bytes
    are removed by compression). The query cost itself is not: the fix is eager loading inside
    `WorkbookService.get_workbooks`, a class that does not exist and that this work may not build.
    The request-concurrency bound protects the pool from the consequence rather than removing it.
26. **A browser cannot measure how much compression saved it.** Cross-origin
    `PerformanceResourceTiming` reports `transferSize`, `encodedBodySize` and `decodedBodySize` as
    `0` unless the server sends `Timing-Allow-Origin`, which it deliberately does not — that is an
    observability enhancement rather than a security control. Compression is real and was measured
    at the network layer; anyone re-measuring it must do so there too, not from inside a page.
    For the same reason the response's `Content-Encoding` and `Vary` are not exposed to page
    JavaScript: no client code has a use for either, and exposing them would widen the cross-origin
    surface for nothing.

27. **The browser application does not build, so no user-interface behaviour above is observed
    in a browser.** `react-scripts build` fails before emitting a bundle: modules the screens
    import were never authored, and the prop and reducer-payload contracts that would have to
    change to fix the rest live in files this change set is forbidden to edit. Every claim in
    this document about the request seam, the classified failure messages, the dashboard's
    display and the document's own policy meta tags is therefore evidenced by tests and by the
    served document, not by a rendered application. Two consequences worth being plain about.
    Residuals 17 and 18 describe behaviour of a user interface that currently has no rendered
    form, so they are latent rather than active. And nothing visual is merely *unverified* — it
    is **unmeasurable**: no screenshot, breakpoint, focus order or contrast ratio can be taken
    at all, so the absence of a reported visual defect here is not evidence of its absence.
    Closing this needs the modules authored, which is carried as follow-up F52, and the design
    source supplied, which is F53.

28. **The enforcing Content-Security-Policy blanks FastAPI's own `/docs` page.** The generated
    documentation UI loads its CSS and JavaScript from a CDN and bootstraps itself with an inline
    script, and `script-src 'self'` with no `'unsafe-inline'` refuses all of it — so
    `SwaggerUIBundle` is never defined and the page renders empty, with the violations reported in
    the browser console. This is a hardening trade-off working as intended rather than a defect,
    and it is recorded because an operator who opens `/docs` expecting Swagger will otherwise
    conclude the API is broken. The specification itself is unaffected: `/openapi.json` serves
    normally and is what the `security` assertions read. Widening the policy to restore the UI
    would mean admitting a CDN and inline script to every page the policy covers, which is not a
    trade this project makes for a documentation viewer.
29. **The access log records the request line verbatim, including any query string.** Nothing
    redacts it. The application's own client never puts a credential in a URL — the token travels
    in the `Authorization` header — and query-parameter credentials are refused with `401`, so
    this is latent rather than active. It becomes real the moment an integration is added that
    passes a token or another secret as a query parameter: that value would be persisted in the
    log in clear. Control characters in the logged value are escaped, so the line cannot be
    forged, but the value itself is not filtered.

## Compliance

**No compliance standard is claimed as attained.** The project documentation names GDPR, CCPA,
SOC 2 Type II, HIPAA, ISO/IEC 27001 and the OWASP Top 10 as intent. The controls above make
measurable progress on access control and on transport encryption. They make **no** progress on
customer-managed encryption keys or audit logging, both of which remain unimplemented. Treat any
statement of intent in the specification documents as intent, not as attainment.

## Verifying the controls yourself

```bash
PYTHONPATH=. venv/bin/python -m pytest backend/tests/test_security.py -q
```

**785 tests, all passing** as measured by the command above. They assert the `401` on all five routes, rejection of a forged token,
the absence of any public-ACL call, CORS allow and deny behaviour, `sslmode` in the connection
arguments, the header set on success / `401` / `429` / preflight, the exact relationship between
the CSP artifacts described above — including that the API producer takes no API-origin input —
the `429` threshold **counted across different endpoints, different path parameters and paths that
match no route**, the storage-failure path keeping enforcement, and token lifetime in both
directions.

Several tests read the Terraform, Nginx, `index.html`, workflow and deployment files directly, so
a control removed in one place fails a test rather than drifting unnoticed. Those include the
one-account identity topology and its Workload Identity binding, `roles/firebaseauth.viewer`
alongside the `check_revoked` call it exists for, the unconditional port-80 redirect, the absence
of every withdrawn configuration key, the equality of the `.env.example` key set with
`Settings.__fields__` in both directions, each documented switch default against the code's actual
default, and that the CI security job's checks are gates rather than reports.

Note that `python -c "import backend.app.core.security"` is **not** a usable check: constructing
`Settings` performs a live Secret Manager read, so it fails outside a configured Google Cloud
project even with every environment variable set. The suite above stubs that read.

### Verifying the Firestore rules against the real rules engine

The suite above parses `firestore.rules` and asserts its conditions, which catches a rule that
was widened or removed but cannot tell you how Google's evaluator behaves. For that, run the
rules against the emulator. This is the procedure `scripts/deploy.sh` refers to in its
post-deployment manual steps.

```bash
npm install -g firebase-tools          # once
firebase emulators:exec --only firestore \
  "PYTHONPATH=. python backend/tests/firestore_rules_emulator_check.py"
```

`backend/tests/firestore_rules_emulator_check.py` drives the emulator's REST surface directly
and asserts **32 cases**, in both directions. By caller:

| Caller | read | create | update, content fields | delete |
|--------|------|--------|------------------------|--------|
| anonymous | denied | denied | denied | denied |
| signed in, unrelated | denied | denied | denied | denied |
| collaborator | allowed | denied | allowed | **denied** |
| owner | allowed | allowed | allowed | allowed |

The remaining cases are the ones a per-caller table cannot express, and they are the reason the
script exists rather than a handful of spot checks:

- **Ownership cannot be reassigned by anyone, the owner included.** A collaborator promoting
  itself, a collaborator taking ownership, and *an owner handing ownership away* are all denied.
  Only the owner may change the collaborator list.
- **The authorization fields must be present and correctly typed.** A document with no
  `ownerUid`, a non-string `ownerUid`, an empty `ownerUid`, a missing `collaboratorUids`, or a
  `collaboratorUids` stored as a map rather than a list is denied — the map case specifically,
  because `in` would otherwise match its keys.
- **Creation cannot name somebody else as owner**, and a document cannot be created without
  those fields.
- **Unmatched paths are denied**, including a collection with no rule and a subcollection under
  a workbook, which confirms the platform default rather than assuming it.

It is a script rather than a `pytest` module on purpose: it needs a running emulator, so
collecting it into the suite would make the suite fail on any machine without the Firebase CLI.
It exits non-zero on the first mismatch, so it works as a gate wherever an emulator is
available. The collaborator-`delete` row is the one to watch — the rules originally admitted it
while this script already expected it denied, so the rules were contradicting their own
acceptance artefact.

It needs no Google Cloud project and no credentials: the emulator issues its own tokens.

#### Two traps for anyone writing a *different* rules check

Both were found by QA testing, and neither is a defect in the rules. They are recorded because
each one costs an afternoon, and one of them can produce a confident false **PASS**.

**A denial is only visible in the HTTP status on the REST surface.** The script above drives the
emulator's REST API, where a refused read really does come back `403 PERMISSION_DENIED` — which
is why classifying by status is sound *there*, and it is measurable:

```text
owner read      -> HTTP 200  ALLOWED
stranger read   -> HTTP 403  DENIED   {"error":{"code":403,...,"status":"PERMISSION_DENIED"}}
anonymous read  -> HTTP 403  DENIED   {"error":{"code":403,...,"status":"PERMISSION_DENIED"}}
```

The **browser** does not use that surface. `frontend/src/services/collaboration.ts` subscribes
through the Firestore SDK's streaming transport, and there every request — owner, stranger,
anonymous, and a document missing `ownerUid` alike — returns **HTTP 200**, with the refusal
carried inside the channel payload as an `ADD` followed by a `REMOVE` and a `cause.code` of `7`.
QA measured 21 such requests in a browser session and every one of them was a `200`. So a CI step
or an operational check written against browser traffic and asserting on status codes would report
**PASS while the rules were wide open**. Verify either through the script above or by inspecting
the payload — never by status alone on the streaming path.

**The emulator prints an "evaluation error" on some deny paths, and it does not mean the rules are
broken.** A refused read can come back as:

```text
{"error":{"code":403,"message":"\nevaluation error at L132:22 for 'get' @ L132,
 false for 'get' @ L132","status":"PERMISSION_DENIED"}}
```

`L132` is the `allow read` rule. The line accompanies a **correct** `PERMISSION_DENIED`, it also
appears for a document that carries no `ownerUid` and on the `delete` path, and all 32 assertions
above pass while it is being emitted. It is the emulator's verbose diagnostic for a rule term it
could not evaluate to a value on a path that was going to deny anyway. QA formed the hypothesis
that it indicated a rules bug and then disproved it; this note exists so it is not investigated a
third time.

## Related documents

- [Security Decision Log](./documentation/Security%20Decision%20Log.md) — why each control was
  implemented the way it was, with alternatives and risks
- [Security Traceability Matrix](./documentation/Security%20Traceability%20Matrix.md) — each
  vulnerability mapped to its implementation and verification, in both directions
- [Developer Onboarding](./documentation/Developer%20Onboarding.md) — setup, pitfalls, and the
  prioritised list of next tasks
- [`.env.example`](./.env.example) — every configuration key and its accepted values
