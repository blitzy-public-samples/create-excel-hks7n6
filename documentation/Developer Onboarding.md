# Developer Onboarding

From a clean machine to running the test suite, plus the domain context and the traps this
codebase actually contains. Read [Pitfalls](#pitfalls) before you spend time debugging
something — most of what looks broken here is broken for a known reason.

**Set expectations first.** This project is under construction. The backend service does not
currently start, and the frontend does not currently compile. Both have specific, pre-existing
causes recorded below. What *does* work, and what you can develop against today, is the backend
test suite.

---

## Prerequisites

| Tool | Version | Why this version |
|------|---------|------------------|
| Python | **3.9** | `infrastructure/docker/Dockerfile.backend` pins `python:3.9-slim`. Pydantic is held below 2 for it. Verified against 3.9.25. |
| Node.js + npm | any current LTS | Only needed for the frontend. |
| PostgreSQL | **13** | The version Terraform provisions for Cloud SQL. Not needed for the test suite. |
| Terraform | **≥ 1.2** | `infrastructure/terraform/main.tf` declares this floor; the security checks use resource `precondition` blocks, introduced in 1.2. |

Python 3.9 reached end of life in October 2025 and receives no upstream security fixes. Do not
"helpfully" upgrade it in passing — `backend/app/core/config.py` uses the Pydantic v1
`BaseSettings` API, and the upgrade is a project in itself. See [Next tasks](#next-tasks), where
it is item 1.

---

## Backend setup

```bash
python -m venv venv
```

Then install the pinned dependency set:

```bash
# macOS / Linux
venv/bin/pip install -r backend/requirements.txt

# Windows PowerShell
.\venv\Scripts\pip install -r backend\requirements.txt
```

`backend/requirements.txt` exact-pins the complete graph — 23 direct requirements and the 66
packages they pull in, 89 lines in total. Install it as a whole; installing a subset re-resolves
the graph and can pick versions that do not work together (see [Pitfalls](#pitfalls)).

### Configuration

Copy `.env.example` to `.env` and fill it in. That file is the authoritative reference for all
21 `Settings` fields. Six are **required and have no default** — a missing one raises a Pydantic
`ValidationError` at import and nothing starts:

| Variable | Notes |
|----------|-------|
| `PROJECT_ID` | Google Cloud project; used to build the Secret Manager resource path |
| `DATABASE_URL` | Validated first, then **overwritten** by the Secret Manager value |
| `REDIS_URL` | Same overwrite behaviour |
| `SECRET_KEY` | Same overwrite behaviour |
| `ALGORITHM` | Not overwritten |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Not overwritten |

The three overwritten ones must still be *present* to pass validation, because
`Settings.__init__` calls `super().__init__()` before it replaces them. That surprises people;
it is not a mistake.

> **`.env` is not ignored by git.** There is no `.gitignore` in this repository and nothing
> excludes `.env` — `git check-ignore .env` exits 1. Exclude it in your own clone **before** you
> put real values in it:
>
> ```bash
> echo ".env" >> .git/info/exclude
> ```
>
> `.git/info/exclude` is local to your clone and is not committed, so this protects you without
> changing the repository.

---

## Running the tests

This is the part of the backend that is verified working:

```bash
# macOS / Linux
PYTHONPATH=. venv/bin/python -m pytest backend/tests/test_security.py -q

# Windows PowerShell
$env:PYTHONPATH="."; .\venv\Scripts\python.exe -m pytest backend\tests\test_security.py -q
```

Expect every test to pass — **119** of them at the time of writing — and 7 warnings. The warnings are pre-existing: four Google
`FutureWarning`s about Python 3.9 being end-of-life, one SQLAlchemy `MovedIn20Warning` from
`backend/app/db/models.py`, and two FastAPI `DeprecationWarning`s about `on_event`.

Two things about this command matter:

- **`PYTHONPATH=.` is required.** There is no `__init__.py` anywhere under `backend/` and no
  installable package metadata, so imports of the form `backend.app.…` only resolve when the
  repository root is on the path.
- **Name `test_security.py` explicitly.** Collecting `backend/tests/` is interrupted before any
  test runs; see [Pitfalls](#pitfalls). Add `--continue-on-collection-errors` to run the suite
  and see the collection errors together — that reports `119 passed, 3 errors`.

`backend/tests/conftest.py` is what makes any of this possible: it populates the required
settings, stubs the Secret Manager client, binds an in-memory SQLite session over
`app.dependency_overrides[get_db]`, and supplies the module attributes the application imports
but the package layout does not provide.

---

## Frontend setup

```bash
cd frontend
npm install
```

Use `npm install`, **not** `npm ci` — no `package-lock.json` is committed.

The frontend does not compile as committed. `frontend/package.json` is incomplete: it declares
neither `react-scripts` (which its own `start`, `build` and `test` scripts invoke) nor
`@reduxjs/toolkit`, `react-router-dom`, `firebase`, `mathjs` or `date-fns`, all of which the
source imports. To get a working tree locally without editing the manifest, install them in a
**single** command — a second `--no-save` install prunes the packages the first one added:

```bash
npm install --no-save react-scripts @reduxjs/toolkit react-router-dom firebase mathjs date-fns @types/node@18
```

Pin `@types/node` to 18.x: a newer one emits many syntax errors against the pinned
TypeScript 4.9.5.

Two further resolution problems remain after that, and both need a source or config change
rather than an install: several modules import across tiers (`backend/app/schema/…`) or through
`@/…` aliases, while `frontend/tsconfig.json` sets `baseUrl` to `src` and maps only
`@components/*`, `@utils/*`, `@hooks/*`, `@services/*` and `@types/*` — the `@/` prefix form is
not mapped.

---

## Domain context

A spreadsheet application in three tiers, plus managed Google Cloud services.

```
Browser (React SPA)
  |  signs in directly ------------------> Firebase Auth / Identity Platform
  |  subscribes directly ----------------> Firestore          <-- no backend in this path
  |  REST + Bearer ID token
  v
FastAPI on GKE
  |--> Cloud SQL PostgreSQL 13   (SQLAlchemy, TLS required)
  |--> Cloud Storage             (uploads, expiring signed URLs)
  |--> Secret Manager            (DATABASE_URL, REDIS_URL, SECRET_KEY)
  |--> Firestore                 (server client library, bypasses rules)
```

**The one structural fact to internalise:** the browser reaches Firestore *directly*, without
passing through the backend. No server-side check can mediate that path, which is why
`firestore.rules` exists and is the only control on it. It is also why the backend cannot be
broken by those rules — server client libraries bypass Firestore rules entirely and authenticate
through Application Default Credentials.

### Identity, and one deliberate asymmetry

The browser signs in with Firebase and sends the resulting ID token as
`Authorization: Bearer <token>`. The backend verifies it with the Firebase Admin SDK against a
pinned project, then resolves the local `User` **by the token's verified `email` claim**.

The Firestore rules, by contrast, key on `request.auth.uid`. That asymmetry is deliberate: UIDs
never change and emails can, so UID is the better key — but `User` has no `firebase_uid` column
and this repository has no migration tooling, so `email` is the only mapping available without a
schema change. Adding `firebase_uid` is [next task](#next-tasks) item 2.

### The HTTP surface

Five endpoints, mounted at the root with no prefix, all requiring authentication:

| Method | Path |
|--------|------|
| GET | `/workbooks` |
| POST | `/workbooks` |
| GET | `/workbooks/{workbook_id}/worksheets` |
| PUT | `/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells` |
| POST | `/workbooks/{workbook_id}/share` |

Note what authentication does **not** yet do: no handler filters by owner, so an authenticated
user can still address another user's `workbook_id`. That is a known open risk, recorded in
[SECURITY.md](../SECURITY.md), and it is next task item 3.

---

## Operator prerequisites for a real deployment

None of these are in code. A deployment fails without them.

1. **Create three Secret Manager secrets** in `PROJECT_ID`, each with a `latest` version:
   `DATABASE_URL`, `REDIS_URL` and `SECRET_KEY`. This repository creates no secrets, and
   `Settings.__init__` reads all three on **every** construction.
2. **Grant `roles/iam.serviceAccountTokenCreator`** on the dedicated signer service account *to*
   the API runtime service account, and make the runtime account reachable from the pod through
   GKE Workload Identity. Without it, signed-URL generation fails with a missing-private-key
   error. The two accounts must be **different** — a Terraform precondition refuses equal
   addresses — and the backend's `signer_service_account` setting must name the signer.
3. **Point DNS at the load balancer address** and wait for the managed certificate to become
   `ACTIVE` before cutting the 443 listener over. Managed certificates need DNS validation and
   are not instant.
4. **Keep `ALLOWED_ORIGINS` consistent** with the Terraform `allowed_origins` list and with
   `google_identity_platform_config.authorized_domains`, which still holds the placeholder
   `example.com`. If they disagree, sign-in and CORS disagree about which origins are legitimate.
5. **Supply the Kubernetes manifests.** `scripts/deploy.sh` applies `deployment.yaml` and
   `service.yaml` from a manifest directory that this repository does not contain.

---

## Pitfalls

Each of these cost real debugging time. They are ordered by how likely you are to hit them.

**Importing almost any backend module calls Google Cloud.** `Settings.__init__` performs three
live Secret Manager reads. So `python -c "import backend.app.core.security"` fails outside a
configured project *even with all six environment variables set* — the error is a Secret Manager
API error, not a Python one. Use the test suite, which stubs it.

**The database engine is bound at import.** `backend/app/db/database.py` calls `create_engine(...)`
at module scope, so an unparseable `DATABASE_URL` fails at *import* time rather than on first
connection, and the traceback points at the import, not at your query.

**The backend service does not start.** `uvicorn backend.app.main:app` fails at import for two
independent reasons:

1. There is no `__init__.py` anywhere under `backend/`, so `backend.app.db` is a namespace
   package that exports nothing. The four route modules do `from backend.app.db import get_db`,
   but `get_db` is defined in `backend/app/db/database.py` — giving
   `ImportError: cannot import name 'get_db' from 'backend.app.db'`.
2. `backend/app/main.py` imports `init_db` from `backend/app/db/database.py`, which does not
   define it.

Also note the container command is wrong independently of that: `Dockerfile.backend` runs
`uvicorn main:app`, but the application object is at `backend.app.main:app`.

**Three test modules cannot be collected.** `test_api.py`, `test_calculation_engine.py` and
`test_collaboration.py` import `app.main`, `calculation_engine` and `backend.collaboration`
respectively — none of which exist. Collection of the directory is interrupted before any test
runs. This is the pre-existing baseline: exactly three collection errors, and any *fourth* is a
regression you introduced.

**Pydantic must stay below 2.** `backend/app/core/config.py` uses `from pydantic import
BaseSettings`, which Pydantic 2 moved into a separate `pydantic-settings` distribution. A v2
resolution raises `ImportError` on the first line of the configuration module.

**`bcrypt` must stay at 4.0.1.** `passlib` 1.7.4 breaks against `bcrypt` 5.x: it reports
`AttributeError: module 'bcrypt' has no attribute '__about__'` and then raises
`ValueError: password cannot be longer than 72 bytes` on any hash. Since `passlib` builds its
`CryptContext` at module scope, an unpinned `bcrypt` makes the security module's password
machinery unusable.

**No dependency advisory here is currently fixable.** The audit reports 14 advisories across 9
packages, and every published fix requires Python 3.10 or newer (`ecdsa` PYSEC-2026-1325 has no
fix at all). The CI gate therefore runs in *delta* mode against a recorded baseline and fails
only on something new. If you change `backend/requirements.txt`, re-measure and update that
baseline in `.github/workflows/ci.yml`.

**`terraform validate` fails before you touch anything.** `infrastructure/terraform/outputs.tf`
references seven resources no configuration declares. `main.tf` and `variables.tf` validate clean
on their own. To check your own Terraform changes:

```bash
terraform -chdir=infrastructure/terraform init -backend=false
terraform -chdir=infrastructure/terraform validate
# then remove the init artefacts - the lock file is deliberately not committed:
rm -rf infrastructure/terraform/.terraform infrastructure/terraform/.terraform.lock.hcl
```

Confirm the errors you see are the same seven in `outputs.tf` and nothing in the file you edited.

**Continuous deployment never triggers.** `.github/workflows/cd.yml` waits on a workflow named
`Continuous Integration`; `.github/workflows/ci.yml` is named `CI`. Promotion is manual. The
`build` job in `ci.yml` is also written for a Node backend that does not exist here — it runs
`npm ci` at the repository root, where there is no `package.json`, and `npm run type-check`,
which is not a defined script. The `security-checks` job in the same file does work.

**Shell scripts on Windows.** The working tree uses CRLF. WSL `bash` cannot read Windows absolute
paths and will report phantom syntax errors in `.sh` files. Use Git for Windows' bash:

```powershell
& "C:\Program Files\Git\bin\bash.exe" -n scripts/deploy.sh
```

---

## How to extend

### Add a protected endpoint

Follow the existing shape exactly — for example `backend/app/api/worksheets.py`:

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: enforce authentication — previously no route required a valid token
@router.get('/your/path')
def your_handler(db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    ...
```

`current_user` is a dependency parameter, so it does not appear in the request contract. Then add
a test asserting the route returns `401` without a token — `test_security.py` drives all five
existing routes from one list, so extend that list rather than writing a new test from scratch.

### Extend the Firestore rules

`firestore.rules` is built from small named predicates — `isSignedIn()`, `isOwner()`,
`isCollaborator()`, `ownerUidUnchanged()`, `withinCollaboratorWriteScope()` and others. Compose
those rather than inlining new conditions, and remember two platform rules: an unmatched path is
denied by default, so you never need a catch-all deny; and **rules are not filters** — a
collection query that *could* return a document the rules forbid fails entirely rather than
returning a subset.

Validate with the emulator before deploying:

```bash
firebase emulators:exec --only firestore "<your assertions>"
```

### Change the throttling tiers

`rate_limit_default` and `rate_limit_write` are configuration, not code. Widen them rather than
editing `backend/app/core/rate_limit.py`. Be careful with the write tier: the cell-update
endpoint fires on every edit, so it must accommodate real autosave cadence. Note also that
throttling counts *all* outcomes including `401`s — deliberate, for credential-stuffing defence,
and another reason the budget must be generous.

### Where rationale goes

Two conventions are enforced here:

- **Rationale belongs in the [Security Decision Log](./Security%20Decision%20Log.md), never in a
  code comment.** A comment states the threat closed or the contract enforced; the log explains
  why that approach was chosen over the alternatives. Terraform `description` and `error_message`
  strings are treated as user-facing documentation and are exempt.
- **A security control change needs a matching test.** Several tests in `test_security.py` read
  the Terraform, Nginx, `index.html` and deployment files directly, and one asserts that
  `SECURITY.md` records the same header values the code emits. A control weakened in one place
  fails a test rather than drifting quietly — including a documentation-only drift.

---

## Next tasks

Discovered while doing the security work, out of its scope, and worth doing. Ordered by value.

1. **Upgrade the Python runtime off 3.9.** This is the highest-value security task in the
   repository. It is not hygiene: *every* one of the 14 outstanding dependency advisories has a
   published fix that requires Python 3.10 or newer, so none can be closed until this happens.
   Doing it means migrating `config.py` to Pydantic 2 with `pydantic-settings`, or pinning
   `pydantic<2` on a newer interpreter. Removing the last entry from the CI ignore baseline is
   the exit criterion.
2. **Add a `firebase_uid` column to `User`** and key identity on it instead of `email`. Needs
   migration tooling, which the repository does not have — `scripts/deploy.sh` still carries an
   unresolved "specify the migration tool" placeholder.
3. **Scope every query by owner.** Close the authenticated cross-tenant read: no handler consults
   `Workbook.owner_id`. Doing it properly means building the absent service layer
   (`WorkbookService`, `WorksheetService`, `CellService`, `CollaborationService`), which the
   handlers already import.
4. **Make the backend importable and startable.** Add `__init__.py` files (or correct the
   imports), define `init_db`, and fix the container command to `backend.app.main:app`.
5. **Populate `ownerUid` on Firestore documents.** `backend/app/services/real_time_sync.py`
   writes workbook documents with no owner or collaborator field, so the rules correctly deny all
   browser access until this exists. Real-time collaboration cannot work before it.
6. **Fix `frontend/package.json`** and commit a `package-lock.json` — that unblocks the frontend
   build, `npm ci` and `npm audit` in one step.
7. **Repair `infrastructure/terraform/outputs.tf`** so `terraform validate` passes and
   infrastructure changes can be machine-checked.
8. **Correct the CI/CD wiring**: align the workflow names so CD triggers, and replace the Node
   `build` job with one that actually runs `pytest`.
9. **Add a collaborators table.** `POST /workbooks/{id}/share` has no persistence model behind
   it, and `backend/tests/test_collaboration.py` documents the intended owner / shared-user /
   admin semantics against an `AccessControl` class that does not exist.
10. **Strengthen transport**: `sslmode=verify-full` or the Cloud SQL connector, so the server's
    identity is verified and not merely the channel encrypted.
11. **Stop leaking exception text.** `cells.py` and `collaboration.py` return `str(e)` in HTTP
    error details.
12. **Harden the container**: add a `.dockerignore` (the backend image's `COPY . .` currently
    ships `.git`), run as a non-root `USER`, add a `HEALTHCHECK`, and pin base images by digest.
13. **Tag images per release.** They are tagged `:latest` and overwritten, so there is no image
    to roll back to.
14. **Replace the long-lived CI service-account key** (`secrets.GCP_SA_KEY`) with Workload
    Identity Federation, and remove the interactive `gcloud auth login` from `deploy.sh`.
15. **Add container image scanning and Dependabot**, neither of which exists.
16. **Codify network segmentation and add audit logging** — no VPC, subnet, firewall rule or
    NetworkPolicy is declared, and there is no audit logging or customer-managed encryption key.
17. **Rewrite `scripts/setup_dev_environment.sh`.** It issues Django `manage.py migrate` and
    `runserver` commands against a FastAPI application and installs from a root
    `requirements.txt` that does not exist. Follow this document instead.

---

## Related documents

- [README](../README.md) — project overview and the current blocker list
- [SECURITY.md](../SECURITY.md) — controls, canonical response headers, residual risks
- [Security Decision Log](./Security%20Decision%20Log.md) — why each control is built the way it is
- [Security Traceability Matrix](./Security%20Traceability%20Matrix.md) — vulnerability to
  implementation to verification, both directions
- [Technical Specifications](./Technical%20Specifications.md) — system design reference
- `.env.example` — every configuration key, its accepted values and its read timing
