# Excel Clone

A web-based spreadsheet application: a React single-page application in TypeScript over a
Python FastAPI service, deployed on Google Cloud.

> **Project status: under construction.** Parts of this repository do not yet run. The
> sections below separate what is verified working from what is blocked, and name the reason
> for each blocker. Read [Known blockers](#known-blockers) before setting up.

## Architecture

Two tiers plus managed Google Cloud services.

| Tier | What it is | Where it runs |
|------|-----------|---------------|
| Frontend | React 18 + TypeScript single-page application, Redux for state | Compiled to static files and published to a Cloud Storage bucket, served through a global HTTPS load balancer |
| Backend | Python FastAPI service, SQLAlchemy over PostgreSQL 13 | Container on Google Kubernetes Engine |
| Identity | Firebase Authentication / Identity Platform | The browser signs in directly; the backend verifies the resulting ID token |
| Collaboration | Firestore | The browser subscribes **directly**, without going through the backend |
| Object storage | Cloud Storage | Uploaded workbooks, reached through time-limited signed URLs |
| Configuration | Secret Manager | `DATABASE_URL`, `REDIS_URL` and `SECRET_KEY` are read at startup |

Because the browser reaches Firestore directly, no backend check can mediate that path.
Firestore security rules in [`firestore.rules`](./firestore.rules) are the only control on it.

## Technology stack

Taken from the committed manifests, not from intent.

**Backend** — pinned in [`backend/requirements.txt`](./backend/requirements.txt): Python 3.9,
FastAPI 0.125.0, Starlette 0.49.3, Uvicorn 0.39.0, SQLAlchemy 1.4.54, psycopg2-binary,
Pydantic **1.x** (`config.py` uses the v1 `BaseSettings` API, so Pydantic 2 will not work),
firebase-admin, `limits` (both throttling tiers — `slowapi` is deliberately **not** pinned because
nothing imports it), google-cloud-storage / -firestore / -secret-manager, Celery, Redis,
NumPy, pandas, pytest. 88 exact pins, 22 of them direct.

**Frontend** — declared in [`frontend/package.json`](./frontend/package.json): React,
React-Redux, Redux, redux-thunk, axios, chart.js, react-chartjs-2, Tailwind CSS, Formik, Yup,
TypeScript. See [Known blockers](#known-blockers) — this manifest is incomplete.

**Infrastructure** — Terraform (`>= 1.2`) in [`infrastructure/terraform`](./infrastructure/terraform),
Docker images in [`infrastructure/docker`](./infrastructure/docker), deployment driven by
[`scripts/deploy.sh`](./scripts/deploy.sh).

## Repository layout

```
backend/app/api/          five REST route handlers
backend/app/core/         configuration, authentication, security headers, rate limiting
backend/app/db/           SQLAlchemy engine, session and models
backend/app/services/     file storage, calculation engine, real-time sync
backend/tests/            test suite
frontend/src/             components, Redux store, services, utilities
infrastructure/terraform/ Google Cloud resources
infrastructure/docker/    backend and frontend images, Nginx configuration
scripts/                  deployment and development-environment scripts
documentation/            specifications and the security documents listed below
firestore.rules           document-level authorization for the collaboration store
```

## HTTP API

Five endpoints, mounted at the application root with no prefix:

| Method | Path |
|--------|------|
| GET | `/workbooks` |
| POST | `/workbooks` |
| GET | `/workbooks/{workbook_id}/worksheets` |
| PUT | `/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells` |
| POST | `/workbooks/{workbook_id}/share` |

**All five require authentication while `auth_enforcement_enabled` is true** — its default, and
the mandatory production setting. Each resolves `Depends(get_current_user)`, which verifies a
Firebase ID token, so a request without a valid `Authorization: Bearer <token>` header is refused
with `401` and a `WWW-Authenticate: Bearer` challenge. The browser client attaches the token
automatically through an axios interceptor in `frontend/src/services/api.ts`.

Setting that switch false is break-glass only and reopens all five routes to any caller able
to present a token, because the presented token's signature, expiry and revocation state stop
being checked. It does **not** open a credential-less path: a request with no `Authorization` header
is still refused by the bearer scheme before any application code runs, whatever the switch says.
It must never be set in production. See
[SECURITY.md](./SECURITY.md#operational-switches) for its exact effect and for why it is not the
remedy for a client-side lockout.

## Prerequisites

Enough to install the backend and run its tests. The full list, split by task and including the
deployment tooling, is in the
[onboarding guide](./documentation/Developer%20Onboarding.md#prerequisites).

- **Python 3.9.2 or newer, within the 3.9 series** — `infrastructure/docker/Dockerfile.backend`
  pins `python:3.9-slim`, and Pydantic is held below 2 for it. The `.2` floor is real:
  `cryptography` excludes 3.9.0 and 3.9.1, so `backend/requirements.txt` will not resolve on
  them. Verified against 3.9.25. This pin is also why no dependency advisory in this project is
  currently fixable; see [Known blockers](#known-blockers).
- **Node.js and npm** — for the frontend. Any current LTS; treat the repository's Node 14 pins as
  stale.
- **PostgreSQL 13** — only to connect to Cloud SQL by hand. The version Terraform provisions.
- **Google Cloud access** — required to *run* the backend, because startup reads Secret
  Manager. Not required to run the test suite.
- **To deploy** — Terraform ≥ 1.2, the Google Cloud SDK (`gcloud` **and** `gsutil`), `kubectl`,
  Docker, the Firebase CLI, a JDK 21 for the Firestore emulator, and a POSIX shell.

## Setup

Clone the repository and change into it, then:

### Backend

```bash
python -m venv venv
venv/bin/pip install -r backend/requirements.txt     # Windows: venv\Scripts\pip
```

Copy [`.env.example`](./.env.example) to `.env` and fill it in. That file is the authoritative
reference for every setting: six are required with no defaults, the rest have defaults in
`backend/app/core/config.py`.

> **`.env` is ignored; `.env.example` is not.** The root `.gitignore` covers `.env` and
> `.env.*` with an explicit `!.env.example` exception, so a filled-in `.env` is not offered for
> commit. Check it with `git check-ignore -v .env`. The same file ignores service-account keys,
> certificates and Terraform state.

### Running the test suite

This is the part of the backend that is verified working:

```bash
PYTHONPATH=. venv/bin/python -m pytest backend/tests/test_security.py -q

# Windows PowerShell
$env:PYTHONPATH="."; .\venv\Scripts\python.exe -m pytest backend\tests\test_security.py -q
```

Expect **823 passed, 5 warnings**. `backend/tests/conftest.py` supplies the six required settings
and stubs the Secret Manager client, so no Google Cloud access is needed, and its
`authentication_database` fixture builds a real in-memory SQLite schema and patches the module-level
`get_db` name that the identity lookup calls — the only seam that reaches it, since
`get_current_user` resolves `get_db` as a module attribute rather than as a FastAPI dependency. Name
that module explicitly rather than collecting `backend/tests/` — see
[Known blockers](#known-blockers).

### Frontend

```bash
cd frontend
npm install
```

Use `npm install`, not `npm ci`: no `package-lock.json` is committed.

Then create **`frontend/.env`** — a different file from the backend `.env`, holding the
build-time variables `react-scripts` inlines into the bundle. Without them the application cannot
sign in and refuses to issue API requests. All are public by design; `.env.example` section E is
the authoritative list.

```bash
REACT_APP_API_BASE_URL=http://localhost:8000
REACT_APP_FIREBASE_API_KEY=…
REACT_APP_FIREBASE_AUTH_DOMAIN=…
REACT_APP_FIREBASE_PROJECT_ID=…      # must equal the backend PROJECT_ID
REACT_APP_FIREBASE_APP_ID=…
```

Two constraints the client enforces, and reports when they are broken:

- `REACT_APP_API_BASE_URL` must be an **absolute URL on a different origin** from the one serving
  the application. Neither static edge routes API paths to the API service, so a relative or
  same-origin value fetches the SPA document instead of API data.
- `REACT_APP_FIREBASE_PROJECT_ID` must equal the backend `PROJECT_ID`, which is the one project
  whose ID tokens the API accepts.

Both are covered in full, along with the Content-Security-Policy origin that must match, in
[Developer Onboarding](./documentation/Developer%20Onboarding.md#configure-the-request-seam--do-this-before-you-expect-the-spa-to-work).

The client-side request tests run under `react-scripts`:

```bash
cd frontend && CI=true npx react-scripts test --watchAll=false --testPathPattern=api.test
```

## Known blockers

These are pre-existing and are recorded here rather than hidden. None is introduced by the
security work described in [SECURITY.md](./SECURITY.md).

**The backend does not start, and would not find its tables if it did.** This is the largest open
item in the project — everything the security work delivers is implemented and tested, and none of it
runs in a deployed process until this is closed. Four independent causes:

1. There is no `__init__.py` anywhere under `backend/`, so `backend.app.db` is a namespace
   package that exports nothing. The four route modules — which between them expose five routes — do `from backend.app.db import get_db`,
   but `get_db` is defined in `backend/app/db/database.py`, giving
   `ImportError: cannot import name 'get_db' from 'backend.app.db'`.
2. `backend/app/main.py` imports `init_db` from `backend/app/db/database.py`, which does not
   define it.
3. The route modules import `WorkbookService`, `WorksheetService`, `CellService` and
   `CollaborationService`, none of which exists.
4. No table-creating DDL and no migration tool exist, and `Base.metadata.create_all` is never
   called — so even a process that started would answer every query, authentication included, with
   PostgreSQL reporting that the relation does not exist.

Each part predates the security work: every affected module is byte-identical from the
pre-remediation baseline through the current commit. Fixing it is item 1 of the prioritised list in
[Developer Onboarding](./documentation/Developer%20Onboarding.md).

The test suite is unaffected — but not because `conftest.py` resolves any of this, which an earlier
version of this section claimed. It does not resolve any of it. `test_security.py` never imports
`backend.app.main` or the route modules: it reads them as source text and parses them, and it
exercises the modules that *can* be imported — `security.py`, `config.py`, `database.py`,
`models.py`, the middleware and the storage service — inside probe applications it assembles itself.
`TestKnownResiduals::test_the_application_entry_point_cannot_be_imported` asserts the blocker
directly, so it cannot be quietly forgotten.

**`import backend.app.core.security` cannot be run bare.** Constructing `Settings` performs a
live Secret Manager read, so the import fails outside a configured Google Cloud project even
with all six environment variables set. The test suite is the way to exercise this module.

**Three test modules cannot be collected.** `backend/tests/test_api.py`,
`test_calculation_engine.py` and `test_collaboration.py` each import a module path that does not
exist (`app.main`, `calculation_engine`, `backend.collaboration`). Collection of
`backend/tests/` is interrupted before any test runs, which is why the command above names
`test_security.py` directly. Add `--continue-on-collection-errors` to run both.

**The frontend does not compile.** `frontend/package.json` declares neither `react-scripts`
(which its own `start`, `build` and `test` scripts invoke) nor `@reduxjs/toolkit`,
`react-router-dom`, `firebase`, `mathjs` or `date-fns`, all of which the source imports. Beyond
that, `tsc` reports 59 errors — 30 of them the Redux store's missing typed hooks and default-vs-named
reducer exports, 9 the absent `@/components` and `@/pages` barrel files, and the rest ordinary type
errors in excluded component files. The onboarding guide gives
the [breakdown](./documentation/Developer%20Onboarding.md#why-the-frontend-does-not-compile) and
the pinned recovery install that makes the `api.ts` test suite runnable.

**`terraform validate` fails.** `infrastructure/terraform/outputs.tf` references seven
resources that no configuration declares — `google_storage_bucket.raw_data`, `.processed_data`
and `.model_artifacts`, `google_cloudfunctions_function.data_ingestion`, `.data_processing` and
`.model_training`, and `google_firestore_database.main`. `main.tf` and `variables.tf` themselves
validate clean: CI copies those two files into an empty directory and runs `init -backend=false`
then `validate` there, which reports success, and then asserts the full directory still reports
exactly those seven errors and nothing outside `outputs.tf`. Repairing that file is the only
thing standing between this configuration and an end-to-end validate.

**Continuous deployment does not trigger.** `.github/workflows/cd.yml` waits on a workflow named
`Continuous Integration`, but `.github/workflows/ci.yml` is named `CI`, so promotion is a manual
operation. The `build` job in `ci.yml` is also written for a Node backend that does not exist
here; the `security-checks` job in the same file does work.

## Security

Authentication is enforced on every endpoint, uploads are served through expiring signed URLs
rather than public object ACLs, CORS is an explicit allow-list, the database connection requires
TLS through a validated psycopg2 URL, security response headers are emitted on both tiers, requests
are rate limited, and Firestore carries document-level authorization rules.

Four further controls bound what a single caller can cost the server, each added after the
behaviour was measured under load rather than reasoned about. A list route serves at most 100 rows
and refuses an out-of-range page parameter with a `422` instead of letting it reach SQL. Responses
of 500 bytes or more are compressed, taking the default workbook page from 2,742,333 bytes on the
wire to 36,648. Requests in flight are capped at the database pool's capacity and excess is shed
with a `503` and `Retry-After`, which replaced a 30-second wait ending in a `500` — at concurrency
120 the failure rate went from 47.3% to zero. And a failed cell update or share now reports a fixed
message while the driver text it used to return goes to the log instead.

Read that as a statement about the code. Each control is implemented and each is covered by a test,
but none has run inside the application entry point, because the entry point does not import — see
[Known blockers](#known-blockers). The controls are enforceable rather than enforced, and
[SECURITY.md](./SECURITY.md) says which of them are executed by a test and which are asserted from
the committed configuration.

**No compliance standard is claimed as attained.** [SECURITY.md](./SECURITY.md) lists the
controls that exist, the residual risks that remain open, and how to report a vulnerability.

## Documentation

| Document | What it covers |
|----------|----------------|
| [Developer Onboarding](./documentation/Developer%20Onboarding.md) | Clean machine to running tests; what runs today and every gap blocking a running application; domain context; pitfalls; the full deployment sequence; how to extend a route, the Firestore rules, the throttling tiers and the Content-Security-Policy; next tasks |
| [SECURITY.md](./SECURITY.md) | Reporting process, controls introduced, response headers, residual risks |
| [Security Decision Log](./documentation/Security%20Decision%20Log.md) | Every non-trivial decision with its alternatives, rationale and risks |
| [Security Traceability Matrix](./documentation/Security%20Traceability%20Matrix.md) | Each vulnerability mapped to its implementation and verification, in both directions |
| [Technical Specifications](./documentation/Technical%20Specifications.md) | System design reference |
| [Software Requirements Specification](./documentation/Software%20Requirements%20Specifications%20%28SRS%29.md) | Requirements reference |
| [Software Project Proposal](./documentation/Software%20Project%20Proposal.md) | Scope and intent reference |

## Contributing

There is no `CONTRIBUTING.md` in this repository. Two conventions do apply and are enforced:

- Rationale belongs in the [Security Decision Log](./documentation/Security%20Decision%20Log.md),
  not in code comments. A comment states the threat closed or the contract enforced; the log
  explains why that approach was chosen.
- A security control change needs a matching test in `backend/tests/test_security.py`. Several
  existing tests assert on configuration and infrastructure files directly, so a control removed
  in one place fails a test rather than drifting quietly. **The documentation is checked the same
  way**: `TestOperatorFacingClaims` and `TestDocumentedFactsMatchTheCode` read `SECURITY.md`,
  `README.md`, the onboarding guide, the decision log and the traceability matrix, and assert
  published values against the code that emits them — the header set, the test-case count, the
  Terraform resources named, the recovery install command, and that every test node ID a document
  cites exists. So a published value must be updated in the same commit as the code, and if it is
  not, the suite says so rather than a reader discovering it later. That covers the values those
  tests name; it is not a guarantee that every sentence is checked, so treat it as a safety net
  under the habit rather than a substitute for it.

## License

No license file is present in this repository, so the terms of use are unstated. Add a `LICENSE`
file before distributing.
