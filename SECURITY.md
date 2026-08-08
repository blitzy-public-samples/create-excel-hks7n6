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

| Control | Implemented in | Effect |
|---------|---------------|--------|
| Authentication on every endpoint | `backend/app/api/*.py`, `backend/app/core/security.py` | All five routes resolve `Depends(get_current_user)`, which verifies a Firebase ID token. A request without a valid token is refused with `401` and a `WWW-Authenticate: Bearer` challenge. |
| Server-side identity verification | `backend/app/core/security.py` | The ID token is verified against Google's published signing keys with the project pinned explicitly, so identity is not taken from a client assertion. |
| Client credential attachment | `frontend/src/services/api.ts` | An axios request interceptor reads the token from the Firebase SDK per request, so it survives a page reload. It is removed from a failed request before any retry. |
| No public object ACLs | `backend/app/services/file_storage.py`, `infrastructure/terraform/main.tf` | Uploads are returned as expiring V4 signed URLs. The uploads bucket has uniform bucket-level access and `public_access_prevention = "enforced"`, so a public ACL cannot be granted even by code. |
| Explicit CORS allow-list | `backend/app/main.py`, `backend/app/core/config.py` | `allow_methods` is `GET, POST, PUT, OPTIONS` and `allow_headers` is `Authorization, Content-Type` — no wildcards. Origins come from `ALLOWED_ORIGINS`, validated against a single grammar. |
| Database transport encryption | `backend/app/db/database.py`, `infrastructure/terraform/main.tf` | The client passes `sslmode` (only `require`, `verify-ca` and `verify-full` are accepted) and Cloud SQL is `ENCRYPTED_ONLY`, so the server refuses an unencrypted connection. |
| Security response headers | `backend/app/core/security_headers.py` and three further delivery points | Six header names on every response, including errors and CORS preflight. See below. |
| HTTPS at the edge | `infrastructure/terraform/main.tf`, `scripts/deploy.sh` | A managed certificate and a 443 listener, with port 80 serving only a redirect. |
| Request throttling | `backend/app/core/rate_limit.py` | A global per-client ceiling (`600/minute`) plus a tighter budget for `POST`/`PUT`/`PATCH`/`DELETE` (`120/minute`), returning `429` with `Retry-After`. Counting uses a shared store so the quota does not multiply per worker. |
| Firestore authorization | `firestore.rules` | Document-level `read`, `create`, `update` and `delete` rules on `/workbooks/{workbookId}`, keyed on the immutable Firebase UID. Any unmatched path is denied by the platform default. |
| Cloud Function invoker identity | `scripts/deploy.sh`, `infrastructure/terraform/main.tf` | `--allow-unauthenticated` is not used; any `allUsers` invoker binding is revoked and the revocation is confirmed, failing the deployment if either read fails. |
| Configuration-governed token lifetime | `backend/app/core/security.py` | Lifetime comes from an explicit argument if given, otherwise `ACCESS_TOKEN_EXPIRE_MINUTES`. |
| Pinned dependency manifest | `backend/requirements.txt` | Every direct and transitive version is exact-pinned, holding `python-jose` above the CVE-2024-33663 fix boundary. |
| Dependency audit in CI | `.github/workflows/ci.yml` | The `security-checks` job audits the manifest on every push and pull request and fails on any advisory outside the recorded baseline. |

Middleware registration order is itself a control. In `backend/app/main.py` the header
middleware is added **last**, making it outermost, which is what puts the headers on `401`,
`429` and CORS preflight responses rather than only on successful ones.

## HTTP response headers

Six header names. These are the canonical values; the four delivery points render the same set.

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

Thirteen directives, in this order:

```
default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none';
form-action 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self';
style-src-attr 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:;
connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com
https://firestore.googleapis.com https://firebaseinstallations.googleapis.com;
upgrade-insecure-requests
```

`connect-src` carries the four Google endpoints the single-page application calls directly. When
a separate API origin is configured it is appended as one further source; when one origin serves
both the application and the API, `'self'` already covers it.

The policy is delivered at four points. The three header-delivered copies render from the same
two inputs — the API origin and the report-only mode — while the document's meta policy is fixed
and governs loading only:

| Delivery point | Renders from |
|----------------|-------------|
| API responses | `backend/app/core/security_headers.py` |
| Frontend container | `infrastructure/docker/nginx.conf`, substituting `CSP_HEADER_NAME` and `CSP_CONNECT_SRC_API` |
| Load-balancer edge | the `security_response_headers` local in `infrastructure/terraform/main.tf` |
| Compiled document | `frontend/public/index.html` — a fixed loading policy naming neither `connect-src` nor `default-src`, so it cannot constrain the API |

**Two consequences worth knowing before changing any of them.** A `<meta>` element cannot carry
`Content-Security-Policy-Report-Only`, so the document's policy is *always enforced* and
`csp_report_only` does not reach it. And a browser applies every policy it receives, so the
effective policy is the **intersection** of those delivered — widening one delivery point alone
does not widen the result.

## Operational switches

Each is a configuration value, documented in [`.env.example`](./.env.example). Those read per
request take effect on the next request; those read when middleware is constructed need a
process restart.

