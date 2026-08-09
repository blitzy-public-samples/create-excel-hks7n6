# Developer Onboarding

From a clean machine to running the test suite, plus the domain context and the traps this
codebase actually contains. Read [Pitfalls](#pitfalls) before you spend time debugging
something — most of what looks broken here is broken for a known reason.

**Set expectations first.** This project is under construction. The backend service does not
currently start, and the frontend does not currently compile. Both have specific, pre-existing
causes recorded below. What *does* work, and what you can develop against today, is the backend
test suite — **823** cases, all passing, exercising the security controls against the real modules that
implement them.

Be precise about what that means, because it is the difference between a control that exists and a
control that protects anything. Authentication, throttling, the response headers, the signed-URL
change and the database transport contract are all implemented and all tested. None of them has run
inside `backend/app/main.py`, because that module cannot be imported. Closing that is
[next task](#next-tasks) item 1, and it is the prerequisite for every other item on the list.

---

## Prerequisites

Split by what you are actually doing. Only the first group is needed to install the backend and
run its test suite, which is the part of this project that works today.

**To install and test the backend**

| Tool | Version | Why this version |
|------|---------|------------------|
| Python | **3.9.2 or newer, within the 3.9 series** | `infrastructure/docker/Dockerfile.backend` pins `python:3.9-slim`, and Pydantic is held below 2 for it. The `.2` floor is not cosmetic: `cryptography` declares `Requires-Python ">=3.9, !=3.9.0, !=3.9.1"`, so `backend/requirements.txt` does not resolve on 3.9.0 or 3.9.1. Resolved and verified against **3.9.25**, which is what `python:3.9-slim` currently ships. |

**To run the frontend test suite or build the application**

| Tool | Version | Why this version |
|------|---------|------------------|
| Node.js + npm | any current LTS | The repository's own pins say Node 14 (`Dockerfile.frontend`, `ci.yml`, `setup_dev_environment.sh`, `README.md`), which is end-of-life. A current LTS runs the test suite; treat the Node 14 pins as stale rather than as a requirement. |

**To deploy, or to check infrastructure changes**

| Tool | Version | Why it is needed |
|------|---------|------------------|
| Terraform | **≥ 1.2** | `infrastructure/terraform/main.tf` declares this floor; the security checks use resource `precondition` blocks, introduced in 1.2. |
| Google Cloud SDK — `gcloud` **and** `gsutil` | current | `scripts/deploy.sh` uses `gcloud` for every preflight read, for GKE credentials and for the Cloud Function invoker policy, and `gsutil -m rsync` to publish the compiled frontend. `gsutil` ships with the SDK but is a separate binary — check both are on `PATH`. |
| `kubectl` | matching the GKE cluster | `scripts/deploy.sh` applies `deployment.yaml` and `service.yaml`. Installable as `gcloud components install kubectl`. |
| Docker | current | The backend image is built and pushed to `gcr.io/$PROJECT_ID/excel-app-backend:latest`. |
| Firebase CLI | current | `firebase deploy --only firestore:rules` publishes `firestore.rules`, and `firebase emulators:exec` is the only way to check them. Installable as `npm install -g firebase-tools`. |
| Java (JDK) | **21** | The Firestore emulator is a Java program and will not start without a JDK. Any 21 distribution works. |
| Bash | POSIX-compatible | `scripts/deploy.sh` is a `sh` script. On Windows use Git for Windows' bash, **not** WSL bash — see [Pitfalls](#pitfalls). |
| PostgreSQL client | **13** | Only for connecting to Cloud SQL by hand. The version Terraform provisions. Not needed for the test suite. |

**Both runtimes this repository pins are past end of life, and the two need opposite treatment.**

Python 3.9 reached end of life in October 2025 and receives no upstream security fixes, but you
must use it: `backend/app/core/config.py` uses the Pydantic v1 `BaseSettings` API, so do not
"helpfully" upgrade it in passing. The upgrade is a project in itself and is
[next task](#next-tasks) item 1.

Node 14 left support on 30 April 2023 and is pinned in four places —
`infrastructure/docker/Dockerfile.frontend`, the `build` job matrix in `.github/workflows/ci.yml`,
`scripts/setup_dev_environment.sh` and `README.md`. Unlike Python, you should **not** use it: the
`react-scripts` 5 toolchain the sources need does not run on it, which is why the CI security job
pins Node 22 for itself instead of inheriting that matrix. Use Node 22 locally. The four pins are
left alone because moving them is a runtime upgrade rather than a documentation fix.
Python 3.9 reached end of life in October 2025 and receives no upstream security fixes. Do not
"helpfully" upgrade it in passing — `backend/app/core/config.py` uses the Pydantic v1
`BaseSettings` API, and the upgrade is a project in itself. See [Next tasks](#next-tasks), where
it is item 2 — behind only the work that makes the application start at all.

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

`backend/requirements.txt` exact-pins the complete graph — **22 direct requirements and the 66
packages they pull in, 88 pinned lines** across a 165-line file, the remainder being the comments
that record why each constraint exists. Install it as a whole; installing a subset re-resolves the
graph and can pick versions that do not work together (see [Pitfalls](#pitfalls)).

### Configuration

Copy `.env.example` to `.env` and fill it in. That file is the authoritative reference for all
**17 `Settings` fields**, and it is verified to be: a test compares its key set against
`Settings.__fields__` in both directions, so it cannot document a key the application does not
read or omit one it does. Six are **required and have no default** — a missing one raises a
Pydantic `ValidationError` at import and nothing starts:

| Variable | Notes |
|----------|-------|
| `PROJECT_ID` | Google Cloud project; used to build the Secret Manager resource path |
| `DATABASE_URL` | Validated, then **overwritten** by the Secret Manager value — which is validated again on the way in, because the environment value's check says nothing about the secret that replaces it. Both must be a synchronous psycopg2 PostgreSQL URL; see [The database contract](#the-database-contract) |
| `REDIS_URL` | Same overwrite behaviour |
| `SECRET_KEY` | Same overwrite behaviour |
| `ALGORITHM` | Not overwritten |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Not overwritten |

The three overwritten ones must still be *present* to pass validation, because
`Settings.__init__` calls `super().__init__()` before it replaces them. That surprises people;
it is not a mistake.

> **`.env` is ignored, and `.env.example` deliberately is not.** The repository's root
> `.gitignore` covers `.env` and `.env.*` with an explicit `!.env.example` exception, so filling
> in a real `.env` cannot commit it by accident. Confirm it in your clone:
>
> ```bash
> git check-ignore -v .env          # prints the matching rule
> git check-ignore .env.example     # exits 1 - the template stays tracked
> ```
>
> The same file also ignores service-account keys, certificates and Terraform state. This is a
> control rather than tidiness: a credential that reaches history stays reachable after it is
> deleted from the working tree, and the remedy then is rotating the credential, not another
> commit.

---

## Running the tests

This is the part of the backend that is verified working:

```bash
# macOS / Linux
PYTHONPATH=. venv/bin/python -m pytest backend/tests/test_security.py -q

# Windows PowerShell
$env:PYTHONPATH="."; .\venv\Scripts\python.exe -m pytest backend\tests\test_security.py -q
```

Expect every test to pass, with no failures and no errors. A total is deliberately not quoted
here: the suite grows whenever a control is added, so a number written into this guide is wrong
by the next change and tells a newcomer nothing a failing run would not. `pytest` prints the
count; what matters is that the failure count is zero.

A few warnings are expected and pre-existing: Google `FutureWarning`s about Python 3.9 being
end-of-life — one per Google library that checks, so the number moves with the dependency set —
and one SQLAlchemy `MovedIn20Warning` from `backend/app/db/models.py`.

Two things about this command matter:

- **`PYTHONPATH=.` is required.** There is no `__init__.py` anywhere under `backend/` and no
  installable package metadata, so imports of the form `backend.app.…` resolve only when the
  repository root is on the path, and then only as namespace packages.
- **Name `test_security.py` explicitly.** Collecting `backend/tests/` is interrupted before any
  test runs; see [Pitfalls](#pitfalls). Add `--continue-on-collection-errors` to run the suite
  and see the collection errors together — that reports the whole suite passing alongside
  `3 errors`. The three are `test_api.py`, `test_calculation_engine.py` and
  `test_collaboration.py`, and that exact set is asserted by CI, so a fourth error — or the
  disappearance of one of the three — fails the workflow rather than passing unnoticed.

`backend/tests/conftest.py` is what makes any of this possible. Precisely three things, and it is
worth knowing it does **not** do a fourth:

1. It populates the six required settings variables, so `Settings()` validates.
2. It stubs the Secret Manager client, so constructing `Settings` performs no network call.
3. It binds an in-memory SQLite session over `app.dependency_overrides[get_db]`, so tests that
   need a database get one.

It does **not** supply the module attributes the application imports but the package layout does
not provide. Nothing does — which is why `backend.app.main` cannot be imported at all, and why the
tests import the individual modules (`backend.app.core.security`, `backend.app.core.rate_limit`)
and compose their own application. See
[Reaching a running application](#reaching-a-running-application).

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
source imports. To get a working tree locally without editing the manifest, install them at these
exact versions in a **single** command — a second `--no-save` install prunes the packages the first
one added. These are the versions `.github/workflows/ci.yml` installs, so a local tree matches CI:

```bash
npm install --no-save --no-audit --no-fund \
  react-scripts@5.0.1 @reduxjs/toolkit@1.9.7 react-router-dom@5.3.4 \
  @types/react-router-dom@5.3.3 firebase@10.14.1 mathjs@11.12.0 \
  date-fns@2.30.0 @types/node@18.19.86
```

Two of those pins are not arbitrary and changing them costs you errors:

- **`react-router-dom` must be 5, not 6.** `frontend/src/app.tsx` imports `Switch` and passes
  `component=` to `Route`; version 6 removed both. Measured: against `6.30.1` the type check
  reports four errors in that file (`TS2305` for `Switch`, three `TS2322` for `component=`);
  against `5.3.4` it reports none. Migrating the source to v6 is a deliberate frontend change, not
  a dependency bump.
- **`@types/node` must be 18.x.** The version hoisted transitively emits roughly fifty syntax
  errors against the pinned TypeScript 4.9.5.

### Configure the request seam — do this before you expect the SPA to work

The compiled bundle takes its API address and its Firebase configuration from **build-time**
variables, which `react-scripts` inlines. Without them the application signs in against nothing
and refuses to issue API requests at all. They belong in **`frontend/.env`** — a different file
from the backend `.env`, and the one `scripts/deploy.sh` reads during its preflight. Every value
is public by design; none is a secret. `.env.example` section E is the authoritative list.

```bash
# frontend/.env
REACT_APP_API_BASE_URL=https://api.example.com

REACT_APP_FIREBASE_API_KEY=your-firebase-web-api-key
REACT_APP_FIREBASE_AUTH_DOMAIN=your-gcp-project-id.firebaseapp.com
REACT_APP_FIREBASE_PROJECT_ID=your-gcp-project-id
REACT_APP_FIREBASE_APP_ID=1:000000000000:web:0000000000000000000000
```

Exclude that file from your clone the same way as the backend one — `echo "frontend/.env" >>
.git/info/exclude` — because nothing else does.

Four things about these values are worth knowing before you spend time on a symptom:

- **`REACT_APP_API_BASE_URL` must be an absolute URL naming a *different* origin from the one
  serving the application.** `frontend/src/services/api.ts` refuses to dispatch otherwise, and
  says so. This is not fussiness: neither static edge routes API paths to the FastAPI service —
  the container serves files with an SPA fallback, and the load balancer's URL map has one
  backend, the static bucket, which rewrites an unmatched path to `/index.html` with status 200.
  A relative or same-origin base URL therefore fetches the application document and a caller
  would read it as workbook data. For local development that means the API's own port, for
  example `http://localhost:8000` while the SPA runs on `:3000`.
- **`REACT_APP_FIREBASE_PROJECT_ID` must equal the backend's `PROJECT_ID`.** An ID token names
  its issuing project; the API pins verification to `PROJECT_ID`, and the backend's
  `firebase_project_id` setting is refused at start-up unless it is empty or equal to it. So one
  project ID belongs in all of: this file, the backend `.env`, the Terraform `project_id`, and
  nothing else. `scripts/deploy.sh` fails the deployment if the two disagree.
- **The API origin must also be admitted by the served Content-Security-Policy.** Set the
  Terraform `api_origin` variable — which is required, and rejects the SPA's own domain — to
  exactly the *origin* of `REACT_APP_API_BASE_URL` (`scheme://host[:port]`, no path). For the
  container image, set `CSP_CONNECT_SRC_API` to that same origin with one leading space — the
  image **refuses to start** without it, and refuses a value missing that leading space, because
  either one renders a policy admitting no API origin. The deployment preflight compares the
  origin against the policy the load balancer actually serves, matching whole `connect-src`
  source tokens.
- **A missing Firebase value is reported, not thrown.** `api.ts` logs which variables are absent
  as it loads and then fails each request, so check the browser console first.

Run the client-side request-seam tests with:

```bash
# macOS / Linux
cd frontend
CI=true npx react-scripts test --watchAll=false --testPathPattern=api.test

# Windows PowerShell
cd frontend
$env:CI="true"; npx react-scripts test --watchAll=false --testPathPattern=api.test
```

Expect all 93 cases to pass. `CI=true` is what stops the runner entering watch mode. Plain
`npx jest` fails to parse the file — nothing configures a TypeScript transform outside
`react-scripts`.

### What still does not resolve

Two resolution problems remain after the install above, and both need a source or config change:

- **The `@/…` alias resolves for the type checker only.** `frontend/tsconfig.json` now maps
  `"@/*"` alongside the older `@components/*`, `@utils/*`, `@hooks/*`, `@services/*` and
  `@types/*` forms, so `tsc` finds those modules — but `react-scripts` honours `baseUrl` and
  ignores `paths`, so a webpack build still cannot resolve them.
- **`@/components` and `@/pages` have no barrel file**, and `frontend/src/utils/formulaParser.ts`
  still imports a type from the Python schema path, which no TypeScript resolver can load. The
  parser is out of scope for the security work, so it stays that way.

---

## Reaching a running application

Neither tier runs as committed, and this section is the complete specification of what stands in
the way — not a summary of it. Every gap below pre-dates the security work and none of them was
authorised to be fixed by it: the change authorisation that governed that work designates all four
backend gaps as *functional* gaps, to be flagged and not implemented. That conflicts directly with
the requirement that a new developer reach a running application, and the conflict is recorded in
full as **DEV-11** in the [Security Decision Log](./Security%20Decision%20Log.md), with the remedy
carried as follow-up **F24**. Nothing here is guesswork; each line was reproduced against this
working tree.

### What does run today

This is what you can develop against. Every row was executed against this working tree and the
result is what it printed.

| What | Command | Observed result |
|------|---------|-----------------|
| The backend security surface | `PYTHONPATH=. venv/bin/python -m pytest backend/tests/test_security.py -q` | **823 passed, 5 warnings** |
| The whole backend test directory | the same, plus `--continue-on-collection-errors`, on `backend/tests/` | **823 passed, 5 warnings, 3 errors** — the three are the pre-existing collection failures |
| The client half of the identity bridge | `cd frontend && CI=true npx react-scripts test --watchAll=false --testPathPattern api.test` | **93 passed**, after the recovery install above |
| Your Terraform changes | `init -backend=false` then `validate` | clean for `main.tf` and `variables.tf`; **seven** pre-existing errors, all in `outputs.tf` |
| The deployment script's syntax | `bash -n scripts/deploy.sh` | clean |
| The dependency audit | `pip-audit -r backend/requirements.txt --progress-spinner off` with the baseline's `--ignore-vuln` flags | `No known vulnerabilities found, 14 ignored`, exit 0 |

One further verification exists and is the one of record for the Firestore rules, but it needs
tooling that is not part of the backend setup above — the Firebase CLI and a **JDK 21**, because the
emulator is a Java program. Install both before relying on it:

```bash
firebase emulators:exec --only firestore --project demo-excel-clone \
  "python backend/tests/firestore_rules_emulator_check.py"
```

It asserts every verb for an owner, a collaborator, a stranger and an anonymous caller. Without a
JDK the emulator does not start, and the rules are then covered only by `TestFirestoreRules`, which
reads the file's text and cannot evaluate it.

Two of those deserve a note, because they are why the security controls are verifiable at all
while the application is not. The Python suite imports the individual modules
(`backend.app.core.security`, `backend.app.core.rate_limit`, `backend.app.core.security_headers`)
and composes its own FastAPI application from them, rather than importing
`backend.app.main`. And `api.test.ts` transpiles `api.ts` on its own with fakes for `axios`,
`firebase/app` and `firebase/auth`, so it exercises the module's real behaviour without needing
the rest of the frontend module graph to resolve.

### Why the backend does not start — four gaps

Reproduce the first one:

```console
$ PYTHONPATH=. venv/bin/python -c "import backend.app.main"
  File "backend/app/main.py", line 3, in <module>
    from backend.app.api import workbooks, worksheets, cells, collaboration
  File "backend/app/api/workbooks.py", line 4, in <module>
    from backend.app.db import get_db
ImportError: cannot import name 'get_db' from 'backend.app.db' (unknown location)
```

That is the first of four. Each becomes visible only once the previous is closed, which is why
fixing one and retrying feels like no progress — all four must land.

| # | Gap | Evidence | What has to be added |
|---|-----|----------|---------------------|
| 1 | **No package initialiser exists anywhere under `backend/`.** So `backend.app.db`, `backend.app.schema` and `backend.app.services` resolve as PEP 420 namespace packages, which carry no attributes to import from. | A recursive search for `__init__.py` under `backend/` returns nothing. All four route modules import at package level — lines 4, 5 and 6 of `workbooks.py`, `worksheets.py`, `cells.py` and `collaboration.py`. | An `__init__.py` in `backend/`, `backend/app/` and each subpackage. `backend/app/db/__init__.py` must re-export `get_db`, which is defined in `db/database.py` line 32; `backend/app/schema/__init__.py` must re-export the schema names. Rewriting the imports to name the modules directly would also work, but the package form is what the route modules, the tests and the [extension example](#add-a-protected-endpoint) all use. |
| 2 | **`CollaboratorSchema` does not exist.** | `backend/app/api/collaboration.py` line 5 imports it and line 16 annotates a parameter with it. `backend/app/schema/workbook_schema.py` defines `CellSchema` (line 5), `WorksheetSchema` (line 10), `WorkbookSchema` (line 15), `FormulaSchema` (line 29) and `ChartSchema` (line 33) — and no collaborator model. | Define it. Its shape depends on how sharing is actually persisted, which is why it is entangled with the absent collaborators table — [next task](#next-tasks) item 10. |
| 3 | **None of the four domain service classes exists.** | `backend/app/services/` holds only `calculation_engine.py` (`CalculationEngine`), `file_storage.py` (`FileStorageService`) and `real_time_sync.py` (`RealTimeSyncService`). The route modules import `WorkbookService`, `WorksheetService`, `CellService` and `CollaborationService` on line 6 and call them in every handler body. | Implement all four. This is also where owner scoping belongs, which is why closing the authenticated cross-tenant read, write, share and attribution waits on it — [next task](#next-tasks) item 3. |
| 4 | **`init_db` does not exist.** | `backend/app/main.py` line 11 imports it from `backend.app.db.database`, and line 17 `await`s it in the startup event. `database.py` defines `engine` (line 21), `SessionLocal` (line 29) and `get_db` (line 32) only. | Define it. Note it is awaited, so it must be a coroutine — or the `await` has to go with it. |

`backend/tests/test_security.py::TestKnownResiduals::test_the_application_entry_point_cannot_be_imported`
asserts that this import *fails*. That is deliberate: when you close these gaps that test starts
failing, which is the signal to delete it rather than a regression.

Two further defects used to break the first request that reached the database. Both are **closed**,
and both are now pinned in the positive direction so they cannot come back:

- `User` declares `workbooks = relationship("Workbook", back_populates="owner")`, completing the
  pair `Workbook.owner` already declared. Without it **any** query against `User` raised
  `InvalidRequestError: … has no property 'workbooks'` while configuring the mapper - and
  authentication queries `User` on every request, so it was a total outage rather than a degraded
  path. `TestOrmSeam::test_the_mappers_configure` now asserts that configuration completes.
- `WorksheetSchema` carries `class Config: orm_mode = True`, which the frozen worksheets route's
  `from_orm` call requires; without it the route raised `ConfigError` before reading a field. The
  field contract is unchanged and asserted so. A worksheet holding populated `Cell` rows still
  cannot be serialised - the schema declares a map and the ORM holds a list - and that projection
  belongs to the absent `WorksheetService`, which is [next task](#next-tasks) item 4.

### Why the frontend does not compile

`npx tsc --noEmit -p frontend/tsconfig.json` reports **59** errors, measured against the pinned
install above. That is down from 67 at the pre-remediation baseline, and what remains has a
different shape: the request seam is clean and the residue is the Redux store gap plus ordinary
component defects.

Every module the request-seam work owns type-checks with **zero** errors. `services/api.ts`,
`services/auth.ts`, `services/collaboration.ts`, `schema/workbookTypes.ts`, `store/userSlice.ts`,
`store/workbookSlice.ts` and `index.tsx` carried 13 errors between them at baseline and carry none
now. The remaining 59 fall into four groups:

1. **30 are the Redux store gap.** `frontend/src/store/index.ts` imports `workbookReducer` and
   `userReducer` as named exports when both are default exports, and it exports no
   `useAppSelector` and no `useAppDispatch` — which eleven modules import. That file is excluded as
   Redux store structure, so this is [next task](#next-tasks) item 22.
2. **9 are missing barrel files**: eight imports of `@/components` and one of `@/pages`. Neither
   directory has an `index.ts` — same next task.
3. **4 are payload shapes the reducers do not accept.** `Grid.tsx`, `Cell.tsx` and `FormulaBar.tsx`
   dispatch objects that do not match `updateCell`'s declared payload. Component files are
   excluded from this change set.
4. **The remaining 16 are ordinary type errors** in excluded files: nine implicit `any` parameters,
   four unused declarations, one missing property, one `AuthProvider` that `services/auth.ts` does
   not export, and one cross-tier import of the Python schema path in `utils/formulaParser.ts`,
   which is excluded as formula parsing.

Note what is no longer on this list, because it tells you what to expect from your own run. The
bare `@/…` prefix resolves now, because `frontend/tsconfig.json` maps `"@/*"`. The repository-root
imports of `backend/app/schema/workbook_schema` and `frontend/src/schema/workbookTypes` are gone
except for the one in `formulaParser.ts`. And `react-router-dom` contributes nothing, because the
pinned install uses `5.3.4` — the version `app.tsx` is actually written against.

One counter-intuitive effect is worth knowing before you read a diff of these counts: resolving the
alias **raised** the error count in the component files by nine. An unresolved module reports one
error per import line; a resolved one reports one error per missing symbol. That is the same defect
described more precisely, not new breakage.

`tsc` is not the whole story. `react-scripts` honours `baseUrl` and ignores `paths`, so a webpack
build still cannot resolve `@/…` even though the type checker can — [next task](#next-tasks)
item 21. And declaring the packages in the manifest ([next task](#next-tasks) item 7) remains the
prerequisite for a reproducible build.

---

## Domain context

A spreadsheet application in three tiers, plus managed Google Cloud services.

```
Browser (React SPA)
  |  signs in directly ------------------> Firebase Auth / Identity Platform
  |  subscribes directly ----------------> Firestore          <-- no backend in this path
  |  REST + Bearer ID token
  v
FastAPI on GKE  (pod identity = one Google service account, via Workload Identity)
  |--> Cloud SQL Auth Proxy sidecar -----> Cloud SQL PostgreSQL 13
  |      127.0.0.1:5432, psycopg2, sslmode=disable, instance ENCRYPTED_ONLY
  |--> Cloud Storage             (uploads, expiring signed URLs, signed as itself)
  |--> Secret Manager            (DATABASE_URL, REDIS_URL, SECRET_KEY)
  |--> Identity Platform         (token verification, incl. a revocation check per request)
  |--> Firestore                 (server client library, bypasses rules)
```

**The one structural fact to internalise:** the browser reaches Firestore *directly*, without
passing through the backend. No server-side check can mediate that path, which is why
`firestore.rules` exists and is the only control on it. It is also why the backend cannot be
broken by those rules — server client libraries bypass Firestore rules entirely and authenticate
through Application Default Credentials.

### The database contract

The backend never addresses the database instance directly: it connects to the Cloud SQL Auth Proxy
on loopback inside the pod, and the proxy owns the authenticated channel to Cloud SQL. That single
fact determines the whole contract, and `scripts/deploy.sh` asserts every part of it against the pod
manifest before it publishes anything.

- **Driver.** `DATABASE_URL` must be a synchronous psycopg2 PostgreSQL URL — `postgresql://` or
  `postgresql+psycopg2://`, and nothing else. `sslmode` is a psycopg2 connect argument, so an
  `asyncpg` or `sqlite` URL would silently discard the transport setting; the engine refuses to build
  on any other dialect. Both the environment value and the Secret Manager value that overwrites it
  are validated, and neither is ever quoted in a rejection message, because the URL carries the
  database password.
- **Host.** In the deployment it is the proxy's loopback listener, `127.0.0.1:5432`. The database
  name and the login must be the ones step 3 creates, and `deploy.sh` compares both against your
  `DB_NAME` and `DB_USER`.
- **Transport.** `db_sslmode` is `Literal["disable", "require"]` — two legal values and no others.
  `allow` and `prefer` are refused because each negotiates plaintext silently whenever the server
  offers it; `verify-ca` and `verify-full` are refused because the proxy's local listener carries no
  certificate for the instance's name, so verification against it cannot complete at all. `disable`
  is not freely selectable either: `Settings` refuses it unless the **resolved** `DATABASE_URL` — the
  Secret Manager value, not the environment one — names a loopback host, one of `127.0.0.1`,
  `localhost`, `::1` or `[::1]`. A socket-based URL is not an alternative, because the URL grammar
  requires a host and refuses `…@/db?host=/cloudsql/…` a step earlier. The plaintext mode can
  therefore only ever describe a connection that never leaves the machine, and exactly one value is
  deployable on any given topology: **`disable` on GKE behind the proxy**, which is what the pod
  manifests must set, and `require` on a direct connection. Encryption is not lost in the first
  case — the proxy's own leg to the instance is mutually authenticated, which is what satisfies the
  instance's `ENCRYPTED_ONLY` mode, and the unencrypted leg is a socket inside the pod.

### Identity, and one deliberate asymmetry

The browser signs in with Firebase and sends the resulting ID token as
`Authorization: Bearer <token>`. The backend verifies it with the Firebase Admin SDK against a
pinned project, then resolves the local `User` **by the token's verified `email` claim**.

The Firestore rules, by contrast, key on `request.auth.uid`. That asymmetry is deliberate: UIDs
never change and emails can, so UID is the better key — but `User` has no `firebase_uid` column
and this repository has no migration tooling, so `email` is the only mapping available without a
schema change. Adding `firebase_uid` is [next task](#next-tasks) item 5.

### The HTTP surface

Five endpoints, mounted at the root with no prefix, all requiring authentication **while
`auth_enforcement_enabled` is true** — its default, and the mandatory production setting:

| Method | Path |
|--------|------|
| GET | `/workbooks` |
| POST | `/workbooks` |
| GET | `/workbooks/{workbook_id}/worksheets` |
| PUT | `/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells` |
| POST | `/workbooks/{workbook_id}/share` |

The client calls **three** of those five. `frontend/src/services/api.ts` exports exactly
`fetchWorkbooks`, `createWorkbook` and `updateCell`; nothing calls the worksheet-listing or share
routes from the browser, and that is deliberate rather than an omission — both need a caller that
does not exist yet, and adding one is a feature rather than a security fix. That work is recorded
as follow-up F25 in the [Security Decision Log](./Security%20Decision%20Log.md). If you add a
caller, add it to that module rather than issuing a bare `axios` call: the request interceptor
that attaches the ID token is registered on that client instance only.

Two things authentication does **not** do, both worth knowing before you rely on it.

**It does not scope a workbook to its owner.** No handler filters by owner, so an authenticated
user can still address another user's `workbook_id`. That is a known open risk, recorded in
[SECURITY.md](../SECURITY.md) as residual 1 and in the Decision Log as D13, and closing it is
[next task](#next-tasks) item 3.

**It is weakened, though not removed, by `auth_enforcement_enabled=false`.** That switch is
break-glass only and must never be set in production. What it changes is precisely this: a
presented token is admitted **on its unverified claims** — signature, expiry, revocation and
account state are all skipped, so anyone able to mint arbitrary claims for a known user's email
address is admitted as that user.

What it does **not** change is that a credential is still required. `oauth2_scheme` is
`OAuth2PasswordBearer(tokenUrl='token')` and carries its default `auto_error`, so a request with
no `Authorization` header is answered `401` with a `WWW-Authenticate: Bearer` challenge before any
application code runs; an empty `Bearer` value is refused unconditionally by
`_resolve_current_user`; and the identity the claims name must still resolve to a row in the local
`users` table. There is no placeholder caller in the module and no setting that admits one, so no
admitted request is anonymous — each one is attributable to a stored user. Every bypass is logged
at warning level and marked with `X-Auth-Enforcement-Bypassed: true`, which makes it auditable
rather than harmless.

An earlier version of this section said the switch admitted a request carrying no `Authorization`
header at all, on a placeholder `User()`, and that the bearer scheme was constructed with
`auto_error` disabled. None of that was ever true of this module, and the difference matters in
the direction that counts: the switch makes the credential check worthless, but it does not make
the credential optional.

If the browser stops attaching a token, the remedy is to revert the interceptor in
`frontend/src/services/api.ts`, not to flip that switch and not to select
`auth_token_verifier=legacy_jwt` — that verifier wants an HS256 token signed with `SECRET_KEY`
whose `sub` is a local `users.id` and which carries an `exp` claim, which nothing in this
repository mints and the browser cannot produce.

---

## Deploying

Read this section end to end before starting. **A deployment cannot currently complete**, for one
reason stated up front rather than discovered at step 2: `infrastructure/terraform/outputs.tf`
references seven resources that no configuration declares, and Terraform validates outputs during
`plan` as well as `validate`, so `terraform plan` and `terraform apply` both fail before anything
is provisioned. Repairing that file — [next task](#next-tasks) item 8 — is the first thing a real
deployment needs. Everything below is the sequence once it is fixed, and it is written out in full
so that fixing one file is all that stands between you and a deployment.

The steps are in dependency order, and one of those dependencies is easy to get wrong: **the three
Secret Manager secrets must exist before Terraform runs**, because
`google_secret_manager_secret_iam_member.api_runtime_secret_accessor` grants read access on secrets
it names by ID rather than creating them. Apply Terraform first and it fails on a secret that does
not exist.

### Step 1 — create the three Secret Manager secrets

This repository creates no secrets, and `Settings.__init__` reads all three on **every**
construction. Each needs a `latest` version:

```bash
PROJECT_ID=your-gcp-project-id

# The login you create in step 3 and the database Terraform creates. Whatever you use here must be
# the same DB_USER and DB_NAME you export in step 7: deploy.sh reads this secret and compares its
# login and its database name against those two, and aborts on a mismatch.
DB_USER=excel-app
DB_NAME=main-database

# The host is the Cloud SQL Auth Proxy's loopback listener inside the pod, never the instance's own
# address, and the driver must be psycopg2. The password is read from a prompt so it does not enter
# your shell history; percent-encode it if it contains any of : / ? # [ ] @ or a space.
read -r -s -p "password for $DB_USER: " DB_PASSWORD; echo
printf '%s' "postgresql+psycopg2://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}" \
  | gcloud secrets create DATABASE_URL --project="$PROJECT_ID" --data-file=- \
      --replication-policy=automatic
unset DB_PASSWORD

printf '%s' 'redis://10.0.0.3:6379/0' \
  | gcloud secrets create REDIS_URL --project="$PROJECT_ID" --data-file=- \
      --replication-policy=automatic

python -c "import secrets; print(secrets.token_urlsafe(48), end='')" \
  | gcloud secrets create SECRET_KEY --project="$PROJECT_ID" --data-file=- \
      --replication-policy=automatic
```

Use `gcloud secrets versions add <NAME> --data-file=-` to rotate one later. You do **not** need to
grant access by hand: Terraform binds `roles/secretmanager.secretAccessor` on exactly these three
secrets to the runtime identity, and on no others, so a fourth secret is unreadable until it is
added to that resource deliberately.

The `DATABASE_URL` value is not free-form. `scripts/deploy.sh` reads the secret during its preflight
and checks four things about it, aborting before it publishes anything if any of them disagrees:
the **driver** is `postgresql` or `postgresql+psycopg2` and nothing else; the **host** is `127.0.0.1`,
`localhost`, `::1` or `[::1]` — a socket-based URL is refused, because `Settings` requires the URL to
name a host; the **database** is your `DB_NAME`; and the **login** is your `DB_USER`. `Settings`
enforces the same driver and, because
the pod manifests set `db_sslmode=disable`, the same locality — see
[The database contract](#the-database-contract). No `?sslmode=` query belongs in the URL: the
transport mode is a `connect_args` value read from `db_sslmode`, not a URL parameter.

An earlier version of this step showed `postgresql://user:password@host:5432/dbname?sslmode=require`.
That value is refused three times over — a literal host `host` that is not local, a database `dbname`
that is not the provisioned one, and a login `user` that is not `DB_USER` — so an operator who pasted
it got a failed preflight rather than a deployment. Measured against the real script, the host is what
aborts first: *"The DATABASE_URL secret names host 'host', which is not a local endpoint."* Its fourth
defect is **not** a refusal, which is worth being exact about: a `?sslmode=` query passes both gates
untouched, so it fails silently rather than loudly, stating a transport mode nothing reads while
`db_sslmode` supplies the one that is used.

Note the ordering trap inside the application as well: the three variables must *also* be present
in the environment for validation to pass, because `Settings.__init__` calls `super().__init__()`
before it replaces them with the Secret Manager values. Environment placeholders are fine; the real
values come from Secret Manager.

### Step 2 — provision the infrastructure

Seven Terraform variables have no default and must be supplied: `project_id`, `domain_name`,
`allowed_origins`, `api_origin`, `signer_service_account`, `function_runtime` and
`function_source_bucket`.

`api_origin` is the one that catches people out, so it is worth being exact about. It must be a
**non-empty** exact origin — `scheme://host[:port]`, no trailing slash, no path — and it must
**differ from `https://<domain_name>`**. Two `validation` blocks reject an empty value and a
plaintext scheme outside `localhost`, and a precondition on `google_compute_url_map.excel_app`
rejects the edge domain itself. So the API has to be served from its own origin, such as
`https://api.example.com`.

The reason is structural rather than stylistic: the URL map this configuration builds has exactly
one backend, the static bucket, and rewrites any unmatched path to `/index.html`. An API base URL
on the edge domain would therefore return the SPA document under `200` where the client expected
JSON — a failure that looks like a parsing bug and is really a routing one. Pointing
`api_origin` at the edge domain does not misconfigure a header; it misroutes the API.

An earlier version of this step said `api_origin` "is normally the empty string" and the sample
below set it to `""`. That combination cannot pass `terraform plan` at all: `validate` accepts the
tfvars, and the variable's own validation then rejects the value.

Four further variables are declared but **read by no resource** — `environment`,
`storage_bucket_name`, `gke_min_nodes` and `gke_max_nodes`. Each carries a default and a
`NOT REFERENCED` description recording what actually governs the value, so Terraform does not
demand them and setting one has no effect. There is deliberately **no** `db_user` and no
`db_password` variable: declaring the password would persist a live database credential in
Terraform state and in whatever tfvars supplied it, for a value no resource reads. The login is
created outside Terraform (step 3), its password travels only inside the `DATABASE_URL` secret,
and `scripts/deploy.sh` takes the login from your `DB_USER` environment variable and confirms it
exists on the instance before deploying.

Create `infrastructure/terraform/terraform.tfvars`:

```hcl
project_id             = "your-gcp-project-id"
region                 = "us-central1"

# Optional. Declared, defaulted, and read by no resource - setting either has no effect.
# Left commented out deliberately; see the NOT REFERENCED notes in variables.tf.
# environment          = "production"
# storage_bucket_name  = "unused-see-variables-tf"

# There is no db_user and no db_password variable. Do NOT add them here: Terraform would
# reject an undeclared variable, and db_password would put a live credential in state. The
# Cloud SQL login is created in step 3 and the backend authenticates with the DATABASE_URL
# secret; scripts/deploy.sh reads the login from your DB_USER environment variable.

# The HTTPS edge. domain_name must be a domain you control and can point at the
# load balancer address, because the managed certificate is validated over DNS.
# A Terraform precondition also requires it to appear as the HOST of an allowed
# origin below, so the domain this certificate serves cannot be one the API refuses.
domain_name            = "app.example.com"

# Every origin the browser may call the API from. Keep the backend's ALLOWED_ORIGINS
# setting equal to this list. Identity Platform's authorized sign-in domains are
# DERIVED from it (local.allowed_origin_hosts), so those two cannot drift apart.
allowed_origins        = ["https://app.example.com"]

# REQUIRED, non-empty, and it must NOT be https://<domain_name>. This configuration routes
# no API path to the API service, so the API needs its own origin. Appended to connect-src
# in the served CSP. Must equal the ORIGIN of the frontend build's REACT_APP_API_BASE_URL.
api_origin             = "https://api.example.com"

# The one service account the pods authenticate as and sign object URLs with.
signer_service_account = "excel-app-url-signer@your-gcp-project-id.iam.gserviceaccount.com"

# Where the pods run, for the Workload Identity binding.
kubernetes_namespace       = "default"
kubernetes_service_account = "excel-app-backend"

# The Cloud Function. function_runtime is validated against a live gcloud read by
# scripts/deploy.sh, so a runtime Google has withdrawn fails the preflight.
function_runtime       = "nodejs20"
function_source_bucket = "your-gcp-project-id-function-source"
```

Then:

```bash
terraform -chdir=infrastructure/terraform init
terraform -chdir=infrastructure/terraform plan -out=tfplan
terraform -chdir=infrastructure/terraform apply tfplan
```

`.terraform.lock.hcl` is deliberately not committed, so `init` selects a provider inside the
`~> 7.0` constraint each time. Fixing that is [next task](#next-tasks) item 19. To check a
Terraform change *without* provisioning anything, use `init -backend=false` and `validate` — the
exact command, and how to tell your own errors from the seven pre-existing ones, is in
[Pitfalls](#pitfalls).

### Step 3 — create the database login

The Cloud SQL instance exists now, but its login does not. Terraform deliberately does not create
it, because managing the password there would persist it in Terraform state:

```bash
gcloud sql users create excel-app --instance=main-instance \
  --project="$PROJECT_ID" --prompt-for-password
```

The `DATABASE_URL` secret names this login, so without it every database call fails — including
authentication, which queries the users table before any route body runs. `scripts/deploy.sh`
checks the user exists and refuses to deploy if it does not.

### Step 4 — understand the single identity

**One** service account is both the runtime identity and the signing identity: the address in
`signer_service_account`. Terraform declares all of it, so there is nothing to run here — but
knowing the shape saves an afternoon when a signed URL fails:

| Grant | Resource | Why it exists |
|-------|----------|---------------|
| `roles/iam.serviceAccountTokenCreator` **on itself** | `google_service_account_iam_member.url_signer_token_creator` | Exactly the `iam.serviceAccounts.signBlob` permission `generate_signed_url` needs when the runtime holds a token and no private key. The account is both caller and resource because the runtime signs as itself. |
| `roles/iam.workloadIdentityUser` | `google_service_account_iam_member.api_runtime_workload_identity` | Lets `<namespace>/<kubernetes_service_account>` act as this account. Without it the pods hold no Google identity at all — they cannot read their secrets, cannot reach Cloud SQL and cannot sign. |
| `roles/storage.objectAdmin` on the uploads bucket | `google_storage_bucket_iam_member.user_uploads_signer_object_admin` | Scoped to that one bucket. All three verbs are needed: it uploads, it deletes a generation it could not sign, and a signed URL is authorized as its signer, so without read the URL resolves to an authorization failure. |
| `roles/cloudsql.client` | `google_project_iam_member.api_runtime_cloudsql_client` | The Cloud SQL Auth Proxy authenticates as this identity. |
| `roles/secretmanager.secretAccessor` | on each of the three secrets | The three values `Settings.__init__` reads. |

Terraform declares both Google-side halves of this: the cluster carries
`workload_identity_config` with the `<project>.svc.id.goog` pool, and the
`roles/iam.workloadIdentityUser` binding above names the exact Kubernetes principal. What it
cannot create is the Kubernetes side: the `ServiceAccount` must exist in the namespace carrying
the `iam.gke.io/gcp-service-account: <signer_service_account>` annotation, and the pod spec must
select it by `serviceAccountName`. Both live in the manifests, step 6, and `scripts/deploy.sh`
refuses to apply a pair that does not match the binding. No key is ever issued for this account,
and none should be.

That one account holding both the runtime permissions and the signing authority is a recorded
residual risk — see [SECURITY.md](../SECURITY.md#residual-risks).

### Step 5 — point DNS at the load balancer, and wait

```bash
gcloud compute addresses describe excel-app-lb-ip --global \
  --project="$PROJECT_ID" --format='value(address)'
```

Create an `A` record for `domain_name` pointing at that address, then wait for the certificate:

```bash
gcloud compute ssl-certificates describe excel-app-ssl-cert --global \
  --project="$PROJECT_ID" --format='value(managed.status)'
```

It must read `ACTIVE`. Managed certificates are validated over DNS and are not instant — expect
tens of minutes. Terraform creates **both** listeners unconditionally, the 443 listener and the
port-80 redirect, so there is no staged cutover to perform and no switch to flip: until the
certificate is `ACTIVE`, HTTPS simply does not serve. Port 80 carries only a `301`, never content.

### Step 6 — supply the Kubernetes manifests

`scripts/deploy.sh` applies `deployment.yaml` and `service.yaml` from `$K8S_MANIFEST_DIR`
(default `k8s`), and **this repository contains neither**. The script checks them before applying
and refuses on any of these, so treat the list as the specification:

- `spec.template.spec.serviceAccountName` must be `excel-app-backend` — the value of
  `kubernetes_service_account`. Without it the pods run as the namespace default account, which
  holds no Workload Identity binding.
- A `ServiceAccount` must be declared in the namespace, annotated
  `iam.gke.io/gcp-service-account: <signer_service_account>`. Both halves of the binding have to
  agree or the pods hold no Google identity.
- A **`cloud-sql-proxy` sidecar** for the instance's connection name. This is not optional and
  there is no alternative: `google_sql_database_instance.main` is provisioned with no private
  network, so there is no private IP to route to, and reaching its public address directly would
  need an authorized network and client certificates this infrastructure does not create.
- `db_sslmode` must be **`disable`** here, and must not be `require`: the proxy presents a plain
  TCP loopback listener inside the pod, so a client asking for TLS on it cannot connect at all.
  `verify-ca` and `verify-full` are not accepted values at all. Note this is the one place the
  setting's own default (`require`, which suits a direct connection) is the wrong value, and
  `Settings` permits `disable` only because the resolved `DATABASE_URL` names a loopback endpoint.
  Encryption is not lost — the proxy dials the instance over its own mutually authenticated TLS
  session, which is what satisfies the instance's `ENCRYPTED_ONLY` mode.
- The six required settings variables must reach the container.

One more mismatch to correct in the manifest or the image:
`infrastructure/docker/Dockerfile.backend` runs `uvicorn main:app`, which is the wrong module
path — the application object is at `backend.app.main:app`.

### Step 7 — run the deployment script

```bash
export PROJECT_ID=your-gcp-project-id
export REGION=us-central1
export DOMAIN_NAME=app.example.com
export SIGNER_SERVICE_ACCOUNT=excel-app-url-signer@${PROJECT_ID}.iam.gserviceaccount.com
export DB_USER=excel-app
sh scripts/deploy.sh
```

`PROJECT_ID`, `REGION`, `DOMAIN_NAME`, `SIGNER_SERVICE_ACCOUNT` and `DB_USER` are required and
have no default, deliberately — a default silently targets the wrong project. `K8S_MANIFEST_DIR`
defaults to `k8s`, and `KUBERNETES_NAMESPACE`, `KUBERNETES_SERVICE_ACCOUNT`, `GKE_CLUSTER` and
`SQL_INSTANCE` default to the Terraform values.

The frontend build values are read from `frontend/.env` (override the path with `FRONTEND_ENV`).
Two are checked against the live project, because a bundle can otherwise be published that
authenticates against another Firebase project or calls an API origin the served
Content-Security-Policy does not admit — a failure that appears only in the browser:
`REACT_APP_FIREBASE_PROJECT_ID` and `REACT_APP_API_BASE_URL`. Both are public by design;
`react-scripts` inlines every `REACT_APP_*` value into the bundle, so nothing secret may go there.

The script runs six preflight checks against the *live* project before it publishes anything —
the project and region match what Terraform provisioned; the HTTPS edge exists and both
forwarding rules answer on the load balancer address; the compiled frontend's Firebase project and
API origin agree with the deployed infrastructure and the served CSP; the Kubernetes manifests
carry the right identity and database path; the browser's Firestore access pattern is checked
against the rules about to be deployed; and the Cloud Function runtime is one Google still
deploys. The Cloud Function's invoker policy is **read and asserted** in that same preflight,
before any mutation: the script aborts if `allUsers` or `allAuthenticatedUsers` holds
`roles/cloudfunctions.invoker`, if the intended service account does not hold it, or if the policy
cannot be read at all — the last of those matters most, because an unreadable policy must not be
reported as proof that no public binding exists. Removing a binding is Terraform's job, not the
script's: `google_cloudfunctions_function_iam_binding` is authoritative, so an `apply` removes any
member it does not list (`D56`). The script then builds and publishes the frontend, builds and
pushes the backend image, applies the manifests, deploys the Firestore rules, and prints the manual
checks that remain.

**A second reason this step cannot complete today**, beyond the `outputs.tf` defect at the top of
this section: the script runs `npm run build`, and the frontend does not compile — see
[Why the frontend does not compile](#why-the-frontend-does-not-compile). Both have to be fixed
before a deployment reaches the end.

### The one configuration value you must keep in step by hand

Terraform derives more of this than it used to, so there is exactly **one** manual agreement left,
and it is worth knowing which:

- **Derived, and cannot drift.** `google_identity_platform_config.default.authorized_domains` is
  set to `local.allowed_origin_hosts` — the hosts parsed out of `var.allowed_origins`. And a
  precondition on the managed certificate requires `var.domain_name` to be one of those hosts. So
  the served domain, the CORS allow-list and the authorized sign-in domains are one value with two
  derivations.
- **Manual.** The backend's `ALLOWED_ORIGINS` setting is read from the environment or Secret
  Manager, not from Terraform, so **it is the one value you must keep equal to `allowed_origins`
  yourself.** If the two disagree the browser is refused by CORS on an origin Identity Platform
  happily signed it in on, and the failure appears only in the browser.

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

**The backend service does not start.** `uvicorn backend.app.main:app` fails at import, and it
fails four times over — each gap only visible once the previous one is closed. The first error you
will see is `ImportError: cannot import name 'get_db' from 'backend.app.db'`. All four are
specified with their evidence and their remedy under
[Reaching a running application](#reaching-a-running-application); do not spend time diagnosing
them from the traceback.

Also note the container command is wrong independently of all that: `Dockerfile.backend` runs
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
# Remove generated Terraform init artifacts; this repository does not commit the lock file.
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

**A page is capped at 100 rows, and `limit=0` is now a `422`.** `GET /workbooks` and
`GET /workbooks/{id}/worksheets` both refuse a `limit` above 100 or below 1, and a `skip` above one
million or below zero. If you are looking for a workbook that is not in the response, page with
`skip` — asking for a bigger `limit` will not work, by design. The numbers live in
`backend/app/core/pagination.py` with the reason each one is what it is. Note that the worksheets
route only gained these parameters recently: code written against the old behaviour may assume it
returns *all* of a workbook's worksheets, and for a workbook with more than 100 it no longer does.

**Measure compression at the network layer, not from page JavaScript.** Responses of 500 bytes or
more are gzipped. A page cannot see it: `Content-Encoding` is not exposed cross-origin, and
`PerformanceResourceTiming` reports all three size fields as `0` without `Timing-Allow-Origin`,
which the API deliberately does not send. Both are correct behaviour and both look like the feature
is off. Use the browser's network panel, or a client with automatic decoding disabled:

```powershell
# httpx and requests both decode transparently - iter_raw() is what shows the wire size
.\venv\Scripts\python.exe -c "import httpx; r = httpx.Client().build_request('GET', 'http://127.0.0.1:8000/workbooks'); print('use response.iter_raw() to count wire bytes')"
```

**A `503` with `Retry-After` is not a bug, and it is not the rate limiter.** Requests in flight are
capped at the database pool's capacity; past that, a request waits briefly and is then shed with a
`503`. That is a different control from the `429` the throttling tiers return: `429` means the client
exceeded its quota, `503` means the server declined the work. If you see `503`s under load, the
server is protecting the pool — look at how long each request holds its connection, not at the
throttling thresholds.

**If you resize the database pool, resize the concurrency bound with it.** They are equal by
construction and a test enforces it. See next task 27 for why that equality is load-bearing.


---

## How to extend

### Add a protected endpoint

Copy the shape below rather than the shape of an existing route module. The five existing modules
open with `from backend.app.db import get_db`, and **that import does not work** — `backend/app/db`
has no `__init__.py`, so it is a namespace package that exports nothing and the import raises
`ImportError: cannot import name 'get_db' from 'backend.app.db'`. It is one of the two reasons the
service does not start. Import from the defining module instead, and your route will still work once
the package exports are added:

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.core.security import get_current_user
from backend.app.db.database import get_db          # NOT backend.app.db
from backend.app.db.models import User

router = APIRouter()


# SECURITY: enforce authentication — previously no route required a valid token
@router.get("/your/path")
def your_handler(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ...
```

Three things to know before you rely on it:

- `current_user` is a dependency parameter, so it does not appear in the request contract — no
  change to the path, the method, the request body or the response model.
- `current_user` is **detached** from any Session. Read only its loaded columns (`id`, `email`,
  `username`, `created_at`); touching `current_user.workbooks` raises `DetachedInstanceError`. If you
  need related rows, query them through your own `db` session using `current_user.id`.
- Authentication is not authorization. Nothing filters by owner, so add your own ownership check
  against `Workbook.owner_id` if your route addresses a workbook — and see next task item 3, which
  is the project-wide version of that.

Then add a test asserting the route returns `401` without a token: `TestRouteAuthenticationDependency`
and `TestUnauthenticatedRequestsAreRefused` drive all five existing routes from one list, so extend
that list rather than writing a new test from scratch. If your route reads the database, add a case
to `TestAuthenticatedIdentityResolution` too — it shows how to seed a `User` through the
`authentication_database` fixture and reach a handler with a verified token.

### Extend the Firestore rules

`firestore.rules` is built from small named predicates — `isSignedIn()`, `isOwner()`,
`isCollaborator()`, `ownerUidUnchanged()`, `withinCollaboratorWriteScope()` and others. Compose
those rather than inlining new conditions, and remember two platform rules: an unmatched path is
denied by default, so you never need a catch-all deny; and **rules are not filters** — a
collection query that *could* return a document the rules forbid fails entirely rather than
returning a subset.

Validate with the emulator before deploying. The assertions are committed — 
`backend/tests/firestore_rules_emulator_check.py` — so this is a command you can run rather than a
template to fill in:

```bash
firebase emulators:exec --only firestore --project demo-excel-clone \
  "python backend/tests/firestore_rules_emulator_check.py"
```

It passes when the process exits 0 and prints `ALL n ASSERTIONS PASSED`; any request denied that
should have been allowed, or allowed that should have been denied, prints the offending case and
exits 1. It uses only the Python standard library, deliberately, so it adds no dependency to either
manifest — in particular it does not pull in `@firebase/rules-unit-testing`.

Two prerequisites, both easy to miss: the **Firebase CLI**, and a **JDK or JRE at version 21 or
above** with `JAVA_HOME` pointing at it. Current `firebase-tools` releases refuse to start the
Firestore emulator on Java 17 or below. This is the only place these rules can be evaluated at all
— they run inside Google's rules engine, and the backend's `firestore.Client()` is a server client
library that bypasses rules entirely, so no in-process test can reach them.
It needs the Firebase CLI and a **JDK 21** on `PATH`, because the emulator is a Java program. The
`--project demo-excel-clone` argument matters: a `demo-` prefix keeps the emulator entirely local
and stops it reaching a real project. Extend that script when you add a rule, rather than checking
by inspection — it is the only verification of the authorization semantics that actually evaluates
the rules, since `TestFirestoreRules` in the Python suite can only assert the file's text.

The per-verb authority the rules currently enforce, which the script asserts:

| Verb | Owner | Collaborator | Anyone else |
|------|-------|--------------|-------------|
| `read` | yes | yes | no |
| `create` | yes, and only naming itself as owner | no | no |
| `update` | yes, including changing the collaborator list, but may not reassign ownership | yes, confined to the content fields, and may touch neither authorization field | no |
| `delete` | yes | **no** | no |

Two things to know before you write a rules check of your own, both found by QA testing and
neither a defect in the rules:

- **Never assert on the HTTP status of a browser request.** The script above drives the emulator's
  REST surface, where a refusal really is `403 PERMISSION_DENIED` — measured: owner `200`, stranger
  `403`, anonymous `403`. The browser does not use that surface. Through the Firestore SDK's
  streaming transport every request returns **`200`**, refusal included, with the denial carried in
  the channel payload (`ADD` then `REMOVE`, `cause.code 7`). A check that keys on status there
  reports **PASS against wide-open rules**. Use the script, or inspect the payload.
- **An "evaluation error at L132:22" in the emulator output is not a failure.** It accompanies a
  correct `PERMISSION_DENIED`, appears on deny paths including a document with no `ownerUid`, and
  all 32 assertions pass while it is printed. It is the emulator's verbose diagnostic, and it has
  already been investigated and disproved as a rules bug once — see `SECURITY.md`.

### Change the throttling tiers

`rate_limit_default` and `rate_limit_write` are configuration, not code. Widen them rather than
editing `backend/app/core/rate_limit.py`. Four things to know before you do:

- **The ceiling is one bucket per client for the whole application**, not per route. Every request
  to every URL decrements it, including paths that match no route at all — which is deliberate,
  because scanning for endpoints that do not exist is exactly the traffic a ceiling should meter.
- **The write tier is a second bucket**, so a `POST` costs one unit in each.
- **`rate_limit_write` and `CELL_WRITE_COALESCE_MS` in `frontend/src/services/api.ts` are one
  decision.** The client coalesces cell writes per worksheet into one `PUT` per 1000 ms window, so
  a continuously edited worksheet costs at most 60 writes a minute; at `300/minute`, five such
  worksheets reach the ceiling and four leave 60/minute for workbook creation and sharing. Move one
  number and you must move the other — a test asserts both values.
- **Throttling counts all outcomes including `401`s**, which is deliberate for credential-stuffing
  defence and another reason the budget must be generous.

Two behaviours are not configurable, and both are worth knowing rather than rediscovering. Windows
are counted in the Redis store derived from `REDIS_URL`, so there is no second URL to keep in step;
and if that store fails, both tiers keep **enforcing** on process-local counters — logging once
with a traceback and re-counting the triggering request — rather than admitting unmetered traffic.
The cost of that degradation is precision, not enforcement: counters become per-process, so a
client's real ceiling is the configured value times the number of processes.

**How you launch the server decides whether a client can be identified at all**, so check this
before you conclude a threshold is wrong. uvicorn trusts `X-Forwarded-For` from a *loopback* peer by
default and replaces the connection's peer with the address that header names — with no flag of any
kind — so in a local run, under compose, or behind an in-pod sidecar proxy the reported peer is
whatever the caller wrote. Behind a Google load balancer the peer is not loopback and no rewrite
happens. The application detects a rewritten peer (a forwarded chain carries no port, so the
rewriting layer reports zero, which an accepted connection never does) and counts every such request
against **one shared bucket**, which is why rotating the header cannot buy extra quota. The
consequence to expect while that is happening is aggregate metering: every client looks like one, so
either size the ceiling for the total or serve with the transport peer preserved —
`--no-proxy-headers`, or trusting only the address of the proxy actually in front of you. Widening
the trusted set is the one move to avoid: it is what makes the header authoritative again.

### Read the logs

`backend/app/core/logging_config.py` is called once by the entry point and is the only thing that
configures logging. Every record then carries an ISO timestamp, its level and its logger name, so
`WARNING backend.app.core.rate_limit` is greppable and a log pipeline can route on severity. Two
details matter when you add a record of your own:

- **Informational records are emitted.** The root level is `INFO`, so `logger.info(...)` reaches the
  stream. Before this existed the records went to `logging.lastResort`, which drops everything below
  `WARNING` — which is how the record naming the throttling window store managed to never appear.
- **Control characters in a message are escaped**, because a record may quote a value that arrived in
  a request header and a newline in one would otherwise write what reads as a second, fabricated
  entry. Escaping covers the message only, so `exc_info=True` still produces a readable multi-line
  traceback. The server's access logger is filtered the same way; it does not propagate to the root
  handler, so it needs its own reach.

If you need a different level, pass it: `configure_logging("DEBUG")`. It is deliberately a function
argument rather than a `Settings` field, because the configuration surface is fixed at the 17 fields
`.env.example` documents.

### Change the Content-Security-Policy

This is the change most likely to break the application in a way that only shows up in a browser,
so read the whole section before editing anything. Adding a script CDN, an analytics endpoint, a
web font host or a second API origin all land here.

**`backend/app/core/security_headers.py` is canonical.** `CONTENT_SECURITY_POLICY` in that module
is the directive list of record — thirteen directives, built by joining a tuple with `"; "`. The
other three delivery points reproduce it and are checked against it by tests. Change it there
first, then propagate.

**All four delivery points, and what each takes as input:**

| # | Delivery point | File | Inputs | Notes |
|---|----------------|------|--------|-------|
| 1 | API responses | `backend/app/core/security_headers.py` | `csp_report_only` only | The policy is a fixed module constant. It admits **no** configurable API origin, and a test asserts no module under `backend/app` even mentions one — the API serves no HTML, so no browsing context loads from this origin. |
| 2 | Frontend container | `infrastructure/docker/nginx.conf` | `CSP_HEADER_NAME`, `CSP_CONNECT_SRC_API` | An nginx **template**. `Dockerfile.frontend` installs it at `/etc/nginx/templates/default.conf.template` and sets `NGINX_ENVSUBST_FILTER=^CSP_`, so the entrypoint expands only those two names and the `$uri` references reach nginx unchanged. `CSP_CONNECT_SRC_API` must begin with a **space** — it is concatenated directly onto the `connect-src` list — and is **enforced**: `/docker-entrypoint.d/05-require-csp-connect-src-api.sh`, written into the image by the Dockerfile, exits non-zero when it is empty or unspaced, and the entrypoint runs those scripts under `set -e`, so the container does not start (D102). |
| 3 | Load-balancer edge | `local.content_security_policy` in `infrastructure/terraform/main.tf`, attached through `local.security_response_headers` | `var.api_origin`, `var.csp_report_only` | The policy is assembled from `local.csp_connect_src_sources`; `local.csp_header_name` picks the header name. **This is the authoritative point for the deployed site**, because the compiled application is published to a bucket and served by the load balancer, not by the container. |
| 4 | Compiled document | `frontend/public/index.html` | none — fixed | A `<meta http-equiv>` loading policy that deliberately names neither `connect-src` nor `default-src`, so it cannot constrain any API origin. It is a subset of the module's policy in every directive but one: `style-src-elem` also admits `'unsafe-inline'`, so the development server — which sends no response header — does not refuse the stylesheet `style-loader` injects. Both delivered paths add a header that still says `'self'`, and the effective policy is the intersection, so production is unaffected (D99). |

**Two platform facts that decide how you edit these.** A `<meta>` element cannot carry
`Content-Security-Policy-Report-Only`, so delivery point 4 is *always enforced* and
`csp_report_only` does not reach it — which is exactly why it omits `connect-src`. And a browser
applies **every** policy it receives, so the effective policy is the **intersection**: widening
one delivery point alone changes nothing. Adding a script host to the API middleware and not to
the Terraform local will still block the script.

**The two configuration inputs:**

- **`api_origin`** — a *delivery-side* input, not a backend setting. Set the Terraform variable
  (and `CSP_CONNECT_SRC_API` if you use the container) to the exact origin of the frontend build's
  `REACT_APP_API_BASE_URL`. It is **required and may not be empty**, and it may not be
  `https://<domain_name>`: the edge routes no API path, so the API must have its own origin. An
  empty value fails `terraform plan` on the variable's own validation, not later at runtime.
- **`csp_report_only`** — flips both header names, at the API middleware and at the edge. Ship a
  widened policy in report-only mode first, read the violation reports, then enforce. It does not
  reach the document policy.

**What will fail if you get it wrong.** These tests read the files directly, so a divergence is a
test failure rather than a browser bug found later:

- `TestStaticDeliveryPolicies` — every directive the document policy names must also be served and
  must admit at least what the served copy admits, and they may differ in **exactly one** place:
  `style-src-elem` in the document may carry `'unsafe-inline'` and nothing else may diverge, while
  all three header producers must keep `style-src-elem 'self'`. Also: the document must name
  neither `connect-src` nor `default-src`, must still set the referrer policy, must carry no
  `<script>` element of its own and no repository path; the nginx template must contain
  `${CSP_CONNECT_SRC_API}`; the Terraform must reference `var.api_origin`; and all four Firebase
  endpoints must appear in the served policies.
- `TestSecurityResponseHeaders` — the canonical set on a success, a `401`, a `429`, a CORS
  preflight and an unhandled `500`, and that `csp_report_only` selects the other header name.
- `TestPublishedSecurityDocumentation` — `SECURITY.md` must publish the same policy text, the same
  header values, both header names and the same two counts. **If you change the directive list,
  update `SECURITY.md` in the same commit**, including the words "thirteen directives" if the count
  moves.

### Where rationale goes

Two conventions are enforced here:

- **Rationale belongs in the [Security Decision Log](./Security%20Decision%20Log.md), never in a
  code comment.** A comment states the threat closed or the contract enforced; the log explains
  why that approach was chosen over the alternatives. Terraform `description` and `error_message`
  strings are treated as user-facing documentation and are exempt.
- **A security control change needs a matching test.** Several tests in `test_security.py` read the
  Terraform, Nginx, `index.html` and deployment files directly and assert them *against each other*,
  so a control weakened in configuration fails a test rather than drifting quietly. **Some tests do
  read Markdown**, and an earlier version of this section flatly denied it — which was wrong in the
  direction that costs time, because it told a reader that documentation could not fail a test.
  `TestPublishedSecurityDocumentation` asserts `SECURITY.md` publishes the header values, both
  policy header names, the policy text and the two counts the code produces;
  `TestOperatorFacingClaims` reads `README.md`, `SECURITY.md` and this guide; and
  `TestDocumentedFactsMatchTheCode` reads all five documents, including an **equality** assertion on
  the published test-case count. What is *not* covered is everything else in them, so changing a
  published value — a response header, a database name, a TLS mode, an IAM role — still means
  updating `SECURITY.md`, `.env.example` and this guide in the same commit.

---

## Next tasks

Discovered while doing the security work, out of its scope, and worth doing. Ordered by value.

The first two are **blocking**: until both are done, nothing this project delivers can be observed
in a running process, and no item below them can be tested end to end. They are stated first for
that reason and not because they are the most interesting.

1. **Make the backend importable, startable and queryable.** Four separate gaps, all in the same
   critical path:
   - add the `__init__.py` files the `backend.app.*` imports assume, or correct the five route
     modules to import `get_db` from `backend.app.db.database` (see
     [Add a protected endpoint](#add-a-protected-endpoint));
   - define `init_db` in `backend/app/db/database.py`, which `backend/app/main.py` imports;
   - implement `WorkbookService`, `WorksheetService`, `CellService` and `CollaborationService`,
     which the four route modules already import and call;
   - create the tables. There is no migration tool and no committed DDL, and `Base.metadata.create_all`
     is never called, so even a startable process finds no `users` table — which means authentication
     fails after the token verifies. `scripts/deploy.sh` still carries an unresolved "specify the
     migration tool" placeholder.

   Also correct `Dockerfile.backend`'s `uvicorn main:app` to `backend.app.main:app`. Every control
   the security work delivered is present in source and asserted by tests against the real modules;
   this item is what makes it present in a *deployment*.
2. **Upgrade the Python runtime off 3.9.** The highest-value security task once the application
   runs. It is not hygiene: of the 14 outstanding dependency advisories, **13 have a published fix
   and every one of those fixes requires Python 3.10 or newer**, so none of the 13 can be closed
   until this happens — measured by attempting each fix version against the pinned interpreter,
   which refuses all 11 distinct versions. Doing it means migrating `config.py` to Pydantic 2 with
   `pydantic-settings`, or pinning `pydantic<2` on a newer interpreter.

   The exit criterion is **13 of the 14 baseline entries removed from `.github/workflows/ci.yml`,
   not all 14.** The fourteenth is `ecdsa` PYSEC-2026-1325, which has no published fix on any
   runtime and so survives the upgrade untouched; it reaches the tree transitively through
   `python-jose`, which the plan retains deliberately. Closing that one is a separate decision
   about `python-jose` itself — see item 25 — so an upgrade measured against "the baseline is
   empty" would look like a failure when it had in fact done everything available to it.
3. **Scope every query by owner.** Close the authenticated cross-tenant access: no handler
   consults `Workbook.owner_id`, and `current_user` is referenced zero times in all five handler
   bodies. It is not read-only — a caller who owns nothing can write cells into another user's
   worksheet, share another user's workbook with a third party, and attribute a new workbook to
   somebody else through the body's `owner_id`. Residual 1 in
   [SECURITY.md](../SECURITY.md#residual-risks) states the measured scope facet by facet. Doing it
   properly means building the service layer item 1 also needs, so the two are naturally done
   together.
4. **Give `Worksheet` the `named_ranges` attribute the schema reads.** The `cells` half of this is
   done: `WorksheetSchema.cells` is a map keyed by cell reference while the ORM holds a list of row
   and column rows, and a `pre` validator on the field now projects one onto the other, so
   `GET /workbooks/{id}/worksheets` answers `200` for a populated worksheet instead of `500`.
   `TestOrmSeam::test_the_worksheets_route_serializes_a_populated_worksheet` drives the real route
   over real tables and pins it. What is still open is `named_ranges`: `Worksheet` declares no such
   attribute, so the field a response advertises is always `null`, and it passes today only because
   the field is optional. Giving it a real value means a column and therefore the migration tooling
   item 1 establishes, which is why it was left —
   `TestKnownResiduals::test_a_worksheet_row_still_carries_no_named_ranges` pins that half.
5. **Add a `firebase_uid` column to `User`** and key identity on it instead of `email`. Needs the
   migration tooling item 1 establishes. Until then a Firebase account that never proved control of
   an address can authenticate as the local user holding it.
6. **Populate `ownerUid` on Firestore documents.** `backend/app/services/real_time_sync.py`
   writes workbook documents with no owner or collaborator field, so the rules correctly deny all
   browser access until this exists. Real-time collaboration cannot work before it.
7. **Fix `frontend/package.json`** and commit a `package-lock.json` — that unblocks the frontend
   build, `npm ci` and `npm audit` in one step.
8. **Repair `infrastructure/terraform/outputs.tf`** so `terraform validate` passes and
   infrastructure changes can be machine-checked.
9. **Correct the CI/CD wiring**: align the workflow names so CD triggers, and replace the Node
   `build` job with one that actually runs `pytest`.
10. **Add a collaborators table.** `POST /workbooks/{id}/share` has no persistence model behind
   it, and `backend/tests/test_collaboration.py` documents the intended owner / shared-user /
   admin semantics against an `AccessControl` class that does not exist.
11. **Strengthen transport**: `sslmode=verify-full` or the Cloud SQL connector, so the server's
    identity is verified and not merely the channel encrypted.
12. **DONE — exception text is no longer returned.** `cells.py` and `collaboration.py` used to put
    `str(e)` in the HTTP error detail. Each now answers a fixed message at its unchanged status and
    records the cause server-side with `logger.exception`, and a refusal the service raised
    deliberately is re-raised untouched rather than being flattened into the catch-all. Kept in
    place, numbered, because this list is cited by position.
13. **Harden the container**: add a `.dockerignore` (the backend image's `COPY . .` currently
    ships `.git`), run as a non-root `USER`, and add a `HEALTHCHECK`. Base-image digest pinning is
    **half done** — `Dockerfile.frontend`'s nginx serving stage carries a digest alongside its tag,
    and a test refuses a tag without one, but `Dockerfile.backend`'s `python:3.9-slim` and
    `Dockerfile.frontend`'s `node:14` build stage still float. Those two are out of this change
    set's scope because they also pin the end-of-life runtimes item 1 replaces, so pin them as
    part of that upgrade rather than pinning a base image that is about to be discarded.
14. **Tag images per release.** They are tagged `:latest` and overwritten, so there is no image
    to roll back to.
15. **Replace the long-lived CI service-account key** (`secrets.GCP_SA_KEY`) with Workload
    Identity Federation. The interactive `gcloud auth login` in `deploy.sh` no longer blocks an
    unattended run — it is reached only when `gcloud auth list` finds no active account — but the
    prompt is still *present*, so a CI runner with no credential configured stalls on it rather
    than failing fast. Once the federation lands, replace the branch with an assertion.
16. **Add container image scanning and Dependabot**, neither of which exists.
17. **Codify network segmentation and add audit logging** — no VPC, subnet, firewall rule or
    NetworkPolicy is declared, and there is no audit logging or customer-managed encryption key.
18. **Rewrite `scripts/setup_dev_environment.sh`.** It issues Django `manage.py migrate` and
    `runserver` commands against a FastAPI application and installs from a root
    `requirements.txt` that does not exist. Follow this document instead.
19. **Commit `infrastructure/terraform/.terraform.lock.hcl`.** The `~> 7.0` constraint bounds the
    provider's major version, but drift inside it can still land between a plan and an apply. A
    committed lock cannot be added until `outputs.tf` is repaired, because `init` currently has to
    run against a configuration that does not validate.
20. **Pin artifact hashes in `backend/requirements.txt` and make the audit gate strict.** The
    manifest pins versions but not file contents, and the audit gate is delta-only until the
    runtime upgrade lands. Both depend on item 1.
21. **Make the `@/…` alias resolve at build time.** `frontend/tsconfig.json` maps `"@/*"`, which
    satisfies `tsc`, but `react-scripts` honours `baseUrl` and ignores `paths`, so a webpack build
    still cannot resolve those imports. Either add a webpack alias or rewrite them relative.
22. **Add the missing `@/components` and `@/pages` barrel files** and the store's typed
    `useAppSelector` / `useAppDispatch` hooks plus the selectors six modules import. These are the
    remaining reasons the frontend does not compile once the manifest gap is closed.
23. **Give `WorksheetSchema` an identifier field.** The cells route is addressed with the
    worksheet's *name*, because the frozen response schema carries no id, so names must be unique
    within a workbook and renaming one changes the address its cell writes use.
24. **Persist user settings, and build the worksheet and sharing screens.**
    `frontend/src/pages/Settings.tsx` applies its form to the store only because no route accepts
    it, and `GET /workbooks/{id}/worksheets` and `POST /workbooks/{id}/share` are live and
    authenticated with no caller at all.
25. **Decide whether `python-jose` stays.** It is pinned at `3.5.0`, safely above the
    `CVE-2024-33663` fix floor of `3.4.0`, so it carries no *known* unpatched flaw of its own. Two
    things are nevertheless worth a deliberate decision rather than drift. It is the only reason
    `ecdsa` is in the tree, and `ecdsa` PYSEC-2026-1325 has no published fix on any runtime — so
    that advisory outlives the runtime upgrade in item 2 and is the one baseline entry that upgrade
    cannot remove. And the library is thinly maintained relative to the alternatives, which is the
    kind of supply-chain signal (CWE-1104) that matters most for a component sitting in an
    authentication path. The exposure today is genuinely small: after the Firebase bridge landed,
    the live verification path uses `firebase-admin`, and `python-jose` is reached only by
    `create_access_token` and the `legacy_jwt` verifier, neither of which any deployed client uses.
    Replacing it with `PyJWT` is roughly six lines. It was kept because the plan retains it by
    explicit instruction, so removing it is a decision to take openly rather than a cleanup.
26. **Make the default workbook page one query instead of 452.** `GET /workbooks` fetches each
    workbook's worksheets and each worksheet's cells per row, which is why the default page takes
    roughly 5.4 seconds and holds a pooled connection for the whole of it. That connection-holding
    is what made pool exhaustion reachable in the first place, so this is the deepest of the
    load-behaviour fixes even though it reads as a performance task. Two of its three symptoms are
    already gone: the page is capped at 100 rows so the statement count cannot grow with the table,
    and compression removed 98.66% of the bytes. The fix is eager loading (`selectinload` or
    `joinedload`) inside `WorkbookService.get_workbooks` — which means it is blocked on item 1,
    building the service layer.
27. **Revisit the pool and concurrency numbers together, never separately.** The request-concurrency
    bound is `DB_POOL_CAPACITY` by construction, and a test asserts the two are equal. That equality
    is the whole reason the bound works: it holds because each in-flight request needs at most one
    pooled connection at a time. If you resize the pool, resize the bound; if you make a request
    path hold two connections at once, the invariant is broken and the pool-exhaustion `500`s come
    back. The numbers live in `backend/app/db/database.py` with the measurements that chose them.


---

## Related documents

- [README](../README.md) — project overview and the current blocker list
- [SECURITY.md](../SECURITY.md) — controls, canonical response headers, residual risks
- [Security Decision Log](./Security%20Decision%20Log.md) — why each control is built the way it is
- [Security Traceability Matrix](./Security%20Traceability%20Matrix.md) — vulnerability to
  implementation to verification, both directions
- [Technical Specifications](./Technical%20Specifications.md) — system design reference
- `.env.example` — every configuration key, its accepted values and its read timing
