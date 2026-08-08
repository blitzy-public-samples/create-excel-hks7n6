# Security Traceability Matrix

This document exists because the **Explainability** rule requires a bidirectional traceability
matrix for a refactor of this shape — one that maps every source construct to its target
implementation at 100% coverage with no gaps. It maps, and it counts. It carries no rationale:
every "why", every alternative weighed and every trade-off accepted lives in
[Security Decision Log](<./Security Decision Log.md>), which is the single source of truth for
that. Where a reader needs a reason here, this document cites an identifier — `D3`, `DEV-2`,
`R8` — and the log answers it.

Everything below is measured against base commit **`45a6b7b`**, the state of the repository
before any security change was made.

Two tables, read in opposite directions:

- **[Forward](#forward-direction--finding-to-implementation-to-verification)** — each finding
  to the artifacts that implement it, the control introduced, and how that control is
  verified. Answers *is every finding fixed, and how do I prove it?*
- **[Reverse](#reverse-direction--artifact-to-justification)** — each changed artifact back to
  the finding, rule or deviation that authorizes it. Answers *is every change accounted for?*

The reverse table is the load-bearing one. Because the Explainability rule treats an
unexplained deviation as a defect, a path that appears in `git diff --name-status 45a6b7b`
with no row here is not an untidiness — it is a defect, mechanically detectable by the
procedure in [Re-verification](#re-verification). That makes this document the **acceptance
gate** for that rule rather than a courtesy, and the
[coverage assertion](#coverage-assertion) states the counts plainly, including where the
delivered set differs from the set originally planned.

Deployment-time commands below use the placeholders `API`, `DOMAIN`, `BUCKET`, `INSTANCE` and
`FUNCTION_URL` in place of real values. Configuration keys are documented in
[.env.example](../.env.example); canonical response-header values and the residual-risk
inventory belong to [SECURITY.md](../SECURITY.md); setup, pitfalls and suggested next tasks
belong to [Developer Onboarding](<./Developer Onboarding.md>). This document references those
and does not reproduce them.

---

## Forward direction — finding to implementation to verification

Thirteen findings: the twelve numbered vulnerabilities `V1`–`V12`, plus `A06`, the
missing-dependency-manifest weakness that has no `V` number because it was not one of the
twelve but is remediated by the same change set.

Every row names a verification. Where a control has no in-process test, the row says so
explicitly rather than leaving the cell thin — that absence is a finding in its own right, not
an omission. Test names are the real function names in
`backend/tests/test_security.py` and can be run individually with
`python -m pytest backend/tests/test_security.py -k <name>`.

| ID | Finding at `45a6b7b` | Implementing artifact(s) | Control introduced | Verification |
|----|----------------------|--------------------------|--------------------|--------------|
| **V1** | Unenforced authentication on five endpoints. `get_current_user` was defined and referenced nowhere in the repository; all five handlers declared `Depends(get_db)` as their only dependency, so any caller reached the handler body with no identity check. CWE-306, OWASP A01. | `backend/app/api/workbooks.py`, `backend/app/api/worksheets.py`, `backend/app/api/cells.py`, `backend/app/api/collaboration.py` | `current_user: User = Depends(get_current_user)` attached to all five handlers — `get_workbooks`, `create_workbook`, `get_worksheets`, `update_cells`, `share_workbook` — so an unauthenticated request terminates before the body runs. Handler bodies, route decorators, methods and schemas are untouched, and no ownership check is added (`D13`). | `test_v1_route_rejects_an_unauthenticated_request`, `test_v1_route_does_not_reach_its_handler_unauthenticated`, `test_v1_route_admits_an_authenticated_caller`, `test_v1_route_reaches_its_handler_authenticated`, `test_v1_route_declares_the_bearer_security_requirement`, `test_v1_openapi_paths_are_unchanged_by_the_auth_dependency`, `test_v1_auth_dependency_is_absent_from_the_request_contract`. Deployed: `curl -si https://API/workbooks` expecting HTTP 401 with a `WWW-Authenticate: Bearer` challenge. |
| **V2** | `Optional` referenced without importing `typing`, in the `create_access_token` annotation. Python 3.9 evaluates that annotation at definition time, so the module raised `NameError` on import — which made every other control in it unreachable and therefore blocked `V1` and `V3`. | `backend/app/core/security.py` | `Optional` added to the module's `typing` import, restoring importability. | `test_v2_security_module_imports`, `test_v2_optional_annotation_resolves`, `test_v2_security_module_executes_from_source`. Command: `python -c "import backend.app.core.security"` expecting exit status 0. |
| **V3** | Dual unbridged identity systems. The client signed in through Firebase and never called `getIdToken()`; no request carried an `Authorization` header; the server decoded a self-issued HS256 token that nothing ever minted. Identity was asserted by the client and never verified. CWE-287, OWASP A07. | `backend/app/core/security.py`, `backend/app/core/config.py`, `backend/requirements.txt`, `frontend/src/services/api.ts` | Server-side Firebase ID-token verification inside `get_current_user`, with revocation checking, resolving the principal by the verified `email` claim (`D1`, `D2`, `R1`, `R4`); every verification failure — bad token, expired, revoked, disabled user, certificate-fetch or credential fault — mapped to the existing 401 with the cause recorded server-side only and never echoed (`R5`, `R29`); client-side Axios request interceptor attaching `Authorization: Bearer` with the token the Firebase SDK already holds, read at call time so it survives a page reload (`DEV-1`, `R25`); `firebase-admin` pinned in the manifest. Signatures and the Redux state model are unchanged. | `test_v3_forged_token_is_rejected`, `test_v3_forged_token_does_not_reach_the_handler`, `test_v3_rejection_body_carries_no_failure_cause`, `test_v3_verification_fault_is_rejected_not_served`, `test_v3_verification_fault_detail_is_not_echoed`, `test_v3_verification_receives_the_presented_token`. Deployed: sign in through the SPA and call the API end to end, then repeat after a page reload. |
| **V4** | Publicly readable uploads. `upload_file` called `blob.make_public()` and returned `blob.public_url`, so every uploaded workbook was world-readable by a durable, guessable, unauthenticated URL. CWE-732 and CWE-200, OWASP A01 and A05. | `backend/app/services/file_storage.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf`, `infrastructure/terraform/variables.tf` | No ACL is written at all; `upload_file` returns a bounded-expiry V4 signed URL bound to the generation it wrote (`D3`). Reinforced at the bucket so code cannot reintroduce the exposure: `uniform_bucket_level_access` plus `public_access_prevention = "enforced"` on the uploads bucket, and uniform access only on the bucket that intentionally serves the public SPA (`D5`). Signing is authorized through a dedicated signer service account, with `roles/iam.serviceAccountTokenCreator` bound on the signer and the runtime account as its member, reached by workload identity with no key file (`R8`). The code change is ordered no later than the bucket policy, and reverts with it (`D4`). | `test_v4_upload_sets_no_public_acl`, `test_v4_upload_returns_a_signed_url`, `test_v4_signed_url_expiry_is_bounded`, `test_v4_signed_url_is_bound_to_the_written_generation`, `test_v4_upload_targets_the_configured_bucket`, `test_v4_signed_upload_is_retained`. Deployed: anonymous `curl -I` against an object URL expecting HTTP 401 or 403 (a 200 or 302 means still public); `gcloud storage buckets describe gs://BUCKET --format='value(iamConfiguration.publicAccessPrevention,iamConfiguration.uniformBucketLevelAccess.enabled)'` expecting `enforced` and `True`. |
| **V5** | Permissive and unresolved CORS. `allow_origins` referenced a `Settings` field that did not exist, while methods and headers were wildcards with credentials enabled — so preflight echoed any requesting origin verbatim and reflected arbitrary requested headers. CWE-942 and CWE-346, OWASP A05. | `backend/app/core/config.py`, `backend/app/main.py`, `infrastructure/terraform/variables.tf`, `infrastructure/terraform/main.tf` | `ALLOWED_ORIGINS` declared on `Settings`, which makes the origin policy resolvable at all. Both wildcards replaced by finite lists: the four methods the application actually uses, and `Authorization` plus `Content-Type`. Identity Platform's authorized-domain list is derived from the same Terraform variable that drives the allow-list, so the sign-in and CORS views of a legitimate origin cannot diverge (`D26`). | `test_v5_allowed_origin_preflight_is_admitted`, `test_v5_disallowed_origin_preflight_is_rejected`, `test_v5_disallowed_origin_simple_request_carries_no_grant`, `test_v5_allowed_origin_simple_request_carries_that_origin`, `test_v5_arbitrary_request_header_is_not_reflected`, `test_v5_declared_request_headers_are_admitted`, `test_v5_allowed_methods_are_an_explicit_list`. Deployed: `curl -si -X OPTIONS -H "Origin: https://disallowed.example" -H "Access-Control-Request-Method: GET" https://API/workbooks` expecting HTTP 400 with body `Disallowed CORS origin` and no `Access-Control-Allow-Origin` header. |
| **V6** | No database TLS. The engine was created with no transport arguments, and the Cloud SQL instance declared no SSL mode and so sat on the permissive default that accepts unencrypted connections. Credentials and row data could cross the network in cleartext. CWE-319, OWASP A02. | `backend/app/db/database.py`, `backend/app/core/config.py`, `infrastructure/terraform/main.tf` | Client side, `connect_args` carrying the configured `sslmode` is passed to `create_engine`, so the driver requests an encrypted channel. Server side, `ip_configuration` sets `ssl_mode = "ENCRYPTED_ONLY"` so the instance refuses unencrypted connections — the half no client can enforce. Only `ssl_mode` is set, never the superseded attribute (`D7`, `D8`). | `test_v6_engine_requires_sslmode`, `test_v6_sslmode_comes_from_configuration`, `test_v6_engine_addresses_the_configured_database`, `test_v6_probe_leaves_the_imported_module_untouched`. These **spy on the recorded `create_engine` call arguments** rather than inspecting the engine, because SQLAlchemy merges `connect_args` at connect time and they are not readable from the engine object. Deployed: `gcloud sql instances describe INSTANCE --format="json(settings.ipConfiguration.sslMode)"` expecting `ENCRYPTED_ONLY`; then `SHOW ssl;` or `SELECT * FROM pg_stat_ssl;` in a live session expecting SSL in use. |
| **V7** | No HTTP security headers on either tier. No header middleware existed, the SPA document carried no security meta tag, and the repository contained no Nginx configuration file at all — so nothing restricted script sources, framing, MIME sniffing or referrer leakage. CWE-1021 and CWE-693, OWASP A05. | `backend/app/core/security_headers.py`, `backend/app/main.py`, `frontend/public/index.html`, `infrastructure/docker/nginx.conf`, `infrastructure/docker/Dockerfile.frontend`, `infrastructure/terraform/main.tf` | One canonical baseline set — Content-Security-Policy, Strict-Transport-Security, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` and `Permissions-Policy` — emitted at four delivery points that between them cover API responses, the live static SPA, the container path and the load-balancer edge (`D9`). The middleware is registered **last** so it is outermost, which is what puts headers on error and preflight responses (`D10`). The Nginx configuration also carries an SPA deep-link fallback (`D11`), and the `COPY` that installs it is activated (`DEV-2`). Canonical values are defined once and documented in [SECURITY.md](../SECURITY.md). | `test_v7_security_headers_on_a_successful_response`, `test_v7_security_headers_on_an_unauthorized_response`, `test_v7_security_headers_on_a_cors_preflight_response`, `test_v7_security_headers_on_a_rejected_cors_response`, `test_v7_security_headers_on_a_too_many_requests_response`, `test_v7_header_middleware_is_outermost_on_the_application`, `test_v7_header_values_are_the_canonical_set`, `test_v7_content_security_policy_is_enforced_not_reported`, `test_v7_content_security_policy_restricts_framing_and_sources`. Deployed: `curl -sI https://DOMAIN/` and `curl -sI https://API/workbooks` expecting the full set on both origins. |
| **V8** | Plaintext HTTP at the edge. The deployment script created an HTTP target proxy and a port-80 forwarding rule, and Terraform declared no load balancer, URL map, target proxy or SSL certificate of any kind — so all application traffic, bearer tokens included, crossed the network in cleartext. CWE-319, OWASP A02. | `infrastructure/terraform/main.tf`, `infrastructure/terraform/variables.tf`, `scripts/deploy.sh`, `infrastructure/docker/nginx.conf` | A managed SSL certificate, an HTTPS target proxy and a port-443 forwarding rule; an HTTP-to-HTTPS redirect URL map with the port-80 proxy bound only to it, so port 80 never reaches content. The redirect is gated on the certificate actually being active, so a cutover cannot strand the site behind a certificate that is not yet serving. Terraform is the sole owner of the edge and the deployment script can no longer create a plaintext listener, so neither can undo the other (`R12`). | **No in-process unit test** — this control is cloud-edge configuration with no in-process surface to assert against. `terraform plan` shows the certificate, HTTPS proxy, redirect map and both forwarding rules; `bash -n scripts/deploy.sh` parses the script. Deployed: `curl -sI http://DOMAIN/` expecting HTTP 301 to `https://`; `openssl s_client -connect DOMAIN:443 -tls1_3 </dev/null` expecting a successful handshake. |
| **V9** | No rate limiting. No middleware, no library and no quota existed anywhere under the backend package, leaving brute force, credential stuffing and volumetric scraping unbounded. CWE-770 and CWE-307, OWASP A04. | `backend/app/core/rate_limit.py`, `backend/app/main.py`, `backend/requirements.txt` | A global per-client ceiling plus a tighter budget for mutating methods, both installed by a single `register_rate_limiting(app)` call that also binds the limiter and its rejection handler, so the middleware cannot be registered without the handler that turns a rejection into a 429 rather than a 500 (`D6`, `D31`). Rejections carry `Retry-After`. Both thresholds are configuration, and no handler signature changed. | `test_v9_both_tiers_are_registered_on_the_application`, `test_v9_write_tier_rejects_beyond_its_budget`, `test_v9_write_tier_leaves_reads_unaffected`, `test_v9_ceiling_tier_rejects_beyond_its_budget`, `test_v9_probe_budget_is_restored_on_exit`, `test_v9_application_budget_is_not_spent_by_a_probe`. Deployed: a loop of POST requests past the threshold expecting HTTP 429 with `Retry-After`, and a normal cell-editing and autosave session expecting none. |
| **V10** | No Firestore security rules. Neither `firestore.rules` nor `firebase.json` existed, while the deployment script already ran a Firestore rules deploy that resolved nothing — and the browser reaches this store **directly**, without traversing the API, so no server-side control can compensate. CWE-862 and CWE-1188, OWASP A01. | `firestore.rules`, `firebase.json`, `frontend/src/services/collaboration.ts` | Owner and collaborator authorization on `workbooks/{workbookId}`, keyed on the immutable `request.auth.uid`. A creation must name the caller as owner and carry well-formed ownership fields, and an update may not rewrite them — without both, the write path would defeat the read rule (`D34`). Every path no rule matches is denied by Firestore's own default, so no catch-all clause is needed. `firebase.json` declares the rules pointer and nothing else (`D35`), which makes the existing deploy step functional with no edit to it. The sole client caller now addresses the path as a document, so the rules are evaluated at all rather than governing a path no legal request could reach (`DEV-8`). | **No in-process unit test** — rules are validated on the emulator as a documented procedure rather than a committed JavaScript harness (`D22`; machine-verified rules tests are follow-up `F13`). `firebase emulators:exec --only firestore "<assertions>"` expecting the owner admitted, a non-owner denied and an unauthenticated caller denied. Note the rules deny all client reads until an out-of-scope writer populates the ownership fields (`D12`, `F4`); that is the intended fail-closed state, not a verification failure. |
| **V11** | Unauthenticated Cloud Function. The function was deployed with `--allow-unauthenticated`, and no invoker binding existed in Terraform, so invoker IAM was entirely unmanaged and anyone discovering the URL could call it. CWE-306, OWASP A01 and A05. | `scripts/deploy.sh`, `infrastructure/terraform/main.tf` | The flag is removed, so no `allUsers` invoker binding is created. The script additionally revokes any pre-existing `allUsers` invoker binding explicitly and re-reads the policy to confirm the revocation rather than assuming it (`R13`). Terraform declares a `roles/cloudfunctions.invoker` binding to a named service account, expressing the intended caller instead of leaving it unmanaged. | **No in-process unit test** — this control is deployment-time IAM with no in-process surface. `bash -n scripts/deploy.sh` parses the script and `terraform plan` shows the invoker binding. Deployed: `curl -si FUNCTION_URL` expecting HTTP 403, then the same call with `-H "Authorization: Bearer $(gcloud auth print-identity-token)"` expecting success. |
| **V12** | Unused token-lifetime setting. `create_access_token` hardcoded its expiry while the declared `ACCESS_TOKEN_EXPIRE_MINUTES` setting was never read, so an operator shortening the configured lifetime had no effect. CWE-613, OWASP A04. | `backend/app/core/security.py` | Lifetime is taken from the caller's `expires_delta` when supplied and from `ACCESS_TOKEN_EXPIRE_MINUTES` otherwise. The `create_access_token` signature is preserved exactly, so no existing or future caller is broken. | `test_v12_default_lifetime_comes_from_configuration`, `test_v12_explicit_expires_delta_wins`, `test_v12_create_access_token_signature_is_unchanged`. |
| **A06** | No pinned dependency manifest for the backend — no `requirements.txt`, `pyproject.toml`, `Pipfile` or lock file of any kind — so every build resolved unpinned, unrecorded and unauditable, with no advisory feed possible. The backend image could not build at all, because its Dockerfile already copied a manifest that did not exist. CWE-1104, OWASP A06:2021. | `backend/requirements.txt`, `.github/workflows/ci.yml` | A fully pinned, reproducible dependency set. Security-critical pins: the JWT library above its CVE-2024-33663 fix floor, so it can no longer resolve into the algorithm-confusion range (`D14`); the password backend at the release its hashing library actually works against (`D15`); the settings framework below the major version that moved `BaseSettings` out of the package the configuration module imports it from (`D16`). The audit tool is deliberately kept out of the manifest so the audited set equals the shipped set (`D36`), and CI gates on an advisory *delta* against a committed baseline (`D19`). **Stated plainly: pinning clears no existing advisory.** Every published fix version for the reported advisories requires a newer Python than the documented runtime, so what this closes is the reproducibility weakness and the ability to silently regress below a known fix floor — not the advisory count, which is unchanged. | `python -m pip_audit -r backend/requirements.txt --progress-spinner off`, carrying the committed baseline of runtime-blocked identifiers as `--ignore-vuln` flags, run by the `security-checks` job in `.github/workflows/ci.yml` — it fails only on an advisory outside that baseline, which is what makes a newly published one visible. The same job installs the manifest and re-runs `python -m pytest backend/tests/test_security.py -q --no-header -p no:cacheprovider`. |

---

## Reverse direction — artifact to justification

Every path this change set touches, traced back to what authorizes it. A path in the diff with
no row here is a scope breach; see [Re-verification](#re-verification).

Five artifacts are justified by a **rule alone** and carry no vulnerability identifier. That is
not an oversight — none of the thirteen findings requires any of them, and making that visible
is the point of listing them.

### A. Delivered — the 30 paths in `git diff --name-status 45a6b7b`

**Backend — core and application**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `backend/app/core/security.py` | UPDATE | V2, V3, V12; the Session the identity lookup opens is now closed on every path (`DEV-5`) |
| `backend/app/core/config.py` | UPDATE | V4, V5, V6; the rollback toggles (`D17`, `D18`) and the signer address the IAM grant must agree with (`R9`) |
| `backend/app/core/rate_limit.py` | CREATE | V9 |
| `backend/app/core/security_headers.py` | CREATE | V7 — the canonical header set is defined here once (`D9`) |
| `backend/app/main.py` | UPDATE | V5, V7, V9; middleware registration order (`D10`) and the innermost bypass marker (`D25`) |
| `backend/app/db/database.py` | UPDATE | V6 |
| `backend/app/services/file_storage.py` | UPDATE | V4 |

**Backend — API routes**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `backend/app/api/workbooks.py` | UPDATE | V1 — `get_workbooks` and `create_workbook` |
| `backend/app/api/worksheets.py` | UPDATE | V1 — `get_worksheets` |
| `backend/app/api/cells.py` | UPDATE | V1 — `update_cells` |
| `backend/app/api/collaboration.py` | UPDATE | V1 — `share_workbook` |

**Backend — reference-only modules changed because enforcing V1 made a dormant defect live**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `backend/app/db/models.py` | UPDATE | `DEV-6` — prerequisite for V1: without the reverse relationship, every successfully authenticated request fails mapper configuration and returns HTTP 500 on all five routes |
| `backend/app/schema/workbook_schema.py` | UPDATE | `DEV-7` — prerequisite for V1: the worksheets route constructs this schema with `from_orm`, which Pydantic v1 refuses without the config attribute, turning every non-empty result into HTTP 500 |

**Backend — dependency manifest and verification**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `backend/requirements.txt` | CREATE | V3, V6, V9 (the packages those controls need) and the A06 manifest gap |
| `backend/tests/conftest.py` | CREATE | `DEV-3` — prerequisite for all in-process verification: without it no test can import the application |
| `backend/tests/test_security.py` | CREATE | `DEV-3` — verification for V1, V2, V3, V4, V5, V6, V7, V9 and V12 |

**Frontend**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `frontend/src/services/api.ts` | UPDATE | V3 via `DEV-1` — the client half, without which enforcing V1 returns 401 to every legitimate user |
| `frontend/src/services/collaboration.ts` | UPDATE | V10 via `DEV-8` — the sole client caller addressed the rules' document path as a collection, so no legal request could reach the rules being deployed |
| `frontend/public/index.html` | UPDATE | V7 |

**Container image and edge configuration**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `infrastructure/docker/nginx.conf` | CREATE | V7, V8 |
| `infrastructure/docker/Dockerfile.frontend` | UPDATE | V7 activation via `DEV-2` — without it the mandated Nginx configuration is dead code |

**Infrastructure and deployment**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `infrastructure/terraform/main.tf` | UPDATE | V4, V6, V7, V8, V11 |
| `infrastructure/terraform/variables.tf` | UPDATE | V4, V5, V8; the trailing newline is logged separately (`D28`) |
| `scripts/deploy.sh` | UPDATE | V8, V11 |

**Google Cloud service configuration**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `firestore.rules` | CREATE | V10 |
| `firebase.json` | CREATE | V10 |
| `.env.example` | CREATE | the V4, V5 and V6 configuration surface, and **Rule 2 (Onboarding)** — an operator cannot configure the application without it |

**Continuous integration**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `.github/workflows/ci.yml` | UPDATE | A06 — the delta-based advisory gate and the automated control verification |

**Rule-mandated documentation**

| Artifact | Mode | Justified by |
|----------|------|--------------|
| `documentation/Security Decision Log.md` | CREATE | **Rule 1 (Explainability)** only |
| `documentation/Security Traceability Matrix.md` | CREATE | **Rule 1 (Explainability)** only — this file; the matrix accounts for itself |

### B. Planned and not yet delivered — 3 paths absent from the diff at the time of writing

These are authored after this matrix, so they are listed as pending rather than counted as
delivered. All three are justified by a **rule alone**. Once they land, the delivered path count
becomes 33 and the [Re-verification](#re-verification) comparison should expect them.

| Artifact | Mode | Justified by | Status |
|----------|------|--------------|--------|
| `README.md` | UPDATE | **Rule 2 (Onboarding)** only — it describes a different application's stack and links files that do not exist (`D24`) | Pending |
| `SECURITY.md` | CREATE | **Rule 2 (Onboarding)** only — reporting process, supported versions, canonical header values and residual risks | Pending |
| `documentation/Developer Onboarding.md` | CREATE | **Rule 2 (Onboarding)** only — clean machine to running application, domain context, pitfalls, how to extend, next tasks (`D23`) | Pending |

### No deletions

**This change set deletes no file, and the absence of `DELETE` rows above is deliberate rather
than an oversight.** No file in the repository introduces a vulnerability in its entirety: V4,
the one finding whose remedy is the removal of code, is remediated by removing a single call
from a file that is otherwise retained and required. Every other remedy is additive or a
value change.

---

## Coverage assertion

Counted from the rows actually written above and from
[Security Decision Log](<./Security Decision Log.md>) itself, not from the plan that preceded
either.

| Direction | Coverage | Gaps |
|-----------|----------|------|
| Forward — finding to implementation **and** verification | **13 of 13** | None |
| Reverse — delivered artifact to justification | **30 of 30** | None |
| Reverse — planned artifact not yet delivered | 3, listed and marked pending | None unaccounted |
| **Total paths accounted** | **33** | — |

How the thirteen findings are verified, since "verified" does not mean the same thing for all
of them:

- **9 findings** — V1, V2, V3, V4, V5, V6, V7, V9, V12 — are covered by in-process automated
  tests in `backend/tests/test_security.py`: **51 test functions** yielding **79 collected test
  cases**, all passing. Every one of the 51 is cited by name in the forward table, so the table
  accounts for the whole suite rather than a sample of it.
- **1 finding** — A06 — is covered by an automated CI command rather than a test: the
  delta-mode dependency audit in the `security-checks` job.
- **3 findings** — V8, V10, V11 — have **no in-process test** and are covered by
  deployment-time or emulator commands only. Each row above says so in its own words. This is a
  known limitation, not a gap in this matrix: the controls are cloud-edge configuration,
  Firestore rules and deployment-time IAM, none of which has an in-process surface to assert
  against. Machine-verified Firestore rules tests are follow-up `F13`.

Decision Log entries this matrix cites into: **36** decisions (`D1`–`D36`), **8** deviations
(`DEV-1`–`DEV-8`), **27** review-response decisions (`R14`–`R17` were never issued), **16**
accepted residual risks (8 and 10 withdrawn) and **20** follow-ups (`F10` and `F18` withdrawn)
— 107 entries. Identifiers are never reused, so a gap in a series means never issued or
withdrawn, never missing.

### Where the delivered set differs from the set originally planned

The plan for this remediation named 30 artifacts. The delivered set is also 30, but it is **not
the same 30**, and saying "30 of 30" without that sentence would be a false assurance. Two
differences, both accounted for above:

- **Three delivered paths were not in the plan**, each having been listed there as
  reference-only. `backend/app/db/models.py` and `backend/app/schema/workbook_schema.py` were
  changed because enforcing V1 turned a dormant defect in each into an HTTP 500 on every
  authenticated request; both are authorized by `DEV-6` and `DEV-7`.
  `frontend/src/services/collaboration.ts` was changed so the V10 rules could be evaluated at
  all. **When this matrix was written that third path had no decision-log entry, which under
  the Explainability rule made it a defect rather than a liberty; `DEV-8` was added to close
  it, and `DEV-4` was amended where it had asserted that no unlisted path remained.** That is
  recorded here because the gate is worth nothing if it is silent about the one breach it
  actually caught.
- **Three planned paths are not yet delivered** — `README.md`, `SECURITY.md` and
  `documentation/Developer Onboarding.md`, all justified by the onboarding rule alone. They are
  in section B and are expected to appear.

---

## Re-verification

This check is the **acceptance gate** for the Explainability rule, not a courtesy: that rule
treats an unexplained deviation as a defect, so a path with no row is a defect and this is the
procedure that finds it. Run it whenever the change set moves.

```bash
# 1. List every path changed since the base commit.
git diff --name-status 45a6b7b

# 2. Compare that list against reverse table section A, path by path.
#    Diff the two lists mechanically; do not read them side by side and judge.
git diff --name-only 45a6b7b | sort > /tmp/actual.txt
#    ...then compare /tmp/actual.txt against the section A paths.

# 3. Confirm the forward table's verifications still hold.
python -m pytest backend/tests/test_security.py -q --no-header -p no:cacheprovider
python -m pip_audit -r backend/requirements.txt --progress-spinner off   # baseline flags per CI
```

Interpreting the comparison:

- **A path in the diff but absent from section A is a scope breach.** It must be reverted, or
  authorized by a new `DEV-n` entry in
  [Security Decision Log](<./Security Decision Log.md>) and given a row here. Both are
  acceptable outcomes; leaving it unexplained is not.
- **A path in section A but absent from the diff is an unimplemented item** — either the change
  was lost or the row is aspirational. Section B is the only place a not-yet-delivered path may
  sit, and only while it is genuinely pending.
- **A forward row whose verification no longer passes is a regressed control**, regardless of
  whether the artifact still appears in the diff.

### Paths that must not appear in the diff

Their absence is itself an assertion — each was examined and deliberately left alone, and any of
them appearing means the boundary moved without a decision to move it. Reasons are in the
Decision Log's follow-up and residual sections; they are not restated here.

| Path | Why its absence is asserted |
|------|------------------------------|
| `frontend/package.json` | No frontend dependency is added; its undeclared-package defect is flagged, not fixed (`R18`) |
| `infrastructure/terraform/outputs.tf` | References resources that do not exist; a reported verification blocker, out of scope |
| `infrastructure/docker/Dockerfile.backend` | Pins the end-of-life runtime that blocks every dependency advisory; upgrading it is follow-up `F1` |
| `scripts/setup_dev_environment.sh` | Issues commands for a different framework; corrected procedure is documented, the script is not |
| `.github/workflows/cd.yml` | Only `ci.yml` is authorized for extension |
| `backend/app/services/real_time_sync.py` | The reason the Firestore rules fail closed; populating the ownership fields is follow-up `F4` |
| `backend/app/services/calculation_engine.py` | Contains no `eval` or `exec`, so no injection vector; explicitly out of scope |
| `backend/app/tasks/background_jobs.py` | Carries the signed-URL persistence ripple, already non-functional |
| `backend/tests/test_api.py`, `backend/tests/test_calculation_engine.py`, `backend/tests/test_collaboration.py` | Left untouched so the pre-existing collection-error baseline stays a valid comparison point |
| `frontend/src/services/auth.ts` | The Firebase sign-in flow is a frozen contract |
| `frontend/src/store/index.ts`, `userSlice.ts`, `workbookSlice.ts` | The Redux state model is a frozen contract |
| `frontend/src/components/Cell.tsx`, `ChartDialog.tsx`, `FormulaBar.tsx`, `Grid.tsx`, `Ribbon.tsx`, `Sidebar.tsx` | UI components are explicitly out of scope |
| `frontend/src/utils/formulaParser.ts`, `frontend/src/utils/cellFormatting.ts` | Formula parsing and cell formatting are explicitly out of scope |
| `documentation/Software Project Proposal.md`, [Software Requirements Specifications (SRS).md](<./Software Requirements Specifications (SRS).md>), [Technical Specifications.md](<./Technical Specifications.md>) | **Reference-only specifications, consulted as authoritative sources and never edited.** These three must never appear in the diff |

One qualification, so this list is read accurately: `frontend/src/services/collaboration.ts`
was on a list of this kind in the plan and **does** now appear in the diff. It moved to section
A under `DEV-8`. It is named here only to record that the move was deliberate and authorized
rather than overlooked.

The `SECU-001` requirement identifiers referenced by the compliance discussion in
[SECURITY.md](../SECURITY.md) — including `SECU-001-02` for at-rest encryption and
`SECU-001-05` for audit logging — are defined in
[Software Requirements Specifications (SRS).md](<./Software Requirements Specifications (SRS).md>).
[Technical Specifications.md](<./Technical Specifications.md>) uses unnumbered headings
(`SECURITY CONSIDERATIONS`, `DATA SECURITY`, `SECURITY PROTOCOLS`) and should be cited by
heading name rather than by section number.
