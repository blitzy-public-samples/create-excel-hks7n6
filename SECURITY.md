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
| Database transport encryption | `backend/app/db/database.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf` | The client passes `sslmode`, and the setting accepts exactly one value: `require`. Cloud SQL is `ENCRYPTED_ONLY`, so the server refuses an unencrypted connection. `disable`, `allow` and `prefer` are refused because each can complete without encryption; `verify-ca` and `verify-full` are refused because the deployment reaches the instance through the Cloud SQL Auth Proxy on a loopback listener, which presents the client no server certificate to verify — the deployment script rejects them for the same reason. |
| Database driver contract | `backend/app/core/config.py`, `backend/app/db/database.py` | `sslmode` is a psycopg2 argument, so `DATABASE_URL` is validated as a synchronous PostgreSQL psycopg2 URL both as an environment value and where the Secret Manager payload overwrites it, and the engine refuses to build on any other dialect. Without this an `asyncpg` or `sqlite` URL would silently discard the transport argument. Rejections never quote the URL, which carries the database password. |
| Security response headers | `backend/app/core/security_headers.py` and three further delivery points | Six header names on every response, including errors and CORS preflight. See below. |
| HTTPS at the edge | `infrastructure/terraform/main.tf`, `scripts/deploy.sh` | A managed certificate and a 443 listener, with port 80 serving only a redirect. |
| Request throttling | `backend/app/core/rate_limit.py` | One ceiling of `600/minute` per client shared **across every route** — an application limit, not a per-endpoint default, so enumerating five endpoints does not yield five quotas — plus a tighter `300/minute` for `POST`/`PUT`/`PATCH`/`DELETE`, returning `429` with `Retry-After`. Counting uses a shared store so the quota does not multiply per worker. |
| Bounded failure when the throttling store is unreachable | `backend/app/core/rate_limit.py` | Errors are not swallowed. The ceiling falls back to an in-memory limiter and the write tier re-charges the same window against a process-local limiter, so an outage degrades the quota from fleet-wide to per-worker instead of admitting every request unmetered. |
| Firestore authorization | `firestore.rules` | Document-level rules on `/workbooks/{workbookId}`, keyed on the immutable Firebase UID. A collaborator may read and update the content fields; **`delete` is owner-only**, because deletion is irreversible and removes the workbook from the owner and from every other collaborator. Any unmatched path is denied by the platform default. |
| Cloud Function invoker identity | `scripts/deploy.sh`, `infrastructure/terraform/main.tf` | `--allow-unauthenticated` is not used. Terraform declares the invoker membership **authoritatively**, so any member not listed is removed on every apply. The deployment additionally revokes `allUsers` *and* `allAuthenticatedUsers`, confirms the revocation by re-reading, and then compares every surviving member against an allow-list — so an unexpected principal aborts the deployment rather than passing unremarked. The function also runs as its own service account holding no role, instead of the App Engine default account that carries project Editor. |
| Workload Identity, with signing split from running | `infrastructure/terraform/main.tf` | The pods authenticate as a dedicated runtime service account bound to their Kubernetes service account; a **separate** account exists only to be the resource signed URLs are minted on behalf of, and holds nothing but read access to the uploads bucket. `roles/iam.serviceAccountTokenCreator` is granted from the runtime **on** the signer — a delegation, never a self-grant — and a Terraform precondition fails the plan if the two addresses are equal. |
| Configuration-governed token lifetime | `backend/app/core/security.py` | Lifetime comes from an explicit argument if given, otherwise `ACCESS_TOKEN_EXPIRE_MINUTES`. |
| Pinned dependency manifest | `backend/requirements.txt` | Every direct and transitive version is exact-pinned, holding `python-jose` above the CVE-2024-33663 fix boundary. |
| Dependency audit in CI | `.github/workflows/ci.yml` | The `security-checks` job audits the manifest on every push and pull request and fails on any advisory outside the recorded baseline. Each of the 14 exceptions carries its own justification naming the package, its pinned version, the published fix version and why that fix cannot be taken. `pip-audit` is version-pinned so the gate's meaning cannot change between two runs of the same commit. |
| Immutable CI action references | `.github/workflows/ci.yml` | Every action the `security-checks` job uses is pinned to a commit SHA rather than a tag. A tag is a mutable pointer its own maintainer can move, and this job holds the repository contents and the audit verdict. Each pin names the version it corresponds to, so it is verifiable with `git ls-remote --tags`. |
| Deployment verifies what it claims | `scripts/deploy.sh` | After publishing, the script confirms the six response headers on the live origin, that `http://` answers with a permanent redirect to an `https://` location, and that an unauthenticated API request is refused with `401`. Any failure aborts and prints a rollback procedure. Authorization rules are deployed **before** any code, so no client is ever live against stale rules. |
| No secret can be committed by accident | `.gitignore` | `.env`, service-account keys, certificates and Terraform state are ignored; `.env.example` is deliberately not. There was previously no `.gitignore` at all, and a credential that reaches history stays reachable after it is deleted from the working tree — the remedy then is rotating it, not another commit. |

Middleware registration order is itself a control. In `backend/app/main.py` the header
middleware is added **last**, making it outermost, which is what puts the headers on `401`,
`429` and CORS preflight responses rather than only on successful ones.

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
`'self'`. Where the browser is served a *separate* API origin, that origin has to be admitted as
one further source; where one origin serves both the application and the API, `'self'` already
covers it.

