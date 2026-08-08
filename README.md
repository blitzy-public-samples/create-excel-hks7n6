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
firebase-admin, slowapi, google-cloud-storage / -firestore / -secret-manager, Celery, Redis,
NumPy, pandas, pytest.

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

**All five require authentication.** Each resolves `Depends(get_current_user)`, which verifies
a Firebase ID token, so a request without a valid `Authorization: Bearer <token>` header is
refused with `401` and a `WWW-Authenticate: Bearer` challenge. The browser client attaches the
token automatically through an axios interceptor in `frontend/src/services/api.ts`.

## Prerequisites

- **Python 3.9** — the version [`infrastructure/docker/Dockerfile.backend`](./infrastructure/docker/Dockerfile.backend)
  pins. Pydantic is held below 2 for it, and it is also why no dependency advisory in this
  project is currently fixable; see [Known blockers](#known-blockers).
- **Node.js and npm** — for the frontend.
- **PostgreSQL 13** — the version Terraform provisions for Cloud SQL.
- **Google Cloud access** — required to *run* the backend, because startup reads Secret
  Manager. Not required to run the test suite.

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

> **`.env` is not ignored by git.** This repository has no `.gitignore` and nothing excludes
> `.env`, so a filled-in `.env` will be offered for commit. Exclude it locally before you
> create it — for example by adding `.env` to `.git/info/exclude`.

### Running the test suite

This is the part of the backend that is verified working:

```bash
PYTHONPATH=. venv/bin/python -m pytest backend/tests/test_security.py -q
```

`backend/tests/conftest.py` supplies the required settings, stubs the Secret Manager client and
binds an in-memory SQLite session, so no Google Cloud access is needed. Name that module
explicitly rather than collecting `backend/tests/` — see [Known blockers](#known-blockers).

### Frontend

```bash
cd frontend
npm install
```

Use `npm install`, not `npm ci`: no `package-lock.json` is committed.

## Known blockers

These are pre-existing and are recorded here rather than hidden. None is introduced by the
security work described in [SECURITY.md](./SECURITY.md).

**The backend does not start.** `uvicorn backend.app.main:app` fails at import, for two
independent reasons:

1. There is no `__init__.py` anywhere under `backend/`, so `backend.app.db` is a namespace
   package that exports nothing. The four route modules do `from backend.app.db import get_db`,
   but `get_db` is defined in `backend/app/db/database.py`, giving
   `ImportError: cannot import name 'get_db' from 'backend.app.db'`.
2. `backend/app/main.py` imports `init_db` from `backend/app/db/database.py`, which does not
   define it.

The test suite is unaffected because `conftest.py` resolves both for the tests it runs.

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
`react-router-dom`, `firebase`, `mathjs` or `date-fns`, all of which the source imports.
Several modules also import across tiers (`backend/app/schema/...`) or through `@/...` aliases
that `frontend/tsconfig.json` does not map.

**`terraform validate` fails.** `infrastructure/terraform/outputs.tf` references seven
resources that no configuration declares — `google_storage_bucket.raw_data`, `.processed_data`
and `.model_artifacts`, `google_cloudfunctions_function.data_ingestion`, `.data_processing` and
`.model_training`, and `google_firestore_database.main`. `main.tf` and `variables.tf` themselves
validate clean.

**Continuous deployment does not trigger.** `.github/workflows/cd.yml` waits on a workflow named
`Continuous Integration`, but `.github/workflows/ci.yml` is named `CI`, so promotion is a manual
operation. The `build` job in `ci.yml` is also written for a Node backend that does not exist
here; the `security-checks` job in the same file does work.

## Security

Authentication is enforced on every endpoint, uploads are served through expiring signed URLs
rather than public object ACLs, CORS is an explicit allow-list, the database connection requires
TLS, security response headers are emitted on both tiers, requests are rate limited, and
Firestore carries document-level authorization rules.

**No compliance standard is claimed as attained.** [SECURITY.md](./SECURITY.md) lists the
controls that exist, the residual risks that remain open, and how to report a vulnerability.

## Documentation

| Document | What it covers |
|----------|----------------|
| [Developer Onboarding](./documentation/Developer%20Onboarding.md) | Clean machine to running tests, domain context, pitfalls, how to extend, next tasks |
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
  in one place fails a test rather than drifting quietly.

## License

No license file is present in this repository, so the terms of use are unstated. Add a `LICENSE`
file before distributing.
