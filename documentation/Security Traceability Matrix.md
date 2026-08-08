# Security Traceability Matrix

Bidirectional traceability for the security remediation, at 100% coverage in both directions.

- **Forward**: every vulnerability maps to the artifacts that implement its fix, the control
  introduced, and a *named* verification.
- **Reverse**: every changed artifact maps back to the vulnerability or rule that justifies it.

Nothing is traced by description alone: each verification names a real test function, a real
command, or a documented procedure.

## Baseline and scope

| | |
|---|---|
| Baseline commit | `45a6b7b` — the state before any security work |
| Vulnerabilities | V1–V12, plus one condition (**A06**) that is not a single defect: the absence of a pinned dependency manifest |
| Changed artifacts | **32** = 30 planned + 2 committed verification artifacts |
| Test suite | `backend/tests/test_security.py` — **169** cases at the time of writing, all passing, alongside `backend/tests/firestore_rules_emulator_check.py` for V10 and `frontend/src/services/api.test.ts` for the V3 client half; the count grows as tests are added, so treat "all pass" rather than the number as the criterion |

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

Expect every test to pass — **169** cases at the time of writing. The classes named below are the test classes in that
module, so `-k <ClassName>` selects one.

---

## Forward: every vulnerability to its implementation and verification

| ID | Vulnerability | Implementing artifacts | Control introduced | Verification |
|----|--------------|------------------------|--------------------|--------------|
| **V1** | No route required authentication. `get_current_user` was defined and referenced nowhere; all five handlers declared `Depends(get_db)` alone. | `backend/app/api/workbooks.py`, `worksheets.py`, `cells.py`, `collaboration.py` | `Depends(get_current_user)` on all five handlers, so a request without a valid token is refused before the handler body runs | `TestRouteAuthenticationDependency` (4 tests — the dependency is attached to all five handlers and stays out of the request contract) and `TestUnauthenticatedRequestsAreRefused` (3 tests — `401` with `WWW-Authenticate: Bearer`, asserted against the real dependency) |
| **V2** | `security.py` annotated `Optional[timedelta]` without importing `typing`, so the module raised `NameError` at import — blocking every other authentication control. | `backend/app/core/security.py` | `from typing import Optional` | `TestSecurityModuleImportability` (3 tests — the module imports and `Optional` resolves) |
| **V3** | Two unbridged identity systems: the client signed in with Firebase and never sent a token; the server decoded a custom JWT nothing issued. | `backend/app/core/security.py`, `backend/app/core/config.py`, `backend/requirements.txt`, `frontend/src/services/api.ts` | Firebase ID-token verification against a pinned project, local `User` resolved by the verified `email` claim; an axios request interceptor attaches the token client-side | `TestTokenVerificationOutcomes` (8 tests — a forged, expired or revoked token is refused without reaching a handler, a provider or credential fault is separated from a caller fault, and no cause is echoed to the caller), `TestIdentityClaimValidation` (4 tests) and `TestAuthenticationEnforcementSwitch` (5 tests). Client half: `frontend/src/services/api.test.ts` |
| **V4** | `blob.make_public()` made every uploaded workbook world-readable, and `blob.public_url` was returned to the caller. | `backend/app/services/file_storage.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf` | Expiring V4 signed URLs, no ACL write; uniform bucket-level access and `public_access_prevention = "enforced"` on the uploads bucket; a dedicated signer identity | `TestObjectStorageAccess` (6 tests — no ACL is set and the returned URL is a bounded-expiry signed URL), `TestStorageLogSanitisation` (3 tests) and the expiry bounds in `TestConfigurationContract`. Deployed: `curl -I <object URL>` → `401`/`403`; `gcloud storage buckets describe` |
| **V5** | `allow_methods=["*"]` and `allow_headers=["*"]` with credentials enabled, against an `ALLOWED_ORIGINS` field that did not exist on `Settings`. | `backend/app/core/config.py`, `backend/app/main.py`, `infrastructure/terraform/variables.tf` | `ALLOWED_ORIGINS` declared and validated; methods narrowed to `GET, POST, PUT, OPTIONS` and headers to `Authorization, Content-Type`; one origin grammar shared by the backend and Terraform | `TestCrossOriginPolicy` (5 tests — a disallowed origin's preflight is refused, an arbitrary requested header is not reflected) and the origin grammar in `TestConfigurationContract`, which refuses a wildcard origin at construction |
| **V6** | `create_engine` received no `connect_args`, and Cloud SQL sat on its default SSL mode, which permits unencrypted connections. | `backend/app/db/database.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf` | Client passes `sslmode`; only `require`, `verify-ca` and `verify-full` are accepted; Cloud SQL set to `ENCRYPTED_ONLY` so the server refuses cleartext | `TestDatabaseTransportSecurity` (4 tests — the engine is built with the configured `sslmode`) and the mode validation in `TestConfigurationContract`, which refuses any mode that can negotiate plaintext. Deployed: `gcloud sql instances describe`, then `SHOW ssl;` in a live session |
| **V7** | No security header anywhere — no middleware, no meta tag, no Nginx configuration. | `backend/app/core/security_headers.py`, `backend/app/main.py`, `frontend/public/index.html`, `infrastructure/docker/nginx.conf`, `infrastructure/docker/Dockerfile.frontend`, `infrastructure/terraform/main.tf` | Six header names on every response, delivered at four points that all render one agreeing policy; the header middleware is registered outermost so errors and preflights are covered | `TestSecurityResponseHeaders` (8 tests — the canonical set on a success, a `401`, a `429` and a CORS preflight, and on an unhandled `500` answered from inside the stack) and `TestStaticDeliveryPolicies` (7 tests — the document policy names neither `connect-src` nor `default-src`, so it cannot block the configured API) |
| **V8** | `deploy.sh` created an HTTP target proxy and a port-80 forwarding rule; Terraform declared no edge at all. | `infrastructure/terraform/main.tf`, `infrastructure/terraform/variables.tf`, `scripts/deploy.sh` | Managed SSL certificate, HTTPS target proxy, 443 forwarding rule, and an HTTP→HTTPS redirect; the script can no longer create a plaintext listener | `TestDeploymentSurface` (3 tests — Terraform declares the certificate, the HTTPS proxy and the redirect, and `deploy.sh` creates no port-80 listener). Deployed: `curl -sI http://DOMAIN/` → `301`; `openssl s_client -connect DOMAIN:443` |
| **V9** | No rate limiting existed anywhere — no middleware, no library, no quota. | `backend/app/core/rate_limit.py`, `backend/app/main.py`, `backend/requirements.txt` | Global per-client ceiling plus a tighter write-method budget, returning `429` with `Retry-After`, counted in a shared store so the quota does not multiply per worker | `TestRequestThrottling` (8 tests — `429` with `Retry-After`, reads unmetered by the write tier, the ceiling enforced, and an unusable window expression failing at registration), `TestThrottlingIdentity` (10 tests — a forwarded address is honoured only from a configured trusted proxy) and `TestMiddlewareRegistrationOrder` (4 tests) |
| **V10** | No Firestore rules were committed, while the browser subscribes to Firestore directly — so no server-side control could compensate. | `firestore.rules`, `firebase.json` | Document-level `read`/`create`/`update`/`delete` rules on `/workbooks/{workbookId}` keyed on the immutable Firebase UID; unmatched paths denied by the platform default | `TestFirestoreRules` (3 tests — the committed rules deny by default and gate on owner or collaborator identity). Authorization semantics are asserted against the emulator by `backend/tests/firestore_rules_emulator_check.py`, run with `firebase emulators:exec --only firestore`. `firebase.json` makes the pre-existing `deploy.sh` rules hook functional |
| **V11** | The Cloud Function was deployed with `--allow-unauthenticated`, and no invoker binding existed in Terraform. | `scripts/deploy.sh`, `infrastructure/terraform/main.tf` | The flag is gone; any `allUsers` invoker binding is revoked and the revocation confirmed, with both policy reads failing closed; Terraform binds the invoker role to a named service account | `TestDeploymentSurface` — `deploy.sh` carries no `--allow-unauthenticated` and reads the invoker policy fail-closed, and Terraform binds the invoker role to a named account. Deployed: unauthenticated `curl` → `403`; a call with `gcloud auth print-identity-token` succeeds |
| **V12** | Token lifetime was hardcoded to 15 minutes; `ACCESS_TOKEN_EXPIRE_MINUTES` was declared and never read. | `backend/app/core/security.py` | Lifetime from an explicit `expires_delta` when given, otherwise from the setting | `TestAccessTokenLifetime` (3 tests — the default comes from the setting and an explicit `expires_delta` still wins) |
| **A06** | No dependency manifest of any kind existed for the backend, so every build re-resolved versions with no record — CWE-1104 / OWASP A06:2021. The backend image could not build at all. | `backend/requirements.txt`, `.github/workflows/ci.yml` | All 89 versions exact-pinned (23 direct + 66 transitive), holding `python-jose` above the CVE-2024-33663 fix boundary; a CI audit job that fails on any advisory outside a recorded baseline | `python -m pip_audit -r backend/requirements.txt --progress-spinner off` with the baseline's `--ignore-vuln` flags → exit 0, "14 ignored". Omitting any baseline entry exits 1, which is what proves the gate detects a new advisory rather than passing unconditionally |

**Forward coverage: 13 of 13.** No vulnerability lacks an implementation, and none lacks a
verification.

### Why two rows have no in-process test

V10 and V11 are not testable inside this process, and substituting a weaker in-process check
would misrepresent them:

- **V10** is enforced by Google's rules engine. A Python test could only re-implement the rules
  and assert against its own re-implementation. The emulator evaluates the real file. A committed
  JavaScript rules-test harness was considered and rejected because it would add a frontend
  dependency.
- **V11** is an IAM binding on a deployed resource. What *is* testable in-process is the
  deployment script's behaviour — that it fails closed when the policy cannot be read — and that
  is covered.

---

## Reverse: every changed artifact to its justification

Compare this table against `git diff 45a6b7b --name-status`.

| # | Artifact | Mode | Justified by |
|---|----------|------|--------------|
| 1 | `backend/app/core/security.py` | UPDATE | V2, V3, V12 |
| 2 | `backend/app/core/config.py` | UPDATE | V3, V4, V5, V6, V7, plus the rollback switches |
| 3 | `backend/app/main.py` | UPDATE | V5, V7, V9 |
| 4 | `backend/app/db/database.py` | UPDATE | V6 |
| 5 | `backend/app/services/file_storage.py` | UPDATE | V4 |
| 6 | `backend/app/api/workbooks.py` | UPDATE | V1 |
| 7 | `backend/app/api/worksheets.py` | UPDATE | V1 |
| 8 | `backend/app/api/cells.py` | UPDATE | V1 |
| 9 | `backend/app/api/collaboration.py` | UPDATE | V1 |
| 10 | `backend/app/core/rate_limit.py` | CREATE | V9 |
| 11 | `backend/app/core/security_headers.py` | CREATE | V7 |
| 12 | `backend/requirements.txt` | CREATE | V3, V6, V9, A06 |
| 13 | `backend/tests/conftest.py` | CREATE | Verification prerequisite — without it no test can import the application |
| 14 | `backend/tests/test_security.py` | CREATE | Verification for V1–V7, V9, V12, A06 and the infrastructure contracts |
| 15 | `frontend/src/services/api.ts` | UPDATE | V3 (client half) |
| 16 | `frontend/public/index.html` | UPDATE | V7 |
| 17 | `infrastructure/docker/nginx.conf` | CREATE | V7, V8 |
| 18 | `infrastructure/docker/Dockerfile.frontend` | UPDATE | V7 — activates the Nginx configuration, without which it is dead code |
| 19 | `infrastructure/terraform/main.tf` | UPDATE | V4, V6, V7, V8, V11 |
| 20 | `infrastructure/terraform/variables.tf` | UPDATE | V4, V5, V8 |
| 21 | `scripts/deploy.sh` | UPDATE | V8, V11 |
| 22 | `firestore.rules` | CREATE | V10 |
| 23 | `firebase.json` | CREATE | V10 |
| 24 | `.env.example` | CREATE | V4, V5, V6 configuration surface; Rule 2 |
| 25 | `.github/workflows/ci.yml` | UPDATE | A06 — the dependency-audit gate and the security verification steps |
| 26 | `README.md` | UPDATE | **Rule 2** |
| 27 | `documentation/Developer Onboarding.md` | CREATE | **Rule 2** |
| 28 | `SECURITY.md` | CREATE | **Rule 2** |
| 29 | `documentation/Security Decision Log.md` | CREATE | **Rule 1** |
| 30 | `documentation/Security Traceability Matrix.md` | CREATE | **Rule 1** — this document |
| 31 | `backend/tests/firestore_rules_emulator_check.py` | CREATE | V10 — executable emulator assertions; the browser reaches Firestore directly, so rules are the only control on that path and this is its only verification |
| 32 | `frontend/src/services/api.test.ts` | CREATE | V3 — asserts the client attaches the verified Firebase ID token, the half of the identity bridge no backend test can observe |

**Reverse coverage: 32 of 32.** Rows 1–30 are the planned artifacts; rows 31–32 are the two
committed verification artifacts, which extend the test-file deviation already recorded in the
[Security Decision Log](./Security%20Decision%20Log.md) rather than adding new product code. The
split is stated explicitly rather than presenting 32 as though all of it had been planned.

---

## Files deliberately not changed

Recorded because their absence from the diff is a decision, not an oversight. Each is read as
authoritative context, and several are the reason a fix took the shape it did.

| File | Why it is untouched |
|------|--------------------|
| `backend/app/db/models.py` (schema) | Columns are unchanged. Adding `firebase_uid` needs migration tooling the repository does not have, which is why V3 keys identity on the verified `email` claim. |
| `backend/app/schema/workbook_schema.py` | Reference-only. `WorksheetSchema` still lacks `orm_mode`, so `from_orm` raises; the defect is unreachable while the domain services are absent, and it is carried as a follow-up rather than fixed outside the authorised set. |
| `frontend/src/services/collaboration.ts` | Reference-only. Its `collection()` call is why the browser's Firestore path is non-functional today, which is precisely why the fail-closed V10 rules regress nothing measurable. |
| `backend/app/services/real_time_sync.py` | Out of scope. It writes workbook documents with no owner field, which is why the V10 rules currently deny all browser access — the correct fail-closed outcome. |
| `backend/app/tasks/background_jobs.py` | Out of scope. It persists a storage URL, which signed URLs would invalidate; the code path is already non-functional, so nothing working regresses. |
| `infrastructure/terraform/outputs.tf` | Out of scope. Its seven references to undeclared resources are why `terraform validate` fails; `main.tf` and `variables.tf` validate clean. |
| `infrastructure/docker/Dockerfile.backend` | Out of scope. Its `python:3.9-slim` pin is why no dependency advisory is currently fixable. |
| `backend/app/services/calculation_engine.py` | Out of scope, and verified to contain no `eval` or `exec`, so there is no formula-injection vector. |
| `frontend/src/services/auth.ts` | A contract that must not change — the Firebase sign-in flow is the front door, which is why V3 bridges into `get_current_user` rather than replacing the identity system. |
| `backend/tests/test_api.py`, `test_calculation_engine.py`, `test_collaboration.py` | Left as-is so the pre-existing three-collection-error baseline stays a valid comparison point. |
| `.github/workflows/cd.yml` | Out of scope. Its workflow-name mismatch with `ci.yml` is why CD does not trigger. |
| `frontend/package.json` | Out of scope. No frontend dependency was added; its undeclared packages are a pre-existing gap. |

---

## Coverage assertion

| | Count | Gaps |
|---|-------|------|
| Forward — vulnerability to implementation and verification | 13 / 13 | none |
| Reverse — artifact to justifying vulnerability or rule | 32 / 32 | none |
| Vulnerabilities with an automated in-process test | 13 / 13 | none, though V8, V10 and V11 are asserted against the committed configuration rather than against live cloud behaviour, which only a deployment can show |

Coverage being complete is a statement about *traceability*, not about residual risk. Several
controls are partial by design, and every one of those is recorded in
[SECURITY.md](../SECURITY.md) under Residual risks — most importantly that authentication is
enforced but per-owner scoping is not, so an authenticated user can still address another user's
workbook.

## Related documents

- [Security Decision Log](./Security%20Decision%20Log.md) — the rationale and the deviation entries
- [SECURITY.md](../SECURITY.md) — controls, canonical response headers, residual risks
- [Developer Onboarding](./Developer%20Onboarding.md) — setup, pitfalls, next tasks
- [README](../README.md) — project overview and current blockers
