# Security Traceability Matrix

Bidirectional traceability for the security remediation.

- **Forward**: every vulnerability maps to the artifacts that implement its fix, the control
  introduced, and a *named* verification.
- **Reverse**: every changed artifact maps back to the vulnerability or rule that justifies it.

Nothing is traced by description alone: each verification names a real test function, a real
command, or a documented procedure.

**Read the verification column precisely.** Traceability being complete does not mean every control
has been observed working in a deployed process, and this document previously blurred the two. Each
verification is labelled with what it actually exercises:

- **executed** — the control runs and the test observes its behaviour;
- **source shape** — the test reads a file and asserts what it declares, which proves the control is
  present and unweakened but not that it runs;
- **deployment** — a command an operator runs against live infrastructure, which nothing in this
  repository can substitute for.

One limit applies to every row and is stated once here rather than repeated: `backend/app/main.py`
cannot be imported, because it imports an `init_db` that does not exist and the route modules import
absent service classes. Every "executed" claim below is therefore executed against the real module
under test — the real `get_current_user`, the real engine builder, the real mapper — assembled into
a probe application, never against the application entry point. That gap is residual 26 in the
[Security Decision Log](./Security%20Decision%20Log.md) and follow-up F27, and
`TestKnownResiduals::test_the_application_entry_point_cannot_be_imported` pins it so it cannot be
quietly closed or quietly forgotten.

## Baseline and scope

| | |
|---|---|
| Baseline commit | `45a6b7b` — the state before any security work |
| Vulnerabilities | V1–V12, plus one condition (**A06**) that is not a single defect: the absence of a pinned dependency manifest |
| Added controls | **H1–H10** — controls that no listed vulnerability required, added inside authorised files while closing them. Traced separately below rather than folded into V1–V12, and recorded as deviation DEV-14; H9 and H10 answer QA testing findings and are recorded as D104, D105 and DEV-18 |
| Changed artifacts | **47** = 30 planned + 2 committed verification artifacts + 1 secret-exposure control + 2 reference-only modules the ORM seam required + 9 frontend request-seam artifacts + 2 backend modules the runtime-profiling round added + 1 container start-up guard. The split is stated rather than presenting 47 as though all of it had been planned, and every unplanned row carries a numbered deviation entry in the [Security Decision Log](./Security%20Decision%20Log.md) |
| Verified how | `terraform fmt -check` **and** `terraform validate` on `main.tf` + `variables.tf` — both clean; validate is run against those two files copied into an empty directory, because the real directory still carries the pre-existing `outputs.tf` defect. Plus `bash -n scripts/deploy.sh`, `shellcheck --severity=warning` on an LF copy of it, a YAML parse of `ci.yml`, `pyflakes` on every changed module, the `pytest` suite below, and the Firestore emulator check. Every one of these now runs in CI except the emulator check |
| Test suite | `backend/tests/test_security.py` — **786** cases, all passing, alongside `backend/tests/firestore_rules_emulator_check.py` for V10 and `frontend/src/services/api.test.ts` (93 cases) for the V3 client half and H8; the counts grow as tests are added, so treat "all pass" rather than the number as the criterion |
| Known collection errors | Exactly three, unchanged from the baseline: `backend/tests/test_api.py`, `test_calculation_engine.py` and `test_collaboration.py` cannot be collected. They are pre-existing, left untouched deliberately, and any *fourth* error is a regression |

### How to re-verify this matrix mechanically

```bash
git diff 45a6b7b --name-status
```