| Switch | Default | Read |
|--------|---------|------|
| `auth_enforcement_enabled` | `true` | per request |
| `auth_token_verifier` | `firebase` | per request |
| `rate_limit_enabled` | `true` | at construction |
| `rate_limit_default` / `rate_limit_write` | `600/minute` / `120/minute` | at construction |
| `csp_report_only` | `false` | at construction |
| `db_sslmode` | `require` | at engine construction |

Every default is the secure position, so a missing value fails closed.

**`auth_enforcement_enabled=false` is break-glass only and must never be set in production.**
Its precise effect: it skips *signature, expiry and revocation verification only*. A request
must still carry an `Authorization: Bearer <token>` header — the OAuth2 scheme refuses it with
`401` before this code runs — the token must still parse well enough for its claims to be read,
and those claims must still name a user that exists in the local database. So endpoints are
**not** reachable without a token; they are reachable with an *unverified* one, which anyone able
to mint arbitrary claims for a known user's email address can produce. Every admitted bypass is
logged at warning level and its response carries `X-Auth-Enforcement-Bypassed: true`.

## Residual risks

Open, and stated rather than implied.

1. **An authenticated user can read another user's workbook.** No route handler filters by
   owner. `Workbook.owner_id` exists but is never consulted, and adding an ownership check needs
   a domain service layer that does not exist. Enforcing authentication turned an anonymous leak
   into an authenticated, attributable cross-tenant read — an improvement, not a complete access
   control. **This is the highest-priority open item.**
2. **`sslmode=require` does not verify server identity.** It defeats passive interception but not
   an active machine-in-the-middle. `verify-full` needs CA material distributed to the client;
   the Cloud SQL connector needs a new dependency and new connection code.
3. **No dependency advisory is currently fixable.** The audit reports 14 advisories across 9
   packages. Every published fix requires Python 3.10 or newer, and `ecdsa` PYSEC-2026-1325 has
   no published fix at all. `infrastructure/docker/Dockerfile.backend` pins `python:3.9-slim`, so
   **upgrading the Python runtime is the single highest-value security task in this repository**;
   until it happens the pins deliver reproducibility, not remediation.
4. **The Firestore rules deny all browser access until documents carry `ownerUid`.**
   `backend/app/services/real_time_sync.py` writes workbook documents without owner or
   collaborator fields. This is the correct fail-closed outcome and it regresses nothing
   measurable, because the browser Firestore path does not currently function. Populating
   `ownerUid` is required before real-time collaboration can work.
5. **Signed URLs require a privilege that cannot be narrowed by IAM conditions.** Signing through
   the IAM `signBlob` API needs `iam.serviceAccountTokenCreator`, and a holder of it can sign on
   behalf of other service accounts in the project. The grant is scoped to a single dedicated
   signer account and no key file ships in any image, but the escalation surface is real. Serving
   private objects through an authenticated backend proxy would avoid it entirely.
6. **Token revocation is not checked.** Verification confirms signature, expiry and issuer. A
   token revoked before its expiry is still accepted, because checking revocation costs an
   extra read per request.
7. **The static assets bucket is directly reachable.** It grants read to `allUsers` so the load
   balancer can serve it, which also leaves every published object readable at
   `https://storage.googleapis.com/<bucket>/<object>`. That path bypasses the load balancer, so
   it carries none of the response headers above and is not redirected to HTTPS. Nothing secret
   may be published there.
8. **The controls are verified by tests, not by a running deployment.** The backend does not
   currently start — see the Known blockers section of the [README](./README.md) — so every
   control above is evidenced by `backend/tests/test_security.py` and by assertions against the
   configuration and infrastructure files, not by observed production traffic.
9. **No network segmentation, key management or audit logging.** No VPC, subnet, firewall rule or
   Kubernetes NetworkPolicy is declared; there is no customer-managed encryption key, no key
   rotation policy and no audit logging.
10. **Continuous deployment does not trigger.** `.github/workflows/cd.yml` waits on a workflow
    named `Continuous Integration` while `.github/workflows/ci.yml` is named `CI`, so promotion
    is manual. A deployment assumed to be automatic would silently ship nothing.
11. **`terraform validate` fails on a pre-existing defect.** `infrastructure/terraform/outputs.tf`
    references seven resources that no configuration declares, so the infrastructure changes
    cannot be validated end to end without first correcting that file. `main.tf` and
    `variables.tf` validate clean on their own.

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

That suite asserts the `401` on all five routes, rejection of a forged token, the absence of any
public-ACL call, CORS allow and deny behaviour, `sslmode` in the connection arguments, the header
set on success / `401` / `429` / preflight, agreement between all four CSP delivery points, the
`429` threshold, and token lifetime in both directions. Several tests read the Terraform, Nginx,
`index.html` and deployment files directly, so a control removed in one place fails a test rather
than drifting unnoticed.

Note that `python -c "import backend.app.core.security"` is **not** a usable check: constructing
`Settings` performs a live Secret Manager read, so it fails outside a configured Google Cloud
project even with every environment variable set. The suite above stubs that read.

## Related documents

- [Security Decision Log](./documentation/Security%20Decision%20Log.md) — why each control was
  implemented the way it was, with alternatives and risks
- [Security Traceability Matrix](./documentation/Security%20Traceability%20Matrix.md) — each
  vulnerability mapped to its implementation and verification, in both directions
- [Developer Onboarding](./documentation/Developer%20Onboarding.md) — setup, pitfalls, and the
  prioritised list of next tasks
- [`.env.example`](./.env.example) — every configuration key and its accepted values