The policy reaches a browser from four artifacts, and they do **not** all render the same string.
The distinction is not cosmetic: it is what decides whether adding an API origin in one place is
enough.

| Artifact | Kind | Renders from | Differs how |
|----------|------|--------------|-------------|
| API responses | header producer | `backend/app/core/security_headers.py` | Takes **no API-origin input, and needs none** — this producer serves the API's own origin, where `'self'` already is the API. It renders from `csp_report_only` alone. |
| Frontend container | header producer | `infrastructure/docker/nginx.conf`, substituting `CSP_HEADER_NAME` and `CSP_CONNECT_SRC_API` | The module's policy **extended** by the configured API origin appended to `connect-src`. |
| Load-balancer edge | header producer | the `security_response_headers` local in `infrastructure/terraform/main.tf` | The same extension, rendered from `var.api_origin`. This is the authoritative producer for the live static path, because the compiled bundle is published to Cloud Storage rather than served by the container. |
| Compiled document | **not a header producer** | `frontend/public/index.html` | A `<meta>` element carrying a strict *subset* of the loading directives and naming neither `connect-src`, `default-src` nor `frame-ancestors`, so it governs loading only and cannot constrain the API. |

With no separate API origin configured — one edge origin serving both the application and the
API — all three header producers emit an identical policy. With one configured, the two static
producers emit the longer `connect-src` and the API producer does not, which is correct rather
than drift.

**Three consequences worth knowing before changing any of them.** A `<meta>` element cannot carry
`Content-Security-Policy-Report-Only`, so the document's policy is *always enforced* and
`csp_report_only` does not reach it. `frame-ancestors` is ignored in a `<meta>` element entirely,
which is why `X-Frame-Options` is emitted alongside it. And a browser applies every policy it
receives, so the effective policy is the **intersection** of those delivered — widening one
artifact alone does not widen the result.

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

`db_sslmode` was listed here as a switch. It is not one any more: it has exactly one legal value,
`require`, so there is nothing to switch it to. It is still read at engine construction, so a future
second value would need a process restart. Changing the database transport now means changing the
connection topology — see [Residual risks](#residual-risks) item 3.

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
The write budget is shared by every mutating request one client makes, so `rate_limit_write` is an
aggregate rather than one route's rate. `300/minute` is set from what the client can produce: it
coalesces cell writes into at most one request per worksheet per 1000 ms, which is 60 a minute per
edited worksheet, so 300 clears five worksheets edited at once plus the workbook-creation and share
routes. Shortening that client window without raising this value would throttle ordinary editing.

**`auth_enforcement_enabled=false` is break-glass only and must never be set in production.**
Its precise effect: it skips *signature, expiry and revocation verification only*. A request
must still carry an `Authorization: Bearer <token>` header — the OAuth2 scheme refuses it with
`401` before this code runs, whatever this switch is set to — the token must still parse well
enough for its claims to be read, and those claims must still name a user that exists in the local
database. So endpoints are **not** reachable without a token; they are reachable with an
*unverified* one, which anyone able to mint arbitrary claims for a known user's email address can
produce. Every admitted bypass is logged at warning level and its response carries
`X-Auth-Enforcement-Bypassed: true`.

Two response facts follow from that, and are worth knowing before writing a client:

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
2. **`sslmode=require` does not verify server identity, and it is the only mode this topology can
   deploy.** It defeats passive interception but not an active machine-in-the-middle. The two
   stronger modes are *accepted* by `Settings` and are **not deployable** here: the pods reach
   Cloud SQL through the Auth Proxy, whose local listener carries no certificate for the
   instance's name, so `verify-ca` and `verify-full` fail at start-up — `scripts/deploy.sh`
   refuses manifests that set either. Encryption is not what is lost: the proxy's own leg to the
   instance is mutually authenticated, which is what satisfies `ENCRYPTED_ONLY`. Widening this
   means changing the connection topology — a private IP with the CA distributed to the client,
   or the Cloud SQL Python Connector in place of the proxy.
3. **The application cannot currently start, so nothing above is enforced in a deployment.**
   `backend/app/main.py` imports an `init_db` that is not defined; the five route modules import
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
   authentication path.
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
10. **The static assets bucket is directly reachable.** It grants read to `allUsers` so the load
    balancer can serve it, which also leaves every published object readable at
    `https://storage.googleapis.com/<bucket>/<object>`. That path bypasses the load balancer, so
    it carries none of the response headers above and is not redirected to HTTPS. Nothing secret
    may be published there. The uploads bucket is the opposite and deliberately so: public access
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
    errors.
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

**529 tests, all passing** as measured by the command above. They assert the `401` on all five routes, rejection of a forged token,
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

## Related documents

- [Security Decision Log](./documentation/Security%20Decision%20Log.md) — why each control was
  implemented the way it was, with alternatives and risks
- [Security Traceability Matrix](./documentation/Security%20Traceability%20Matrix.md) — each
  vulnerability mapped to its implementation and verification, in both directions
- [Developer Onboarding](./documentation/Developer%20Onboarding.md) — setup, pitfalls, and the
  prioritised list of next tasks
- [`.env.example`](./.env.example) — every configuration key and its accepted values