Compare that path set against the [reverse table](#reverse-every-changed-artifact-to-its-justification).
The two must match exactly:

- a path in the diff but **absent** from the reverse table is an unjustified change — either
  revert it or record a deviation for it in the
  [Security Decision Log](./Security%20Decision%20Log.md);
- a path in the table but **absent** from the diff is an unimplemented row.

Then run the suite:

```bash
PYTHONPATH=. venv/bin/python -m pytest backend/tests/test_security.py -q
```

Expect every test to pass — **786** cases. The classes named below are the test classes in that
module, so `-k <ClassName>` selects one.

The client half runs separately, and its runner is a known blocked prerequisite rather than a
declared dependency (see the README's blockers):

```bash
npm --prefix frontend install --no-save --no-audit --no-fund \
  react-scripts@5.0.1 @reduxjs/toolkit@1.9.7 react-router-dom@6.30.1 \
  firebase@10.14.1 mathjs@11.12.0 date-fns@2.30.0 @types/node@18.19.86
cd frontend && CI=true npx react-scripts test --watchAll=false --testPathPattern api.test
```

Expect all 93 cases to pass.

---

## Forward: every vulnerability to its implementation and verification

| ID | Vulnerability | Implementing artifacts | Control introduced | Verification |
|----|--------------|------------------------|--------------------|--------------|
| **V1** | No route required authentication. `get_current_user` was defined and referenced nowhere; all five handlers declared `Depends(get_db)` alone. | `backend/app/api/workbooks.py`, `worksheets.py`, `cells.py`, `collaboration.py` | `Depends(get_current_user)` on all five handlers, so a request without a valid token is refused before the handler body runs. `auth_enforcement_enabled` — true by default and the mandatory production setting — does not decide *whether* a credential is required: `oauth2_scheme` carries its default `auto_error`, so a request carrying no `Authorization` header is answered `401` with a `WWW-Authenticate: Bearer` challenge before any application code runs, whatever the switch is set to, and an empty `Bearer` value is refused by `_resolve_current_user` for the same reason. What the switch relaxes is verification of a token that *is* presented: with it false, signature, expiry, revocation and account state are skipped and the caller is admitted on unverified claims that must still name a row this database holds, so no admitted request is anonymous, and every admitted bypass is logged at warning level and carries `X-Auth-Enforcement-Bypassed: true`. An earlier version of this row stated that the switch admitted a credential-less request on an identity-less placeholder caller; that claim was retracted, the module carries no such caller, and `SECURITY.md` records the correction. The switch is documented as break-glass-only in `.env.example`, `README.md` and `SECURITY.md`. | `TestRouteAuthenticationDependency` (16 tests — the dependency is attached to all five handlers and stays out of the request contract) and `TestUnauthenticatedRequestsAreRefused` (5 tests — `401` with `WWW-Authenticate: Bearer`, asserted against the real dependency). The switch's own behaviour is asserted by `TestAuthenticationEnforcementSwitch` (13 tests), including that a credential-less request is refused whatever the switch says, that no placeholder caller can be admitted, and that every admission is logged and marked on the response |
| **V2** | `security.py` annotated `Optional[timedelta]` without importing `typing`, so the module raised `NameError` at import — blocking every other authentication control. | `backend/app/core/security.py` | `from typing import Optional` | `TestSecurityModuleImportability` (9 tests — the module imports and `Optional` resolves) |
| **V3** | Two unbridged identity systems: the client signed in with Firebase and never sent a token; the server decoded a custom JWT nothing issued. | `backend/app/core/security.py`, `backend/app/core/config.py`, `backend/requirements.txt`, `frontend/src/services/api.ts` | Firebase ID-token verification against a pinned project, local `User` resolved by the verified `email` claim; an axios request interceptor attaches the token client-side | `TestTokenVerificationOutcomes` (23 tests — a forged, expired or revoked token is refused without reaching a handler, a provider or credential fault is separated from a caller fault, and no cause is echoed to the caller), `TestIdentityClaimValidation` (29 tests) and `TestAuthenticationEnforcementSwitch` (13 tests). Client half: `frontend/src/services/api.test.ts` |
| **V4** | `blob.make_public()` made every uploaded workbook world-readable, and `blob.public_url` was returned to the caller. | `backend/app/services/file_storage.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf` | Expiring V4 signed URLs, no ACL write; uniform bucket-level access and `public_access_prevention = "enforced"` on the uploads bucket; a dedicated signer identity | `TestObjectStorageAccess` (6 tests — no ACL is set and the returned URL is a bounded-expiry signed URL), `TestStorageLogSanitisation` (3 tests) and the expiry bounds in `TestConfigurationContract`. Deployed: `curl -I <object URL>` → `401`/`403`; `gcloud storage buckets describe` |
| **V5** | `allow_methods=["*"]` and `allow_headers=["*"]` with credentials enabled, against an `ALLOWED_ORIGINS` field that did not exist on `Settings`. | `backend/app/core/config.py`, `backend/app/main.py`, `infrastructure/terraform/variables.tf` | `ALLOWED_ORIGINS` declared and validated; methods narrowed to `GET, POST, PUT, OPTIONS` and headers to `Authorization, Content-Type`; one origin grammar shared by the backend and Terraform | `TestCrossOriginPolicy` (5 tests — a disallowed origin's preflight is refused, an arbitrary requested header is not reflected) and the origin grammar in `TestConfigurationContract` (155 tests), which refuses a wildcard origin at construction, asserts that the backend's three compiled expressions appear character-for-character in `variables.tf`, and runs 23 origin vectors past both planes requiring the same verdict from each |
| **V6** | `create_engine` received no `connect_args`, and Cloud SQL sat on its default SSL mode, which permits unencrypted connections. | `backend/app/db/database.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf` | Client passes `sslmode`. `db_sslmode` is `Literal["disable", "require"]` — two legal values and no others: `allow` and `prefer` are refused because each negotiates plaintext silently whenever the server offers it, and `verify-ca` and `verify-full` are refused because the only route to the instance is the Cloud SQL Auth Proxy, whose loopback listener carries no certificate for the instance's name. `disable` is not freely selectable — `Settings` refuses it unless the resolved `DATABASE_URL` names a loopback host, one of `127.0.0.1`, `localhost`, `::1` or `[::1]`; a socket-based URL is refused a step earlier, because the URL grammar requires a host — so exactly one of the two is deployable on any given topology: `disable` on GKE behind the proxy, which is what the pod manifests set and what `deploy.sh` asserts, and `require` on a direct connection. Cloud SQL is set to `ENCRYPTED_ONLY`, so the server refuses cleartext, and the proxy's own mutually authenticated leg to the instance is what satisfies it. This contract is stated in one form in `.env.example`, `SECURITY.md`, the onboarding guide's database-contract section and `scripts/deploy.sh`'s failure messages (D7, D54, D91) | `TestDatabaseTransportSecurity` (5 tests — the engine is built with the configured `sslmode`) and the mode validation in `TestConfigurationContract`, which refuses any mode that can negotiate plaintext and refuses `disable` for any endpoint that is not local. Deployed: `gcloud sql instances describe`, then `SHOW ssl;` in a live session |
| **V7** | No security header anywhere — no middleware, no meta tag, no Nginx configuration. | `backend/app/core/security_headers.py`, `backend/app/main.py`, `frontend/public/index.html`, `infrastructure/docker/nginx.conf`, `infrastructure/docker/Dockerfile.frontend`, `infrastructure/terraform/main.tf` | Six header names on every response. **Three header producers**, agreeing byte-for-byte on the five fixed values; their Content-Security-Policies deliberately differ, because the two static producers append the configured API origin to `connect-src` while the API producer takes no such input and needs none — on its own origin `'self'` already is the API. A fourth artifact, the compiled document's `<meta>` element, is not a header producer: it carries the loading directives only, cannot narrow the served policy, and diverges from it in exactly one directive — `style-src-elem` also admits `'unsafe-inline'` there, for the header-less development server (D99). The header middleware is registered outermost so errors and preflights are covered | `TestSecurityResponseHeaders` (8 tests — the canonical set on a success, a `401`, a `429` and a CORS preflight, and on an unhandled `500` answered from inside the stack) and `TestStaticDeliveryPolicies` (25 tests — the container policy is the API policy plus the substituted origin byte for byte, the API middleware takes no API-origin input, the document policy names neither `connect-src` nor `default-src` so it cannot block the configured API, it never narrows the served policy and diverges from it in exactly one directive by exactly one source while all three header producers keep `style-src-elem 'self'`, and the document carries no script element of its own and no repository path) |
| **V8** | `deploy.sh` created an HTTP target proxy and a port-80 forwarding rule; Terraform declared no edge at all. | `infrastructure/terraform/main.tf`, `infrastructure/terraform/variables.tf`, `scripts/deploy.sh` | Managed SSL certificate, HTTPS target proxy, 443 forwarding rule, and an HTTP→HTTPS redirect; the script can no longer create a plaintext listener | `TestDeploymentSurface` (3 tests — Terraform declares the certificate, the HTTPS proxy and the redirect, and `deploy.sh` creates no port-80 listener). Deployed: `curl -sI http://DOMAIN/` → `301`; `openssl s_client -connect DOMAIN:443` |
| **V9** | No rate limiting existed anywhere — no middleware, no library, no quota. | `backend/app/core/rate_limit.py`, `backend/app/main.py`, `backend/requirements.txt` | An **application-wide** per-client ceiling counted against one bucket for the whole application — every URL, including paths matching no route — plus a tighter write-method budget in its own bucket, returning `429` with `Retry-After`. Windows are counted in the Redis store derived from `REDIS_URL`, so a quota does not multiply per worker; a store failure keeps both tiers enforcing on process-local counters and re-counts the triggering request rather than admitting it | `TestRequestThrottling` (14 tests — `429` with `Retry-After`, reads unmetered by the write tier, and the ceiling proven **application-wide** by exhausting it across different endpoints, across different path-parameter values, and against a path that matches no route at all), `TestThrottlingStorageDegradation` (6 tests — a store failure at each of the two tiers keeps enforcing, logs once, and re-counts the failing request), `TestThrottlingIdentity` (9 tests — metering is on the socket peer and no forwarded header is consulted) and `TestMiddlewareRegistrationOrder` (9 tests) |
| **V10** | No Firestore rules were committed, while the browser subscribes to Firestore directly — so no server-side control could compensate. | `firestore.rules`, `firebase.json` | Document-level `read`/`create`/`update`/`delete` rules on `/workbooks/{workbookId}` keyed on the immutable Firebase UID; unmatched paths denied by the platform default | `TestFirestoreRules` (3 tests — the committed rules deny by default and gate on owner or collaborator identity). Authorization semantics are asserted against the emulator by `backend/tests/firestore_rules_emulator_check.py`, run with `firebase emulators:exec --only firestore --project demo-excel-clone "python backend/tests/firestore_rules_emulator_check.py"`, which needs the Firebase CLI and a JDK or JRE at version 21 or above. It passes when the process exits 0 and prints `ALL n ASSERTIONS PASSED`. `firebase.json` makes the pre-existing `deploy.sh` rules hook functional |
| **V11** | The Cloud Function was deployed with `--allow-unauthenticated`, and no invoker binding existed in Terraform. | `scripts/deploy.sh`, `infrastructure/terraform/main.tf` | The flag is gone; any `allUsers` invoker binding is revoked and the revocation confirmed, with both policy reads failing closed; Terraform binds the invoker role to a named service account | `TestDeploymentSurface` — `deploy.sh` carries no `--allow-unauthenticated` and reads the invoker policy fail-closed, and Terraform binds the invoker role to a named account. Deployed: unauthenticated `curl` → `403`; a call with `gcloud auth print-identity-token` succeeds |
| **V12** | Token lifetime was hardcoded to 15 minutes; `ACCESS_TOKEN_EXPIRE_MINUTES` was declared and never read. A later runtime check found the other half of the same objective open: the `legacy_jwt` verifier admitted a token carrying no `exp` at all, for ever (D97). | `backend/app/core/security.py` | Lifetime from an explicit `expires_delta` when given, otherwise from the setting; and an `exp` claim required at the decode, so a credential with no expiry is refused | `TestAccessTokenLifetime` (3 cases — the default comes from the setting and an explicit `expires_delta` still wins) and `TestTokenVerificationOutcomes` (3 further cases — a token with no `exp` is refused with the `Bearer` challenge, one that does expire is still admitted, and the requirement is expressed at the decode rather than after it) |
| **A06** | No dependency manifest of any kind existed for the backend, so every build re-resolved versions with no record — CWE-1104 / OWASP A06:2021. The backend image could not build at all. | `backend/requirements.txt`, `.github/workflows/ci.yml` | All 88 versions exact-pinned (22 direct + 66 transitive), holding `python-jose` above the CVE-2024-33663 fix boundary and declaring nothing no module imports; a CI audit job that fails on any advisory outside a recorded baseline | `python -m pip_audit -r backend/requirements.txt --progress-spinner off` with the baseline's `--ignore-vuln` flags → exit 0, "14 ignored". Omitting any baseline entry exits 1, which is what proves the gate detects a new advisory rather than passing unconditionally |

**Forward coverage: 13 of 13.** No vulnerability lacks an implementation, and none lacks a
verification.

---

## Forward: controls added beyond the twelve vulnerabilities

None of these was required by a listed vulnerability. Each was added inside an already-authorised
file while closing one, which is why the file transformation map does not enumerate it and why the
set is recorded as deviation **DEV-14**. They are traced here rather than folded into the table
above, so the arithmetic of the original scope stays legible.

| ID | Control added | Implementing artifacts | Decision | Verification |
|----|---------------|------------------------|----------|--------------|
| **H1** | Request bodies over 10 MiB are refused with `413`, by declared length before any read and by streamed byte count otherwise. Request-count throttling bounds how often a client calls, not how much one call may demand — so this ceiling has **no switch**, and `rate_limit_enabled=false` does not withdraw it. | `backend/app/core/rate_limit.py` | D35, D90 | `TestRequestThrottling` — the limiter is registered inside the throttling tiers and independently of the throttling switch, and an over-size body is refused with `413` while a body within the maximum is served |
| **H2** | Object reads and writes are bounded at 50 MiB, and `FileStorageService` releases its Cloud Storage client through `close()` or a `with` block. A download checks the declared size before transferring anything. | `backend/app/services/file_storage.py` | D36 | `TestObjectStorageAccess` — an over-size upload and an over-size download are both refused, and an object whose size is unknown is refused rather than assumed |
| **H3** | Object names reach the log only as a `sha256:` prefix, so a caller-supplied name cannot publish user text or forge log lines. | `backend/app/services/file_storage.py` | D37 | `TestStorageLogSanitisation` (3 tests — the name is absent from every record and the digest is stable) |
| **H4** | Every database session carries a 10-second connect timeout and a 30-second server-side statement timeout. | `backend/app/db/database.py` | D46 | `TestDatabaseTransportSecurity` — the engine is constructed with both bounds in `connect_args`, alongside the configured `sslmode` |
| **H5** | An unhandled exception is answered with a `500` produced **inside** the middleware stack, so it carries the security headers, the CORS headers and the bypass marker. | `backend/app/core/security_headers.py`, `backend/app/main.py` | D47 | `TestSecurityResponseHeaders::test_an_unhandled_exception_becomes_a_headed_500` and `TestMiddlewareRegistrationOrder` (the boundary is innermost) |
| **H6** | Throttling disabled by configuration announces itself: a warning naming the consequence at registration, stating that the request-body ceiling is unaffected, and `app.state.rate_limit_enabled`. | `backend/app/core/rate_limit.py` | D45, D90 | `TestRequestThrottling` — with the switch off no tier, handler or limiter is installed, the request-body limiter still is, and the state is recorded |
| **H7** | `SECURITY.md`'s published header values, both policy header names, the full CSP directive block and the two prose counts are pinned to the code that emits them. | `SECURITY.md`, `backend/tests/test_security.py` | D33 | `TestPublishedSecurityDocumentation` (5 tests — a header value, header name, policy or count changed in `backend/app/core/security_headers.py` without the document fails here) |
| **H8** | Client cell writes are coalesced per worksheet over a 1000 ms window and sent as one request; a failing batch rejects every caller in it. The window and `rate_limit_write` were raised together, because at 500 ms against a 120/minute budget one continuously edited worksheet could spend the whole shared budget. | `frontend/src/services/api.ts` | D44, R41 | `frontend/src/services/api.test.ts` — "Cell writes are coalesced per worksheet" (3 cases: two edits become one PUT carrying both cells in queue order, two worksheets are never merged, and a failing batch rejects every caller) plus the one-cell shape in "Request and response shapes match the backend contract" |

| **H9** | The throttling identity is the *transport* peer. A peer the server has replaced with an address from a forwarded header is metered against one fixed shared bucket, so rotating that header moves a caller between no buckets, and a peer that is not an IP address is metered against the shared unknown-client budget rather than becoming a bucket of its own. | `backend/app/core/rate_limit.py` | D104 | `TestThrottlingIdentity` — seven rewritten-peer shapes collapsing to one bucket, eight rotated addresses yielding one key, four non-address peers metered as unknown, IPv6 spellings normalised to one bucket, and two behavioural tests: rotation driven through uvicorn's own `ProxyHeadersMiddleware` reaching `429`, and two genuine peers keeping separate budgets |
| **H10** | Every security record carries an ISO timestamp, its level and its logger name, informational records are emitted rather than dropped, and control characters in a message are escaped so a value quoted from a request cannot forge a second entry. | `backend/app/core/logging_config.py`, `backend/app/main.py` | D105 | `TestLoggingConfiguration` — the level, timestamp and logger name on a record, an `INFO` record surviving, a newline-bearing value confined to one line, a traceback keeping its newlines, idempotent configuration, the access logger filtered, and the entry point calling it before anything registers |

**Added-control coverage: 10 of 10.** Each names the decision that authorised it and a verification
that fails if it is removed.

### Why two controls are not exercised in-process

Every row above names an in-process test, but for V10 and V11 that test asserts the *committed
artifact* rather than exercising the control, because the control does not run in this process.
Substituting a stronger-sounding in-process check would misrepresent them:

- **V10** is enforced by Google's rules engine. `TestFirestoreRules` asserts the committed file
  denies by default and gates on the Firebase UID; it cannot evaluate the rules. A Python test
  that tried to would only re-implement them and assert against its own re-implementation. The
  emulator evaluates the real file, which is why
  `backend/tests/firestore_rules_emulator_check.py` is the verification of record for the
  authorization semantics. A committed JavaScript rules-test harness was considered and rejected
  because it would add a frontend dependency.
- **V11** is an IAM binding on a deployed resource. `TestDeploymentSurface` asserts what *is*
  observable in-process — that the script carries no `--allow-unauthenticated`, revokes any
  `allUsers` binding, fails closed when a policy cannot be read, and that Terraform binds the
  invoker role to a named account. Whether the deployed function refuses an anonymous caller only
  a deployed call can show.
### Why four rows are asserted from configuration rather than executed

V8, V10 and V11 are infrastructure, and V7's two static tiers are build artifacts. For each, the
committed configuration is asserted and the behaviour is a deployment check, because substituting a
weaker in-process check would misrepresent what had been proven:

- **V8** is a load-balancer listener. A test can assert that Terraform declares the certificate, the
  HTTPS proxy and the redirect, and that `deploy.sh` creates no port-80 listener; only `curl` and
  `openssl s_client` against the deployed domain show TLS terminating.
- **V10** is enforced by Google's rules engine. A Python test could only re-implement the rules
  and assert against its own re-implementation. The emulator evaluates the real file. A committed
  JavaScript rules-test harness was considered and rejected because it would add a frontend
  dependency.
- **V11** is an IAM binding on a deployed resource. What *is* testable in-process is the
  deployment script's behaviour — that it fails closed when the policy cannot be read — and that
  is covered.
- **V7** is executed on the API tier, where the middleware runs inside a probe application, and
  asserted as source shape on the compiled document and the Nginx template. Those two are rendered
  at build and start-up respectively, so what a test can check is that neither narrows the policy
  the API emits.

The same distinction applies to the runtime-identity work: `TestRuntimeIdentityAndDatabaseContract`
reads `main.tf`, `variables.tf` and `deploy.sh` and asserts what they declare. It proves the
Workload Identity pair, the IAM bindings and the preflight logic are present and mutually
consistent; it cannot prove an apply happened. `terraform init` cannot run in this repository, so
`terraform fmt -check` and that reading are the whole of the available static verification.

---

## Reverse: every changed artifact to its justification

Compare this table against `git diff 45a6b7b --name-status`.

| # | Artifact | Mode | Justified by |
|---|----------|------|--------------|
| 1 | `backend/app/core/security.py` | UPDATE | V2, V3, V12. Plus the settled bearer contract: `OAuth2PasswordBearer` carries its default refusal, so a request with no credential is refused before any application code runs and no configuration value admits one (D39, superseded in place); a fault that could not check a credential answers the frozen 401 rather than a `503` (D38, superseded in place); and the legacy `sub` claim must already be the canonical integer `User.id` holds, so a non-integral claim can no longer truncate onto another user's id (R30); and that decode now requires an `exp` claim, so a locally-minted token with no expiry is refused instead of being admitted for ever (D97) |
| 2 | `backend/app/core/config.py` | UPDATE | V3, V4, V5, V6, V7, plus the rollback switches. `DATABASE_URL` is validated as a synchronous psycopg2 PostgreSQL URL both as an environment value and where the Secret Manager payload overwrites it, and `db_sslmode` is narrowed to the two modes this topology can complete — `require` for a direct connection, and `disable` only when the resolved URL names a loopback endpoint, which is exactly what the Cloud SQL Auth Proxy presents (R23, D54) |
| 3 | `backend/app/main.py` | UPDATE | V5, V7, V9; plus the runtime-profiling round: the cross-origin policy moved inside the throttling tiers so a preflight is counted (D78), `Retry-After` added to the exposed headers (D79), the logging bootstrap called before any registration (D80), and response compression registered inside CORS and outside the marker (D86) |
| 4 | `backend/app/db/database.py` | UPDATE | V6; explicit pool sizing with a shortened checkout timeout (D81), `pool_pre_ping` and a finite `pool_recycle` (D83), and the published `DB_POOL_CAPACITY` the request-concurrency bound derives from (D82); the engine refuses to build unless the resolved URL selects the psycopg2 dialect its `connect_args` target, and it reads settings exactly once at import (R24). H4 adds the connect and statement timeouts (D46) |
| 5 | `backend/app/services/file_storage.py` | UPDATE | V4; H2's object-size bounds and client lifecycle (D36) and H3's object-name log digest (D37) |
| 6 | `backend/app/api/workbooks.py` | UPDATE | V1; plus bounded page parameters (D84) under deviation DEV-16 |
| 7 | `backend/app/api/worksheets.py` | UPDATE | V1; plus a bounded page window passed through to the service call, where the route previously accepted no page parameter at all (D84), under deviation DEV-16 |
| 8 | `backend/app/api/cells.py` | UPDATE | V1; plus the fixed failure message, the server-side record of the cause, and the deliberate-refusal re-raise (D85), under deviation DEV-16. Plus the contract a reader of this route must not get wrong, stated at the site and pinned by a test rather than left to a reviewer: `worksheet_id` is documented as arriving as the worksheet's NAME, to be resolved by `(workbook_id, name)` rather than by primary key (D98, residual 23, F26) |
| 9 | `backend/app/api/collaboration.py` | UPDATE | V1; the same three changes as row 8 at its own unchanged `400` (D85), under deviation DEV-16 |
| 10 | `backend/app/core/rate_limit.py` | CREATE | V9; the request-concurrency bound derived from the database pool capacity (D82) and the cross-origin headers on the tiers' own refusals (D79); H1's request-body ceiling (D35) and H6's disabled-throttling announcement (D45). The ceiling tier is a `limits` fixed window counted against one application-wide scope rather than `slowapi`, whose `default_limits` were counted per endpoint function (R32), and a window-store failure keeps both tiers enforcing on process-local counters and re-counts the triggering request (R33) |
| 11 | `backend/app/core/security_headers.py` | CREATE | V7; H5's headed `500` produced inside the middleware stack (D47) |
| 12 | `backend/requirements.txt` | CREATE | V3, V6, V9, A06. 88 exact pins, 22 direct. `slowapi` — the package AAP §0.7.1 names for V9 — is deliberately **not** pinned: R32 replaced its tier with `limits`, so nothing imports it, and a pin with no importer ships its transitive surface for no control. That is a logged deviation in the mechanism, not the outcome; V9's ceiling is delivered by `limits`, which is pinned and imported (R32, D68; D52 superseded) |
| 13 | `backend/tests/conftest.py` | CREATE | Verification prerequisite — it populates the required settings and stubs the Secret Manager client, without which no test can import the modules under test. Its `authentication_database` and `failing_authentication_database` fixtures patch the module-level `get_db` name in `backend.app.core.security`, because the identity lookup imports that name rather than declaring it as a FastAPI dependency, so `app.dependency_overrides[get_db]` cannot reach it (R22, DEV-3) |
| 14 | `backend/tests/test_security.py` | CREATE | Verification for V1–V7, V9, V12, A06, H1–H7 and the infrastructure contracts, plus the database seam the seam review added: `TestOrmSeam`, `TestAuthenticatedIdentityResolution`, `TestDatabaseDriverContract` and `TestRuntimeIdentityAndDatabaseContract`. Deviation DEV-3 |
| 15 | `frontend/src/services/api.ts` | UPDATE | V3 (client half): a dedicated Axios instance, the wait for the restored Firebase session, credential redaction on a failing request and ejection on reload (D49); plus H8's per-worksheet write coalescing, whose window and matching write budget were raised together (D44, R41) |
| 16 | `frontend/public/index.html` | UPDATE | V7 — the compiled document's `<meta>` policy, confined to loading directives because a meta element has no report-only form, so it cannot narrow the served policy (D31, D62). Plus three changes made where the runtime QA checkpoint observed them: `style-src-elem` additionally admits `'unsafe-inline'` **here only**, so the header-less development server does not refuse the application's own injected stylesheet while both delivered paths still enforce `'self'` (D99); the comment is reduced to a one-line threat marker that discloses no repository path (D100); and the hardcoded `<script src="%PUBLIC_URL%/bundle.js">` no build emits is deleted (D101) |
| 17 | `infrastructure/docker/nginx.conf` | CREATE | V7, V8 |
| 18 | `infrastructure/docker/Dockerfile.frontend` | UPDATE | V7 — activates the Nginx configuration, without which it is dead code. Deviation DEV-2. Plus the container path's counterpart to the edge's `api_origin` validation: `/docker-entrypoint.d/05-require-csp-connect-src-api.sh`, written into the image so a CRLF working tree cannot break it, refuses to start on an empty or unspaced `CSP_CONNECT_SRC_API` rather than serving a policy that admits no API origin (D102) |
| 19 | `infrastructure/terraform/main.tf` | UPDATE | V4, V6, V7, V8, V11. Plus the runtime-identity and database constructs the seam review required: `google_container_cluster.primary` carries `workload_identity_config.workload_pool` and `google_service_account_iam_member` grants the Kubernetes principal `roles/iam.workloadIdentityUser` on the one account, which is what makes the node pool's `GKE_METADATA` mode resolve to a Google identity at all (R36, superseding R8 and D57); `google_project_iam_member.api_runtime_firebaseauth_viewer` supplies the `firebaseauth.users.get` permission `check_revoked=True` needs on every verification (R37, D58); the Cloud Function runs as its own unprivileged `function_runtime` account with the invoker role bound authoritatively rather than additively (D56); the port-80 redirect listener is unconditional and ordered after the 443 rule (D43, R38); and the database login is documented as an operator-provisioned prerequisite this configuration deliberately does not manage, because managing it would persist its password in Terraform state (R39) |
| 20 | `infrastructure/terraform/variables.tf` | UPDATE | V4, V5, V8. Plus `db_name` given the canonical `main-database` default with a validated grammar (D27), `signer_service_account` re-described for the delivered one-account topology and `runtime_service_account` deleted with it (R36), `db_user`, `db_password` and `https_cutover_enabled` deleted (R39, R38), `var.storage_class` wired on both buckets rather than labelled unread (D60), both `kubernetes_*` descriptions corrected, and the four genuinely unread inputs labelled `NOT REFERENCED` **and** defaulted, so none of them is a required input that does nothing (D27, R39) |
| 21 | `scripts/deploy.sh` | UPDATE | V8, V11. Plus the preflight the seam review required: the runtime identity collapsed onto Terraform's one `signer_service_account`, with the cluster workload pool and the live Workload Identity binding read back before deploying (R36); the invoker policy asserted in the read-only preflight, ahead of the first mutation, instead of revoked after publication (D56); the Cloud SQL Auth Proxy check split so either half missing aborts, the instance connection name matched inside a container argument, the port-80 listener's target verified and the authorization rules deployed before any code (R50); a live TLS handshake against the API origin rather than a reading of its configured text (D65); the `DATABASE_URL` secret's driver, proxy host, database name and login validated without the value being echoed or written (R42); the Cloud Function runtime checked against the list Google publishes at deploy time (D50); and a browser Firestore reference that cannot resolve reported as a warning rather than an abort, because the file it names is outside this change set (D53, R51). Its two `DATABASE_URL` failure messages were also corrected to the transport contract the code enforces, one having offered a socket URL a later gate refuses and the other having instructed the operator to set a `db_sslmode` value `Settings` does not accept (D91) |
| 22 | `firestore.rules` | CREATE | V10 — document-level rules keyed on the immutable Firebase UID, with `delete` owner-only because deletion is irreversible and removes the workbook from every other collaborator too (D51, R45, D64) |
| 23 | `firebase.json` | CREATE | V10 — makes the pre-existing `firebase deploy --only firestore:rules` hook in `scripts/deploy.sh` resolve a rules file |
| 24 | `.env.example` | CREATE | V4, V5, V6 configuration surface; every operational switch with its default; Rule 2 |
| 25 | `.github/workflows/ci.yml` | UPDATE | A06 — the dependency-audit gate, whose fourteen suppressions are justified inline, one per advisory, rather than carried as an opaque baseline list (R52); the Firestore emulator job that turns the committed rules from an asserted artifact into an executed gate (R45); the frontend interceptor step with `continue-on-error` removed and eight packages installed at pinned versions — the six the sources import plus two type packages (R40); the frontend audit reduced to a self-promoting three-state check that becomes a real gate by itself once the manifest is fixed (R53); the step that gates the four database-seam classes, so a regression in the authenticated query fails the build rather than the suite merely still passing; and the scoped `terraform fmt -check` and `validate` step that proves the infrastructure configuration LOADS, which no text assertion can establish (D69) |
| 26 | `README.md` | UPDATE | **Rule 2** |
| 27 | `documentation/Developer Onboarding.md` | CREATE | **Rule 2**; deviation DEV-15 records why a clean machine still cannot reach a running application |
| 28 | `SECURITY.md` | CREATE | **Rule 2**; H7 pins its published header values, both policy header names, the CSP directive block and its two prose counts to the code that emits them (D33) |
| 29 | `documentation/Security Decision Log.md` | CREATE | **Rule 1** |
| 30 | `documentation/Security Traceability Matrix.md` | CREATE | **Rule 1** — this document |
| 31 | `backend/tests/firestore_rules_emulator_check.py` | CREATE | V10 — executable emulator assertions. The browser reaches Firestore directly, so rules are the only control on that path, and this is the verification of record for their semantics. Deviation DEV-3 |
| 32 | `frontend/src/services/api.test.ts` | CREATE | V3 — asserts the client attaches the verified Firebase ID token, refuses rather than sending a request unauthenticated, and redacts the credential from a failing request: the half of the identity bridge no backend test can observe. Also H8's coalescing behaviour. Deviations DEV-3 and DEV-13 |
| 33 | `.gitignore` | CREATE | Secret exposure — `.env`, service-account keys, certificates and Terraform state. This change set adds `.env.example` and instructs a developer to copy it to `.env`, so it creates the hazard it guards. Deviation DEV-4, whose earlier disposition this reverses |
| 34 | `backend/app/db/models.py` | UPDATE | V1 and V3, at the point where they meet the database. One additive line, `workbooks = relationship("Workbook", back_populates="owner")`, completes the pair `Workbook.owner` already declares — without it SQLAlchemy raised `InvalidRequestError` while configuring the mapper, which happens on the first query against `User`, and the first query against `User` **is** the identity lookup. Every successfully authenticated request therefore ended in a logged `500`. No column, no DDL, no migration; the plan designates this module reference-only, so the edit is deviation DEV-6 |
| 35 | `backend/app/schema/workbook_schema.py` | UPDATE | V1's admitted path. `class Config: orm_mode = True` on `WorksheetSchema` is the precondition for the `from_orm` call the frozen `worksheets.py` route already makes; without it the route raised `ConfigError` before reading a field. Field names, types and the serialized shape are unchanged, and asserted so. Deviation DEV-7 |
| 36 | `frontend/src/schema/workbookTypes.ts` | UPDATE | The TypeScript mirrors of the frozen Pydantic DTOs, the `User` interface `userSlice` imported but this module never defined, and the explicit DTO↔domain mapping — R40, DEV-13 |
| 37 | `frontend/src/pages/Workbook.tsx` | UPDATE | Aligns the workbook read and the cell write to the frozen five-route contract; it had called a `fetchWorkbook` export that does not exist and dispatched an action that does not exist — R40, DEV-10. Separates the blocking load failure from a non-destructive dismissible save-failure banner that names the cell and renders alongside the grid, announces both surfaces with `role="alert"`, and clears the signed-in identity when `apiFailure(...).reauthenticate` is set — D103, closing residuals 29 and 30 and follow-ups F33 and F34 |
| 38 | `frontend/src/pages/Settings.tsx` | UPDATE | Removes the API call and the store action that have neither a route nor an export — R40, DEV-10 |
| 39 | `frontend/src/pages/Dashboard.tsx` | UPDATE | Types the collection response as the DTO the client now returns — R40, DEV-10. Surfaces the classified failure to the user in a dismissible `role="alert"` region instead of writing it only to the console, and clears the signed-in identity when `apiFailure(...).reauthenticate` is set — D103, closing residual 29 and follow-up F33 |
| 40 | `frontend/src/services/auth.ts` | UPDATE | Replaces the unresolvable Python-path type import and the double cast through `unknown` with a real projection. The sign-in flow itself is unchanged, because it is a frozen contract — R40, DEV-11 |
| 41 | `frontend/src/services/collaboration.ts` | UPDATE | The browser's Firestore reference repointed from `collection(db, 'workbooks', workbookId)`, which names an even number of path segments and type-checks to a `QuerySnapshot` carrying neither member the function calls, to `doc(db, 'workbooks', workbookId)` — the reference the V10 rules govern. Type import repointed with it; nothing else changed — DEV-8 |
| 42 | `frontend/src/store/userSlice.ts` | UPDATE | Type import only; state shape unchanged — R40, DEV-12 |
| 43 | `frontend/src/store/workbookSlice.ts` | UPDATE | Type import, plus the two conformance fixes the resolved types exposed; state shape unchanged — R40, DEV-12 |
| 44 | `frontend/tsconfig.json` | UPDATE | The `"@/*"` path mapping without which the callers' imports do not resolve — R40, DEV-12 |
| 45 | `backend/app/core/logging_config.py` | CREATE | The application log bootstrap, without which every `INFO` record the application emits is dropped under a bare uvicorn — including the one naming the window store the throttling tiers count in, which is the only runtime signal distinguishing a deployment enforcing the configured quota from one enforcing a multiple of it (D80, residual 19). Also H10’s filterable security records (D105): every record gains its level, an ISO timestamp and its logger name, where before they reached `logging.lastResort` with none of the three. And it carries the control-character escaping that closes the log-forging half of H9’s finding, including on the server’s own loggers, which do not propagate to the root handler |
| 46 | `backend/app/core/pagination.py` | CREATE | The single definition of the page window both list routes apply, so the two cannot drift and the documented maximum has one home (D84) |
| 47 | `infrastructure/docker/csp-env-validate.sh` | CREATE | V7 — refuses a `CSP_HEADER_NAME` or `CSP_CONNECT_SRC_API` value that would remove the container's policy or truncate it, before the template is rendered — DEV-17, D89 |


**Reverse coverage: 47 of 47.** Every path in `git diff 45a6b7b --name-status` is justified
above, and no row names a path absent from it. The 47 split by provenance:

- **rows 1–30** are the planned artifacts, exactly as the file transformation map enumerates them;
- **rows 31–32** are the two committed verification artifacts, which extend the test-file deviation
  already recorded as DEV-3;
- **row 33** is the secret-exposure control, which reverses DEV-4's earlier disposition;
- **rows 34–35** are the two modules the plan designates reference-only, edited one line each
  because the defects they carried had become reachable on the authenticated path (DEV-6, DEV-7);
- **rows 45–46** are the two backend modules the runtime-profiling round added, each carrying a
  control that could not be asserted from `main.py` because `main.py` cannot be imported: the
  logging bootstrap (D80) and the shared page window (D84). Row 45 also carries the filterable,
  unforgeable record format a later round added (D105), and is recorded as a deviation because the
  entry point's own brief excluded logging configuration (DEV-18);
- **rows 36–44** are the frontend request seam, each recorded as a deviation (DEV-8, DEV-10,
  DEV-11, DEV-12, DEV-13);
- **row 47** is the container start-up guard for the two substituted policy variables, recorded
  as DEV-17 and decided in D89.

Re-verify by comparing `git diff 45a6b7b --name-only` against this table: **47 paths, 47 rows.**
A path in the diff and not in the table is a scope breach; a row whose path is not in the diff is
an unimplemented item. `TestOperatorFacingClaims::test_the_traceability_arithmetic_is_self_consistent`
and `test_the_reverse_matrix_matches_the_working_tree` assert both halves, so the table cannot drift
from the tree without failing the suite.

---

## Files deliberately not changed

Recorded because their absence from the diff is a decision, not an oversight. Each is read as
authoritative context, and several are the reason a fix took the shape it did.

| File | Why it is untouched |
|------|--------------------|
| `backend/app/db/models.py` (**columns and tables only**) | The module itself is now row 34 above: one relationship line was added. What remains untouched is the schema proper — no column, table, index or constraint changed. Adding `firebase_uid` still needs migration tooling the repository does not have, which is why V3 keys identity on the verified `email` claim. |
| `backend/app/schema/workbook_schema.py` (**field contract only**) | The module itself is now row 35 above: `orm_mode` was enabled on `WorksheetSchema`. What remains untouched is the response contract — no field added, removed, renamed or retyped, asserted by `TestOrmSeam::test_the_worksheet_schema_field_contract_is_unchanged`. A worksheet holding populated `Cell` rows still cannot be serialised, because the schema declares a map and the ORM holds a list; that projection belongs to the absent `WorksheetService` and is residual 16. |
| `frontend/src/services/collaboration.ts` (**everything but the document reference**) | The module is now row 41 above: `collection(db, 'workbooks', workbookId)` became `doc(db, 'workbooks', workbookId)` and the type import was repointed. Nothing else changed — no subscription shape, no callback contract, no error handling. The browser's Firestore path still returns nothing useful until `real_time_sync.py` writes an `ownerUid`, which is why the fail-closed V10 rules regress nothing measurable. |
| `backend/app/services/real_time_sync.py` | Out of scope. It writes workbook documents with no owner field, which is why the V10 rules currently deny all browser access — the correct fail-closed outcome. |
| `backend/app/tasks/background_jobs.py` | Out of scope. It persists a storage URL, which signed URLs would invalidate; the code path is already non-functional, so nothing working regresses. |
| `infrastructure/terraform/outputs.tf` | Out of scope. Its seven references to undeclared resources are why `terraform validate` fails; `main.tf` and `variables.tf` validate clean. |
| `infrastructure/docker/Dockerfile.backend` | Out of scope. Its `python:3.9-slim` pin is why no dependency advisory is currently fixable. |
| `backend/app/services/calculation_engine.py` | Out of scope, and verified to contain no `eval` or `exec`, so there is no formula-injection vector. |
| `frontend/src/store/index.ts` | Out of scope as Redux store structure. Its `workbookReducer` and `userReducer` named imports do not exist, and it exports no `useAppSelector`/`useAppDispatch`, which six modules import — a pre-existing store blocker, not a request-seam defect. |
| `frontend/src/components/*.tsx` | Out of scope. `@/components` has no barrel and several components dispatch payload shapes the reducers do not accept; both are pre-existing frontend blockers. |
| `frontend/src/utils/formulaParser.ts` | Out of scope — formula parsing. It is the last module still importing a type from the Python schema path, and it stays that way for that reason. |
| `frontend/src/services/auth.ts` (sign-in flow) | The flow is a contract that must not change — the Firebase sign-in is the front door, which is why V3 bridges into `get_current_user` rather than replacing the identity system. The same `signInWithEmailAndPassword`, `signOut`, lazy `getAuth()` and error handling remain; only the unresolvable type import and the double cast changed (row 40). |
| `backend/tests/test_api.py`, `test_calculation_engine.py`, `test_collaboration.py` | Left as-is so the pre-existing three-collection-error baseline stays a valid comparison point. |
| `.github/workflows/cd.yml` | Out of scope. Its workflow-name mismatch with `ci.yml` is why CD does not trigger. |
| `frontend/package.json` | Out of scope. No frontend dependency was added; its undeclared packages are a pre-existing gap. |

---

## Coverage assertion

| | Count | Gaps |
|---|-------|------|
| Forward — vulnerability to implementation and verification | 13 / 13 | none |
| Forward — added control to authorising decision and verification | 10 / 10 | none |
| Reverse — artifact to justifying vulnerability or rule | 47 / 47 | none; the 47 rows match `git diff 45a6b7b --name-status` exactly |
| Vulnerabilities with a named in-process test | 13 / 13 | none |
| Vulnerabilities whose control the in-process test *exercises* | 11 / 13 | **V10** and **V11**. Their controls run in Google's rules engine and in IAM, so the in-process test asserts the committed artifact instead; V10's semantics are exercised by the emulator check and V11's only by a deployed call, as set out above |
| Vulnerabilities whose live cloud behaviour a deployment must confirm | 5 of 13 | **V4, V6, V8, V10, V11**. The committed configuration is asserted here; that the applied infrastructure matches it is a deployment-time check, and each row above names the command |
| Added controls with an automated test | 10 / 10 | none. H8 is verified by the frontend suite, which CI runs after installing the seven packages `frontend/package.json` declares nowhere, at pinned versions (R40) |
| Security review findings, order 1 | 22 raised | 15 resolved; 7 declined against a cited Agent Action Plan exclusion and carried as residuals — see the table below |
| Runtime verification findings, final order | 16 / 16 tracked | none untracked. 11 declined against a cited exclusion, 2 recorded as framework behaviour rather than defects, and 3 re-confirmed as accepted residuals — see [Runtime verification findings, final order](#runtime-verification-findings-final-order) |

**The seven declined findings**, each with the clause that forbids the fix rather than a
judgement that it was not worth making. The `Residual` column indexes the residual register in
the [Security Decision Log](./Security%20Decision%20Log.md#4-accepted-residual-risks),
except where it names `SECURITY.md`, which keeps its own numbering:

| Finding | Why it is not fixed here | Residual |
|---------|--------------------------|----------|
| Per-owner scoping on the routes | §0.9.3 and D13 limit route edits to the authentication dependency; §0.8.3.3 records authenticated cross-tenant access as an accepted residual and the top follow-up | 1 |
| Make the application importable (service layer, `init_db`) | §0.9.2.1 and §0.13.5 exclude the domain service layer and `init_db` outright | 26, F27 |
| Serve static assets from a private origin | D5 — the bucket serves the public SPA, and public access prevention would break it | 10 in SECURITY.md |
| Verify the database server's identity (`verify-full`) | D7, settled by D54 — the proxy presents a plain loopback listener holding no server certificate to verify, so the stronger modes cannot complete at all | 3 |
| Declare `firebase`/`react-scripts` and commit a lockfile | §0.9.1.1 and §0.13.4 item 9 place `frontend/package.json` out of scope | 22 |
| Upgrade the Python runtime off 3.9 | §0.9.2.3 excludes runtime upgrades; §0.13.4 item 1 places `Dockerfile.backend` out of scope | 5 |
| ~~Stop returning `str(e)` from the route bodies~~ | **No longer declined — closed in the runtime-profiling round by `D85`, under deviation `DEV-16`.** Both handlers now answer a fixed message and record the cause server-side | D85, DEV-16 |

Declined is not the same as unexamined. Each is stated as an open item in
[SECURITY.md](../SECURITY.md), and the runtime upgrade is named as the exit criterion of the
dependency-audit exception list in `.github/workflows/ci.yml`, so it cannot be quietly forgotten.

### Runtime verification findings, final order

The table above covers the order-1 security *code* review, which read source. A later order
verified the controls by driving them - real HTTP requests against the real route modules, a real
PostgreSQL 13 with TLS, and the committed suite as its baseline - and raised a separate set of
observations. They are tracked separately from the seven above because they were found a different
way, and because the two sets answer different questions: the earlier one asks whether the code
says the right thing, this one asks what the running code does.

Each row records what was measured, not what was expected. `Where` names the residual register
entry or the follow-up in the
[Security Decision Log](./Security%20Decision%20Log.md#4-accepted-residual-risks), the decision
that settled it, or the test that pins it.

| Finding | What was measured | Disposition and the clause or decision that governs it | Where |
|---------|-------------------|--------------------------------------------------------|-------|
| Unbounded `skip` and `limit` on `GET /workbooks` | The generated specification declares both parameters with a default and no `minimum`; `skip=-1` is admitted, the service receives `skip=-1`, and the value is bound into the emitted statement. PostgreSQL then refuses it, so the caller receives an unhandled `500` where the parameter type otherwise produces a `422`. Nothing is disclosed - the module has no `try`/`except`, so the error boundary answers with its fixed body | **Closed in the runtime-profiling round by `D84`, under deviation `DEV-16`.** Both parameters now carry a floor and a ceiling on both list routes, and a value below or above them answers `422`. The round also found that both parameters answer `500` above `int64`, which this row had not established | D84, DEV-16, `TestListRoutesBoundTheirPageWindow` |
| `GET /workbooks/{id}/worksheets` answers `500` for any worksheet holding a cell | `404` when the workbook has no worksheets, `200` when every worksheet is empty because Pydantic reads `[]` as `{}`, and `ValidationError` - "value is not a valid dict" - as soon as one cell exists | **Declined.** The two candidate fix sites are `workbook_schema.py`, whose request and response contracts §0.1.2.2 freezes, and `models.py`; §0.9.2.1 excludes both, and the projection belongs to the absent `WorksheetService`. No cell-reference key format exists anywhere in the repository, so choosing one would invent wire contract | residual 16, F28, `TestKnownResiduals::test_a_mapped_worksheet_row_still_needs_a_projection` |
| `cells.py` and `collaboration.py` return internal exception text | A malformed identifier, a NUL byte in a cell value and a foreign-key violation each returned a driver message, a table and column name, the parameterised statement and its bound values. The same NUL byte through `POST /workbooks`, which has no `try`/`except`, returned the fixed boundary body - so the disclosure is the handler's, not the framework's | **Closed in the runtime-profiling round by `D85`, under deviation `DEV-16`.** Each handler answers a fixed message at its unchanged status, records the cause with `logger.exception`, and re-raises a deliberate `HTTPException` untouched. `D70`, which kept the defect visible until it could be closed, is marked superseded | D85, DEV-16, `TestHandlersDoNotDiscloseExceptionText` |
| The application cannot start | `uvicorn backend.app.main:app` exits without binding a socket: `ImportError: cannot import name 'get_db' from 'backend.app.db'`. Ten imported names do not exist, because no `__init__.py` exists under `backend/` and four packages are therefore implicit namespaces that re-export nothing, and because `init_db`, the four service classes and `CollaboratorSchema` are absent | **Declined.** §0.9.2.1, §0.13.5 and §0.13.4 item 16 designate all of it as functional gaps to flag rather than implement. DEV-15 records the resulting conflict with the onboarding rule openly rather than resolving it by scope creep | residual 26, F27, F30, DEV-15, `TestKnownResiduals::test_the_application_entry_point_cannot_be_imported` |
| `POST /workbooks/{id}/share` cannot be exercised | `CollaboratorSchema` and `CollaborationService` are both absent, so the route's business behaviour has no implementation to verify | **Declined.** Same exclusion as the row above; the store the route would write to does not exist either | residual 26, F6, F27 |
| The specification declares only `200` and `422`, and `tokenUrl="token"` names no route | Confirmed against the generated document and the router table | **Declined.** R31 settled all of it - documenting `401` means `responses={401: ...}` on the decorator, and `TestRouteAuthenticationDependency::test_route_contract_is_unchanged` requires the decorators to carry no keywords at all. The unroutable `tokenUrl` is D21: the scheme is present to extract the bearer header, and mounting a token route was rejected | residuals 17 and 18, R31, D21 |
| Importing the security or database module performs live Secret Manager reads | `database.py` calls `get_settings()` at module scope, and the constructor reads three secrets | **Declined.** The remedy is caching `get_settings()`, which §0.9.2.1 excludes by name and R24 rejected. The test fixtures stub the client rather than the application working around it | R24, §0.9.2.1 |
| Request bodies are validated permissively | An integer `id` is coerced to `"12345"`, an integer cell value to `"42"`, and an unknown field is dropped without comment. No schema in the module sets `extra` | **Declined.** Adding `extra = "forbid"` changes a frozen request contract (§0.1.2.2) and would refuse bodies that are accepted today. No injection follows: nothing coerced reaches SQL as anything but a bound parameter | residual 33, F45 |
| The create body requires values a caller cannot know | `id`, `owner_id`, `worksheets` and both timestamps are required on `POST /workbooks`; omitting `id` is refused with `422` naming it. No create-specific schema exists | **Declined.** Splitting the request and response shapes means a new schema and a changed request contract, both frozen by §0.1.2.2 | residual 34, F46 |
| A repeated create is accepted twice | The `workbooks` table declares no unique constraint, neither the route nor the schema accepts an idempotency key, and an identical body submitted twice produced two rows with different ids | **Declined.** Any remedy is a constraint in `models.py` or a parameter in the route signature; §0.9.2.1 excludes the model and §0.9.3 the signature. Not a security finding on its own - duplicate rows are a correctness cost, and the volume is bounded by the write tier | residual 35, F47 |
| The specification and documentation endpoints answer with no credential | `/openapi.json`, `/docs`, `/redoc` and `/docs/oauth2-redirect` all return `200` unauthenticated. The document carries schema shape only - no row, address or secret - and the responses still carry the six security headers | **Declined.** These are FastAPI defaults that no application router declares, and nothing in the plan asks for them to be closed. Disabling them means `openapi_url=None` and `docs_url=None` on the application object, which is an entry-point change outside the CORS, header and throttling wiring §0.9.1.2 authorises there | residual 36, F48 |
| `Access-Control-Allow-Headers` lists four names the configuration never set | The header carries `accept`, `accept-language`, `content-language` and `content-type` in addition to the two configured names | **Not a defect.** Starlette's own `SAFELISTED_HEADERS` constant, unioned into the allow-list at construction. All four are CORS-safelisted, so a browser permits them with or without the header. Recorded so it is not raised again | D92 |
| A disallowed-origin refusal still carries the other `Access-Control-*` headers | The `400` carries `Allow-Credentials`, `Allow-Headers`, `Allow-Methods` and `Max-Age`, and does **not** carry `Allow-Origin` | **Not a defect.** `Allow-Origin` is the only header a browser reads to grant access, and it is absent; the rest are a pre-built block returned with the refusal. No access is granted by any of them | D92 |
| Authenticated cross-tenant read, write, share and attribution | A caller authenticated as one user listed workbooks, wrote cells into a workbook owned by another (`200`, cell persisted), shared that workbook with a third party (`200`), and created workbooks attributed to another user through the body's `owner_id`. `current_user` is referenced zero times in all five handler bodies | **Confirmed open, not fixed.** This is the accepted residual §0.8.3.3 names and D13 explains: scoping requires the excluded domain service layer. §0.8.3.3 describes it as a read; the share and attribution facets are wider than that wording, and residual 1 in SECURITY.md states each one as measured. It is the top follow-up | residual 1, F2, D13, SECURITY.md item 1 |
| `sslmode=require` encrypts without authenticating the server | A live session negotiated TLSv1.3 with `TLS_AES_256_GCM_SHA384`, and `pg_stat_ssl.client_dn` was empty | **Confirmed open, not fixed.** D7 as settled by D54 - the proxy presents a plain loopback listener holding no certificate to verify | residual 3, D7, D54, SECURITY.md item 2 |
| Per-process quotas when the window store is unreachable | Enforcement survived the loss of the shared store and continued counting per process | **Confirmed open, not fixed.** Counting coarsens rather than stopping, which is the documented behaviour | residual 19 |

Nothing in this table is closed by pointing at another document. Every declined row names the
clause that forbids the fix, every open residual is in
[SECURITY.md](../SECURITY.md), and each row that can be pinned by a test names it, so the
follow-up landing fails a test rather than passing unnoticed.

Coverage being complete is a statement about *traceability*, not about residual risk, and not
about the application running. Three qualifications matter more than the counts:

- Several controls are partial by design. Every one is recorded in
  [SECURITY.md](../SECURITY.md) under Residual risks — most importantly that authentication is
  enforced but per-owner scoping is not, so an authenticated user can still address another
  user's workbook.
- The traced tests exercise the security controls in isolation, against the real modules through
  fixtures. They do not exercise the assembled application, because it cannot start: four
  pre-existing gaps outside the authorised change set block `import backend.app.main` — no
  package initialisers under `backend/`, no `CollaboratorSchema`, none of the four domain service
  classes, and no `init_db`. `TestKnownResiduals` pins the failure so that closing it is visible
  rather than silent. The [Developer Onboarding guide](./Developer%20Onboarding.md) sets out each
  gap with its file and line, what removing it requires, and what does run today.
- "All pass" is the criterion, not the case count. The counts in this document are true at the
  time of writing and are expected to grow.

### Runtime-profiling findings, and what each one now does

A later round profiled the running application rather than reading it: request concurrency raised
until something broke, database backends terminated mid-flight, response bytes counted on the wire
with decompression disabled, and a real browser driving the API cross-origin. Twelve findings came
back. Eight were fixed, and the four that were not had each already been decided.

Every "after" figure below was measured against the running application, not predicted.

| Finding | Before | After | Governed by |
|---------|--------|-------|-------------|
| A CORS preflight was never counted by either throttling tier, while a browser sends one per authenticated call | `200 x 8` against a 3/minute ceiling; `400 x 8` from a disallowed origin | `200,200,200,429,429,429,429,429` and `400,400,400,429,...`; a non-preflight `OPTIONS` stays metered | D78, `TestPreflightIsThrottled` |
| A throttled cross-origin caller could not read the refusal, and `Retry-After` was unreadable from any browser | The tiers' own refusals carried no cross-origin headers, and `Retry-After` is not CORS-safelisted | The refusal carries the origin echo, credentials flag, `Vary: Origin` and `Access-Control-Expose-Headers: Retry-After`; a refused origin still gets no echo | D79, `TestThrottledResponsesRemainReadableCrossOrigin` |
| No application `INFO` record reached the log, including the throttling store confirmation | `Select-String "Request throttling enabled"` matched nothing | The record is present, and the same bootstrap carries the `WARNING` a shed request emits | D80, `TestApplicationLogging` |
| Pool exhaustion answered `500` after a 30-second wait | 47.3% failures at concurrency 120, median 60,444 ms, 2.2 requests a second, 322 `QueuePool ... timeout` records | Zero failures at every level tested, median 780.6 ms, 142.9 requests a second, zero timeout records; shedding demonstrated separately as exactly 20 served and 10 shed with `Retry-After` | D81, D82, `TestDatabaseConnectionPool`, `TestConcurrencyIsBounded` |
| A server-side connection close surfaced as a user-visible `500` | `{500: 1, 200: 14}` after terminating the process's backends | `{200: 15}`, and no `server closed the connection unexpectedly` record | D83, `TestDatabaseConnectionPool` |
| A list route could be made to serialize the whole table, and negative or above-`int64` values answered `500` | `limit=1000000` returned all 251 rows in 6,809,936 bytes; `limit=-1` and `skip=-1` answered `500`; the worksheets route ignored `limit` entirely | All out-of-range values answer `422` naming the parameter; the worksheets route honours the window, and a 150-worksheet workbook returns exactly 100 | D84, DEV-16, `TestListRoutesBoundTheirPageWindow` |
| Two handlers returned internal exception text | A NUL byte returned the psycopg2 message; the share route returned its service's internal text | A fixed message at the unchanged status, the full traceback in the log, and a deliberate `404` passing through unflattened | D85, DEV-16, `TestHandlersDoNotDiscloseExceptionText` |
| The default page was served entirely uncompressed | 2,742,333 bytes on the wire with no content encoding for any `Accept-Encoding` offered | 36,648 bytes on the wire, a 74.8x reduction confirmed in Chrome, for +1.1 ms; the decoded payload byte-identical | D86, `TestResponsesAreCompressed` |
| The application cannot start | `ImportError: cannot import name 'get_db' from 'backend.app.db'` | Unchanged, and re-confirmed with the full list of absent names enumerated | D88, F27, `TestKnownResiduals::test_the_application_entry_point_cannot_be_imported` |
| `check_revoked=True` costs ~100 ms per successful authentication | — | Unchanged; already recorded with that cost named | D88, R4, D58, residual 9 |
| The uncached `get_settings()` adds ~700 ms per authenticated request | — | Unchanged; forbidden to change, and the rollback toggles depend on it | D88, residual 9 |
| A per-process window store multiplies a client's quota by the worker count | Measured at exactly 4.00x on four workers, 1.00x on one | Unchanged; this is `.env.example`'s documented expectation | D88, residual 19 |


### A committed artifact is not an enforced gate

The table above answers "is this traced?", which is a weaker question than "would a regression be
caught?". Those two were previously conflated, and the gap was load-bearing: `firestore.rules`
counted as verified on the strength of three text assertions about the committed file, while the
one artifact that could evaluate the rules - `firestore_rules_emulator_check.py` - was committed
and never executed by anything. A collaborator was admitted to delete workbooks for exactly that
reason, and every check in the workflow passed.

So verification is classified by what actually runs, and the classes are not equivalent.

**Enforced on every CI run — a regression fails the workflow**

| Gate | What it establishes | Where |
|------|--------------------|-------|
| Backend security suite | Every in-process control: authentication on all five routes, token verification and its failure mapping, the CORS grammar, the transport mode and its topology gate, the header set on success/401/429/preflight, the rate-limit tiers, the object-storage path, and the cross-plane identifier contracts | `security-checks` -> pytest `test_security.py` |
| Firestore rules, evaluated | What the rules *do*, in Google's rules engine: owner, collaborator, stranger and anonymous access to `/workbooks/{workbookId}`, including that deleting is owner-only, and that unmatched paths are denied | `firestore-rules` -> `firebase emulators:exec` |
| Frontend identity bridge | The client half of V3: a bearer credential is attached, a request is refused rather than sent unauthenticated, and the credential is redacted from a failing request | `security-checks` -> `react-scripts test` |
| Dependency advisory delta | That no advisory outside the committed baseline has appeared | `security-checks` -> `pip-audit` |
| Test-collection baseline | That the set of modules failing to collect is exactly the three known ones - a fourth, or the loss of one, fails | `security-checks` -> `pytest --collect-only` |

**Committed and asserted, but not exercised by CI**

| Artifact | What CI can and cannot say | Why |
|----------|---------------------------|-----|
| `scripts/deploy.sh` preflight | CI asserts its *text* - that it names only declared Terraform resources, requires both proxy markers, asserts the invoker policy before the mutation boundary, and performs a live TLS handshake. CI cannot run it | Every check needs `gcloud` and a live project |
| `infrastructure/terraform/*.tf` | CI asserts the presence of specific arguments and resources, **and now runs `terraform fmt -check` and `terraform validate`** — so a configuration Terraform refuses to *load* fails the build instead of passing every text assertion while delivering no control. What CI still cannot say is that an `apply` succeeds, because that needs a live project | Validate is **scoped** to `main.tf` + `variables.tf`, copied into an empty directory. Validating the real directory fails on a pre-existing defect outside this change set — `outputs.tf` references seven resources no configuration declares — and a gate that is red on every run is one the team learns to ignore. The step then asserts that baseline is *unchanged* (exactly seven errors, none outside `outputs.tf`), so repairing `outputs.tf` is noticed and the scoping can be removed (D69) |
| `infrastructure/docker/nginx.conf` | Asserted as text | The frontend image is not the deployed artifact; the static bucket is |

**Tolerated, and visible in the run log**

| Step | Why it cannot be a gate yet |
|------|----------------------------|
| Frontend dependency audit | `npm audit` needs a lockfile, and `frontend/package.json` declares neither `firebase` nor `react-scripts` while no `package-lock.json` is committed. That file is outside this work's change scope. The step runs so the blocked state stays visible rather than being silently dropped |

## Related documents

- [Security Decision Log](./Security%20Decision%20Log.md) — the rationale and the deviation entries
- [SECURITY.md](../SECURITY.md) — controls, canonical response headers, residual risks
- [Developer Onboarding](./Developer%20Onboarding.md) — setup, pitfalls, next tasks
- [README](../README.md) — project overview and current blockers
