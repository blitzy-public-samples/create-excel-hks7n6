# Developer Onboarding

Everything needed to go from a clean machine to a running, modifiable checkout of this
application, and to keep working on it afterwards. Written to be followed rather than
skimmed: every command below was executed against this repository, and where a command does
not currently work, that is stated at the point you would run it rather than left for you to
discover.

Read [Setup](#1-setup) in order. The rest is reference: [Domain context](#2-domain-context)
for the shape of the system, [Common pitfalls](#3-common-pitfalls) when something behaves
strangely, [How to extend](#4-how-to-extend) when you add to it, and
[Suggested next tasks](#5-suggested-next-tasks) when you are looking for the next useful
piece of work.

## Who owns what

This document is one of several, and it deliberately does not repeat the others. Going to the
owner gets you the current answer; a copy here would drift.

| For | Read | Not here because |
|-----|------|------------------|
| A short orientation to the project | [README](../README.md) | It is the entry point and links here for depth. |
| The complete list of configuration keys, their meanings and their defaults | [.env.example](../.env.example) | It is the configuration surface. Reproducing the key list would guarantee the two disagree. |
| The canonical security response headers, and the residual risks this application still carries | [SECURITY.md](../SECURITY.md) | It is the single authoritative table for both. |
| **Why** any security control is shaped the way it is | [Security Decision Log](<./Security Decision Log.md>) | It is the single source of truth for rationale. This document cites it by identifier — `D13`, `DEV-2`, `R8`, `F4` — and never re-argues a decision. |
| Which control closes which finding, and the coverage count | [Security Traceability Matrix](<./Security Traceability Matrix.md>) | It maps and it counts; this document does neither. |
| The product requirements, including the `SECU-001` security requirement block | [Software Requirements Specifications (SRS)](<./Software Requirements Specifications (SRS).md>) | Requirement identifiers live there. |
| The intended architecture and technology choices | [Technical Specifications](<./Technical Specifications.md>) | Cite it by heading name — it has no numbered sections. |

If you find yourself asking "but why was it done that way?", the answer is in the Decision
Log under the identifier cited next to the fact. That separation is deliberate and is
required by the project's **Explainability** rule.

One more pointer, because it will save you an hour: `scripts/setup_dev_environment.sh` looks
like it should do everything in [Setup](#1-setup) for you. **It does not, and it is not
maintained for this application.** The procedure in this document supersedes it; the reasons
are in [Common pitfalls](#the-setup-script-is-wrong-for-this-application) and its repair is
next task 17.

---

## 1. Setup

### 1.1 Runtimes

| Runtime | Version | Where that version is documented |
|---------|---------|----------------------------------|
| Python | **3.9** | `infrastructure/docker/Dockerfile.backend` L2 — `FROM python:3.9-slim`. That is the *only* statement of the Python version in the repository: there is no `setup.py`, `pyproject.toml`, `tox.ini` or `.python-version`. |
| Node | **14** | Four places agree: `infrastructure/docker/Dockerfile.frontend` L14 (`FROM node:14 as build`), `.github/workflows/ci.yml` L15 (`node-version: [14.x]`), `scripts/setup_dev_environment.sh` L11 (`deb.nodesource.com/setup_14.x`) and [README](../README.md) L26. There is no `engines` field and no `.nvmrc`. |
| PostgreSQL | **13** | `infrastructure/terraform/main.tf`, `google_sql_database_instance.main` (L66). |

**Both runtimes are end-of-life.** Python 3.9 stopped receiving upstream security fixes on
31 October 2025 and Node 14 on 30 April 2023. This is not a cosmetic problem for Python:
every dependency advisory currently reported against this project has a fix version that
requires Python 3.10 or newer, so on 3.9 not one of them can be closed. That makes the
runtime upgrade the highest-value task available — next task 1 — with Node at next task 3.
Both are pinned by files outside the change set that introduced the current security
controls, which is why they are still here.

Install the versions above rather than whatever your machine already has. A newer Python will
appear to work and then diverge from what is deployed.

### 1.2 Virtual environment

From the repository root:

```bash
python3 -m venv venv
source venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

`venv/` is not tracked, but not because of a `.gitignore` — this repository has none (see
[Common pitfalls](#there-is-no-root-gitignore)). It is excluded per-clone through
`.git/info/exclude`. If `git status` starts listing `venv/`, `node_modules/` or build output,
append the offending path to that file using the same one-liner shown in
[§1.5](#15-configuration).

### 1.3 Backend dependencies

```bash
pip install -r backend/requirements.txt
```

**The manifest is at `backend/requirements.txt`, not at the repository root.** Getting this
wrong is the single most common setup mistake here, because two files in the repository
encourage it: `scripts/setup_dev_environment.sh` L22 installs from a root path where no
manifest exists, and `infrastructure/docker/Dockerfile.backend` L8 copies `requirements.txt`
relative to its own build context. Neither is a path you can run from the repository root.

Every version in that manifest is pinned exactly, including the transitive graph, and three
of the pins are load-bearing — relaxing any of them breaks the application rather than merely
changing it. They are called out in [Common pitfalls](#3-common-pitfalls); the reasoning is
`D14`, `D15` and `D16`.

### 1.4 Frontend dependencies

```bash
cd frontend
npm install
```

Use `npm install`, **not `npm ci`**: no `package-lock.json` is committed, and `npm ci`
requires one.

**This is not sufficient on its own.** `frontend/package.json` fails to declare six packages
the project needs — five that the source imports, plus `react-scripts`, which every one of the
four npm scripts invokes. So the install completes and the very next command fails. Until that
manifest is repaired (next task 9), supply them yourself — in a **single** command, because
`npm install --no-save` prunes packages added by an earlier `--no-save` run:

```bash
npm install --no-save react-scripts@5.0.1 @reduxjs/toolkit@1.9.7 react-router-dom@6.30.1 \
  firebase@10.14.1 mathjs@11.12.0 date-fns@2.30.0 @types/node@18.19.86
```

Two notes on that command. `@types/node` must be pinned to 18.x: the version hoisted
transitively is far newer and emits dozens of syntax errors against the TypeScript 4.9.5 that
`frontend/package.json` L25 declares. And `react-router-dom` is a genuine guess — nothing in
the repository declares a version, and `frontend/src/app.tsx` L2 imports `Switch` and uses
`component=`, which are the **v5** API, so v6 type-checks worse than v5 would. Deciding that
version is part of next task 9.

### 1.5 Configuration

```bash
cp .env.example .env
```

Then edit `.env`. [.env.example](../.env.example) is the complete configuration surface and
documents every key inline, so it is not repeated here. What you need to *decide* falls into
four groups:

- **The six required core settings** have no defaults. A missing one raises a Pydantic
  `ValidationError` before anything else happens, so the application does not start. Three of
  them (`DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`) are overwritten from Secret Manager at
  runtime and **must still be present in the environment anyway** — the mechanism is in
  [Common pitfalls](#the-secret-backed-settings-are-still-required-in-the-environment).
- **Values that must match a real cloud project**: the project ID, the uploads bucket, the
  Firebase project, the API origin and the CORS origin list. These are not free-form. Several
  of them are the same fact as a Terraform variable *and* a frontend build variable, and a
  deployment is only coherent when all three agree; `.env.example` carries the mapping table.
  Divergence does not fail loudly — it surfaces as refused sign-ins, CORS rejections, or a
  Content-Security-Policy that blocks every API call.
- **Local-only values**: `GOOGLE_CLOUD_PROJECT` and `GOOGLE_APPLICATION_CREDENTIALS`. On GKE
  the runtime's attached identity supplies credentials and no key file is shipped in the
  image; for local work you point at a key file yourself.
- **The rollback toggles** — the authentication verifier and enforcement switches, the
  throttling switches and thresholds, and the CSP mode. **Leave every one at its default.**
  Each default is the secure position, and each toggle exists for a rollback path described in
  [§4.5](#45-exercise-a-rollback). One of them reopens a fixed vulnerability in full when
  disabled (`D18`).

Two placement traps worth stating before you hit them:

- **The frontend build variables go in a different file.** `REACT_APP_*` values belong in
  `frontend/.env`, not in the backend `.env`. `react-scripts` inlines them into the compiled
  bundle at build time, so none of them is a secret — and `frontend/src/services/api.ts`
  throws, naming what is missing, when a required one is absent, which means the browser
  cannot obtain an ID token and every API call fails.
- **How you load `.env` matters.** Prefer `uvicorn --env-file .env`, which parses the file
  itself. Do not use `export $(grep -v '^#' .env | xargs)`: `xargs` word-splits on the spaces
  inside the JSON array that the origin allow-list needs, so the value reaches Pydantic
  malformed and validation fails.

Finally, protect the file you just created. `.env` holds `SECRET_KEY` and the database
password, and because there is no root `.gitignore` it will show up in `git status`. Add a
clone-local rule:

```bash
printf '.env\n' >> .git/info/exclude
```

That is a per-clone file and is not shared with anyone else, so every contributor has to do it
until a tracked ignore file is reintroduced (`DEV-4`, next task 12).

### 1.6 What runs today, and what does not

Be clear-eyed about this before you start changing things.

**The security test suite runs, and passes.** From the repository root:

```bash
python -m pytest backend/tests/test_security.py -q
```

79 tests pass. They need no environment variables and no `PYTHONPATH`:
`backend/tests/conftest.py` puts the repository root on `sys.path` (L65), populates a test
environment (L140), stubs Secret Manager (L241) and supplies the application names the
repository never defines (L457, `D32`). This is the fastest way to confirm your Python
environment is correct.

**The whole suite reports three collection errors and collects zero tests.**

```bash
python -m pytest backend/tests/ -q
```

That is the documented baseline, not damage you caused. All three causes are pre-existing
import errors, named individually in
[Common pitfalls](#the-test-suite-collects-zero-tests--three-collection-errors-is-the-baseline).
The standard to hold yourself to is **"no new collection error"**, not "the suite is green".

**The API server does not start yet.** The correct invocation, from the repository root, is:

```bash
uvicorn --env-file .env backend.app.main:app --reload --port 8000
```

It fails with `ImportError: cannot import name 'get_db' from 'backend.app.db'`, raised from
`backend/app/api/workbooks.py` L4 — and it fails that way **whether or not your configuration
is correct**, because `backend/app/main.py` imports the route modules at L3, before any line
that reads a setting. So a perfect `.env` does not get you past it, and seeing this error tells
you nothing about whether your configuration is right.

The cause: the route modules import from package namespaces — `backend.app.db`,
`backend.app.schema`, `backend.app.services` — and there is **no `__init__.py` anywhere under
`backend/`**, so those names cannot resolve. The test suite gets past it only because
`conftest.py` binds test-scope stand-ins (`D32`). Fixing it for real is next task 16, and until
that lands **there is no way to run the API server.** Use the security suite to validate
backend changes in the meantime.

To check your configuration separately — which is worth doing, since the server cannot do it
for you — import a module that builds `Settings` at import time:

```bash
python -c "import backend.app.db.database"
```

A missing core setting surfaces here as `pydantic.ValidationError` naming each absent field.
That command needs the repository root on `sys.path`, so run it from the repository root (or set
`PYTHONPATH` to it), and it needs the settings in the process environment rather than in a file
— `--env-file` is a `uvicorn` feature, not a Python one.

Note the module path in that command: **`backend.app.main:app`**. A bare `main:app` is wrong
from the repository root, and `infrastructure/docker/Dockerfile.backend` L20 ships exactly
that wrong form, so the container's start command is incorrect too — also next task 16.

**The frontend dev server starts but the application does not compile.**

```bash
cd frontend
npm start
```

This invokes `react-scripts start`, which needs the `--no-save` packages from
[§1.4](#14-frontend-dependencies). Even with them installed, `npx tsc --noEmit` reports 69
pre-existing errors; the causes are in
[Common pitfalls](#the-frontend-cannot-install-build-or-type-check).

### 1.7 Operator provisioning gates

Six things must be true outside the repository before the application can start or deploy.
**This repository creates no Secret Manager secrets and no cloud resources by itself**, so
these are actions somebody has to take, not values to fill in.

[.env.example](../.env.example) Section D enumerates the steps. The table below is the part
that document does not give you: what you will *see* when each gate has not been passed, so
you can recognise the failure instead of debugging it.

| Gate | Symptom when it has not been passed |
|------|-------------------------------------|
| **1. The three Secret Manager secrets exist** — `DATABASE_URL`, `REDIS_URL` and `SECRET_KEY` in the target project, each with a `latest` version. `backend/app/core/config.py` L210-212 reads all three on *every* `Settings()` construction. | An exception from Secret Manager during import of any backend module — not at first use. Nothing starts. |
| **2. The six core settings are in the environment** — including the three that Secret Manager overwrites. | `pydantic.ValidationError` naming the missing fields, raised before Secret Manager is contacted at all. `config.py` L200 calls `super().__init__(**kwargs)`, and validation therefore runs *before* the reads at L210-212. |
| **3. The signer grant is in place** — `roles/iam.serviceAccountTokenCreator` bound **on** the dedicated signer service account **with the API runtime account as member**, the runtime account reachable from the pod through GKE Workload Identity, and the IAM Service Account Credentials API enabled. Terraform declares both bindings (`main.tf` L196 and L204). | Uploads fail when a signed URL is generated: Application Default Credentials report that a private key is needed to sign. The direction of that binding matters — see `R8`; the permission's cost is `D3`. |
| **4. The managed TLS certificate is ACTIVE before the port-80 redirect is created.** This is a **two-stage apply**, and the order is the opposite of what intuition suggests: a Google-managed certificate is validated *through* the load balancer, so it cannot become ACTIVE until the 443 listener already exists. Stage one — leave `https_cutover_enabled` false (its default, `variables.tf` L266-270), apply, and point the domain's DNS A record at the reserved address. Stage two — once `gcloud compute ssl-certificates describe excel-app-ssl-cert` reports ACTIVE, set it true and apply again. Provisioning commonly takes up to an hour. | `scripts/deploy.sh` runs under `set -e` (L13) and its preflight compares the certificate's domain against `DOMAIN_NAME` (L153), so a certificate that is not yet ACTIVE and for the right host aborts the deployment before anything is published. That abort is the designed behaviour, not a bug. |
| **5. The CORS origin list and Identity Platform's authorized domains agree.** Terraform derives the authorized domains from its `allowed_origins` variable, so keep the backend list equal to it (`D26`). | Sign-in and CORS disagree about which origins are legitimate: sign-in succeeds and the API refuses the same origin, or the reverse. Neither failure message names the other list. |
| **6. Promotion is manual.** `.github/workflows/cd.yml` L4-5 triggers on a workflow named `Continuous Integration`, while `.github/workflows/ci.yml` L1 is named `CI`. **Continuous deployment never fires.** | Nothing. That is the danger: a merge appears to deploy and silently ships nothing. Deploy by running `scripts/deploy.sh` deliberately. Next task 14, which carries a trap of its own. |

---

## 2. Domain context

### 2.1 The system in one paragraph

A React 18 / TypeScript single-page application, compiled and published as **static files to
a Cloud Storage bucket**, calling a Python 3.9 FastAPI service on Google Kubernetes Engine.
The service reaches Cloud SQL PostgreSQL 13 through SQLAlchemy, Firestore for collaboration
state, Cloud Storage for uploaded workbooks, and Secret Manager for secrets. End users sign
in through Firebase Authentication / Identity Platform. The intended architecture is described
in [Technical Specifications](<./Technical Specifications.md>) under `Key Technologies` (around
L63).

The consequence worth internalising: **the compiled application and the API are two different
origins**, served by two different mechanisms. That single fact explains why the CORS allow-list,
the `connect-src` directive of the Content-Security-Policy and the API origin all have to be
configured consistently, and why they are each expressed in more than one place.

### 2.2 The browser talks to Firestore directly

`frontend/src/services/collaboration.ts` subscribes to a workbook document straight from the
browser (L13) — it does not go through the API. Nothing server-side sits on that path, so no
server-side control can govern it.

**That is why `firestore.rules` exists.** It is not defence in depth layered on top of an API
check; it is the only control available on that path. If you change how the browser reads or
writes collaboration state, the rules file is where the authorization for it lives.

The corollary makes the rules safe to deploy: `backend/app/services/real_time_sync.py` L6 uses
`firestore.Client()`, a **server** client library, and server client libraries bypass security
rules entirely and authenticate through Application Default Credentials. **The rules cannot
break backend writes.** Their blast radius is confined to the browser path.

### 2.3 The document shape the rules expect, and the current mismatch

[firestore.rules](../firestore.rules) matches `/workbooks/{workbookId}` (L10) and expects each
document to carry:

- `ownerUid` — a non-empty string, the owner's immutable Firebase UID
- `collaboratorUids` — a list of strings

Authorization keys on `request.auth.uid`, never on an email address. Reads are allowed to the
owner or a listed collaborator (L142); creation must name the caller as owner (L146-149); an
update may not rewrite the ownership fields, and a collaborator may only touch a fixed field
list (L153-154, `D34`).

**Nothing populates those fields today.** `real_time_sync.py` L11 addresses
`workbooks/{workbook_id}` correctly but L12-14 writes only the dotted cell path
`worksheets.{worksheet_id}.cells.{cell_id}` — no ownership field at all. The rules therefore
**deny all browser access**, which is the intended fail-closed outcome (`D12`) rather than a
bug to work around. Do not "fix" it by loosening the rules; populate the fields. That is next
task 4.

Note the deliberate asymmetry while you are here: the rules key on the immutable UID, while
the server's identity bridge resolves a local user by the verified `email` claim. The reason
is `D2`, and closing it is next task 5.

### 2.4 The domain hierarchy

Four tables in `backend/app/db/models.py`, each owning the next:

- **`User`** (L7) — integer primary key (L10), unique non-null `email` (L11), `name`,
  `created_at`
- **`Workbook`** (L19) — `owner_id` foreign key to `users.id` (L24), `name`, timestamps, a
  JSON `settings` blob
- **`Worksheet`** (L32) — `workbook_id`, `name`, and an explicit `order` (L38), so worksheets
  are ordered rather than a set
- **`Cell`** (L46) — `worksheet_id`, `row` and `column` (L51-52), plus `value`, `formula` and a
  JSON `style`

Cells are addressed by numeric row and column. The API schema, by contrast, presents a
worksheet's cells as a map keyed by cell reference — those two shapes are not the same, and
nothing currently projects between them. See
[§5.1](#51-known-defects-that-are-not-security-issues).

### 2.5 The API surface

Five endpoints, all mounted **without a URL prefix** by `backend/app/main.py` L32-36, and all
now requiring an `Authorization: Bearer <Firebase ID token>` header:

| Method and path | Handler |
|-----------------|---------|
| `GET /workbooks` | `backend/app/api/workbooks.py` L14 |
| `POST /workbooks` | `backend/app/api/workbooks.py` L23 |
| `GET /workbooks/{workbook_id}/worksheets` | `backend/app/api/worksheets.py` L14 |
| `PUT /workbooks/{workbook_id}/worksheets/{worksheet_id}/cells` | `backend/app/api/cells.py` L16 |
| `POST /workbooks/{workbook_id}/share` | `backend/app/api/collaboration.py` L14 |

Each handler carries `current_user: User = Depends(get_current_user)` and nothing else. A
request without a valid token is refused with 401 and a `WWW-Authenticate: Bearer` challenge
before the handler body runs.

The client attaches the credential in an Axios request interceptor,
`frontend/src/services/api.ts` L154, which reads the token from the Firebase SDK
(`auth.currentUser?.getIdToken()`, L157) rather than from Redux. That matters in practice: the
Redux store is not persisted, so a page reload resets it — reading from the SDK means the
credential survives the reload. The interceptor refuses to send a request at all when no
credential is available.

The `PUT .../cells` endpoint is invoked on cell edits and is by far the highest-frequency
route. Keep that in mind whenever you touch throttling ([§4.3](#43-retune-the-rate-limits)).

### 2.6 Where the security controls live

An orientation map, not a specification:

| Concern | Artifact |
|---------|----------|
| Token verification, identity resolution, the enforcement bypass marker | `backend/app/core/security.py` (`get_current_user` at L479) |
| Every setting and every rollback toggle | `backend/app/core/config.py` |
| CORS, throttling and header registration | `backend/app/main.py` L42-48 — registration order is a requirement, not an accident (`D10`, `D25`) |
| Request throttling | `backend/app/core/rate_limit.py` (`register_rate_limiting` at L225) |
| Security response headers on the API | `backend/app/core/security_headers.py` — the canonical directive set |
| Security headers on the static application | `infrastructure/docker/nginx.conf` L33-38 and the load balancer's custom response headers in Terraform (`D9`) |
| Database transport | `backend/app/db/database.py` L8-10, plus the Cloud SQL instance in Terraform |
| Object access | `backend/app/services/file_storage.py` L50-53, plus the bucket policy in Terraform |
| Browser-path authorization | [firestore.rules](../firestore.rules) |

For the header names and their exact values, read [SECURITY.md](../SECURITY.md). It is the
single authoritative table and this document does not copy it.

### 2.7 There is exactly one deployment path

**Terraform owns the HTTPS edge outright** — the reserved address, the managed certificate,
the backend bucket, the URL maps, the target proxies and the forwarding rules.
`scripts/deploy.sh` creates none of them; its preflight (from L69) only reads their state and
aborts before the first mutation if anything is missing or inconsistent. Apply Terraform
first, always.

Three consequences you need to respect:

- **The compiled application is published to exactly one bucket** — the Terraform-managed
  static-assets bucket that the load balancer's backend bucket serves (`deploy.sh` L353).
- **Never advertise a direct Cloud Storage URL for the application.** The security response
  headers are added at the load balancer, so a direct bucket URL is a header-free path to the
  same content. Advertise only the load-balancer domain.
- **`DOMAIN_NAME` is required** (`deploy.sh` L39) and must equal Terraform's `domain_name`
  variable. There is no default, because reaching the application any other way is not
  supported.

The rationale for consolidating on Terraform is `R12`.

### 2.8 Two service accounts, not one

Object signing involves **two** identities and it is easy to collapse them:

- The **signer** service account is the identity whose name appears in the signature.
- The **runtime** service account is the identity the pod actually runs as.

`roles/iam.serviceAccountTokenCreator` is bound **on the signer** with the **runtime as
member** (`infrastructure/terraform/main.tf` L196), and the runtime is reachable from the pod
through GKE Workload Identity (L204). Binding it the other way round — the signer permitted
to impersonate itself — looks plausible and produces a deployment that cannot sign anything.
The direction is `R8`; the project-equality constraint on both addresses is `R9`.

Four Terraform variables have to match the deployed pod spec: `signer_service_account` (L168),
`runtime_service_account` (L182), `kubernetes_namespace` (L197) and
`kubernetes_service_account` (L208). No key file is downloaded or shipped in any image.

### 2.9 Throttling counts in a shared store

The request throttling counters live in a **shared** store, derived from the Redis URL unless
`rate_limit_storage_uri` overrides it (`backend/app/core/config.py` L89). Counting in process
memory instead would multiply every client's effective quota by the number of workers and
pods serving the API, which is a limit in name only. If you run the API with more than one
worker or replica, the store must be shared and reachable, or throttling does not mean what
its configuration says (`R28`).

`rate_limit_trusted_proxy_hops` (L93) defaults to `0`, which identifies a client by its socket
address only, so a forwarded header cannot be spoofed. Raise it deliberately, and only to the
number of proxy hops actually in front of the API.

---

## 3. Common pitfalls

Each of these cost real time during the work that produced the current security controls.
They are ordered roughly by how soon you will meet them.

### Importing any backend module performs live Secret Manager reads

**Symptom.** A plain `import` of something that looks inert reaches out to Google Cloud, and
fails or hangs when credentials or the network are not available.

**Cause.** `backend/app/core/config.py` L209 opens a `SecretManagerServiceClient` and
L210-212 make three `access_secret_version` calls — on **every** `Settings()` construction.
`get_settings()` (L219-220) is **not cached**, so it re-reads on every call. Anything that
imports `backend.app.core.security` or `backend.app.db.database` pulls this in transitively.

**What to do.** In tests, let `backend/tests/conftest.py` handle it — it stubs the client
(L241) and overrides the settings factory (L256). Outside tests, make credentials available.
Do not add caching to `get_settings()`: it is uncached on purpose, and the rollback behaviour
in [§4.5](#45-exercise-a-rollback) depends on that.

### The SQLAlchemy engine is bound at import time

**Symptom.** `sqlalchemy.exc.ArgumentError: Could not parse SQLAlchemy URL` from what looks
like a harmless import, with no database call anywhere in your code.

**Cause.** `backend/app/db/database.py` reads settings at module scope (L5) and constructs the
engine at module scope (L8-10). An unparseable URL therefore fails at **import**, not at
connect. The single settings read is deliberate — two reads would mean six Secret Manager
calls and could take the URL and the TLS mode from two different fetches (`R24`).

**What to do — and this is the part that wastes an afternoon: fix the value in Secret Manager,
not the one in `.env`.** `Settings.__init__` overwrites `DATABASE_URL` with the secret's
contents (`backend/app/core/config.py` L215) *after* validation, so the string that actually
reaches `create_engine` is the secret's, and the environment value is only ever used to satisfy
validation. A perfectly good URL in `.env` therefore does nothing to prevent this error.

Two corollaries. If the secret is unreachable rather than malformed, the failure surfaces as a
Secret Manager error from the same import instead. And because the engine binds the TLS mode at
import, changing `db_sslmode` needs a process restart, not just a new request.

### The secret-backed settings are still required in the environment

**Symptom.** `pydantic.ValidationError` naming `DATABASE_URL`, `REDIS_URL` or `SECRET_KEY` as
missing — even though you know they come from Secret Manager and are about to be overwritten.

**Cause.** `config.py` L200 calls `super().__init__(**kwargs)` **before** the secret reads at
L210-212. Pydantic validation runs first, so a field with no default and no environment value
fails before Secret Manager is contacted at all.

**What to do.** Set all six core settings in the environment. Placeholder values are fine for
the three that get overwritten; they only have to satisfy validation. `.env.example` flags each
one.

### Pydantic must stay below version 2

**Symptom.** `ImportError` on the very first line of the configuration module.

**Cause.** `backend/app/core/config.py` L1 is `from pydantic import BaseSettings, conint,
validator`. `BaseSettings` moved to a separate distribution in Pydantic v2, so a v2 resolution
cannot import it. The manifest pins `pydantic==1.10.26`; see `D16`, and next task 20 for the
migration.

### `passlib` breaks against `bcrypt` 5.x

**Symptom.** `AttributeError: module 'bcrypt' has no attribute '__about__'`, then
`ValueError: password cannot be longer than 72 bytes`.

**Cause.** `passlib` 1.7.4 cannot read the version of `bcrypt` 5.0.0, and
`backend/app/core/security.py` L87 builds its `CryptContext` at module scope — so an unpinned
`bcrypt` breaks the module on import, not on first use. The manifest pins `bcrypt==4.0.1`
(`D15`).

**What to do.** Install from `backend/requirements.txt` and do not upgrade `bcrypt`
independently.

### The test suite collects zero tests — three collection errors is the baseline

**Symptom.** `python -m pytest backend/tests/ -q` ends with
`Interrupted: 3 errors during collection` and `no tests collected`.

**Cause.** Three pre-existing import errors, one per legacy module:

| Module | Line | Import that fails |
|--------|------|-------------------|
| `backend/tests/test_collaboration.py` | L3 | `from backend.collaboration import RealTimeSync, ConflictResolver, AccessControl` — no such module exists |
| `backend/tests/test_api.py` | L3 | `from app.main import app` — the package path is `backend.app.main` |
| `backend/tests/test_calculation_engine.py` | L2 | `from calculation_engine import CalculationEngine` — a bare top-level name |

Underneath all three: `find backend -name "__init__.py"` returns **nothing**. There is no
package marker anywhere under `backend/`.

**What to do.** Nothing — but measure yourself correctly. The regression standard is
**"no new collection error"**, not "the suite is green". A report claiming a green suite after
a change would be inaccurate. Your own work belongs in `backend/tests/test_security.py`, which
does run ([§4.6](#46-add-a-security-test)).

### `terraform validate` does not pass on a clean checkout

**Symptom.** First `Error: Missing required provider`; after `terraform init`, seven
`Error: Reference to undeclared resource`.

**Cause.** Two separate things.

1. No provider lock file is committed, so `terraform init` must run first. It is deliberately
   not committed — a lock pinning an uncached provider version blocked validation outright
   (`R28`).
2. `infrastructure/terraform/outputs.tf` references **seven** resources that `main.tf` does
   not declare, spread across three output blocks: `google_storage_bucket.raw_data` (L10),
   `.processed_data` (L11) and `.model_artifacts` (L12);
   `google_cloudfunctions_function.data_ingestion` (L24), `.data_processing` (L25) and
   `.model_training` (L26); and `google_firestore_database.main` (L32). What `main.tf` actually
   declares is `google_storage_bucket.static_assets` (L98) and `.user_uploads` (L116),
   `google_cloudfunctions_function.excel_app_function` (L376) and
   `google_firestore_database.database` (L420). The same file also fails
   `terraform fmt -check`.

```bash
terraform -chdir=infrastructure/terraform init -backend=false
terraform -chdir=infrastructure/terraform validate   # expect the seven errors above
```

**What to do.** Recognise those seven as the known baseline and treat any *other* error as
yours. Because validation cannot pass, infrastructure changes here need a human reading of
`terraform plan` output rather than a machine gate. Repairing the file is next task 10.

### The frontend cannot install, build or type-check

**Symptom.** `react-scripts: not found`, or unresolved module errors for packages you can see
being imported.

**Cause.** Three independent pre-existing defects.

1. `frontend/package.json` declares neither `react-scripts` — which all four npm scripts
   invoke (L34-37) — nor `firebase`, and these module specifiers are imported across
   `frontend/src` with nothing declaring them: `firebase/app`, `firebase/auth`,
   `firebase/firestore`, `@reduxjs/toolkit`, `react-router-dom`, `mathjs` and `date-fns`.
2. No `package-lock.json` is committed, yet `.github/workflows/ci.yml` L27-29 runs `npm ci`
   three times, which requires one.
3. `frontend/tsconfig.json` L10 sets `"baseUrl": "src"`, and the source imports across that
   boundary in two ways that cannot resolve. Four modules import Python-side schema types from
   `backend/app/schema/workbook_schema`, all at L2: `frontend/src/services/api.ts`,
   `services/auth.ts`, `services/collaboration.ts` and `utils/formulaParser.ts`. And most
   components import a `@/…` alias prefix that the `paths` map (L11-17) never declares — it
   declares `@components/*`, `@utils/*`, `@hooks/*`, `@services/*` and `@types/*` instead, so
   `@/store` and `@/components` resolve to nothing. `npx tsc --noEmit` currently reports 69
   errors, 4 from the first cause and 37 from the second.

**What to do.** Use the `--no-save` command in [§1.4](#14-frontend-dependencies) to get
moving. This is next task 9, and it **blocks all frontend verification** — including the
dependency audit in CI, which is why that step is marked known-blocked rather than made
blocking (`D30`).

### CI never runs the Python tests

**Symptom.** A green CI run that has not executed a single Python test.

**Cause.** `.github/workflows/ci.yml` L36-39 does `cd backend` and then `npm test`, and
`backend/` has no `package.json`. The pre-existing `build` job cannot invoke `pytest` at all.

**What to do.** Look at the `security-checks` job (from L68) instead — it is the one that runs
`pytest backend/tests/test_security.py` (L118) along with the dependency audits. It
deliberately declares no dependency on `build`, because a dependency on a job that cannot
succeed would mean the security gates never run (`D29`). Repairing `build` is next task 14.

### Continuous deployment never fires

Covered as gate 6 in [§1.7](#17-operator-provisioning-gates). Deploy by running
`scripts/deploy.sh` deliberately, and note the trap recorded in next task 14: renaming
`ci.yml` alone would silently *start* firing CD.

### The setup script is wrong for this application

**Symptom.** `scripts/setup_dev_environment.sh` runs partway and then fails, or appears to
succeed while having installed nothing useful.

**Cause.** It targets a different application. `pip install -r requirements.txt` at L22 uses a
root path with no manifest. L54 runs `python manage.py migrate` and L60 tells you to start the
server with `python manage.py runserver` — Django commands against a FastAPI application that
has no `manage.py`. Three steps are unresolved `HUMAN ASSISTANCE NEEDED` placeholders: the
Cloud SDK setup (L38-39), populating the environment file (L44-45) and initialising the local
database (L49-50).

**What to do.** Follow [§1](#1-setup) instead; it supersedes the script. One line of the script
does now work that previously did not: L43 `cp .env.example .env` resolves, because
`.env.example` exists. Repairing the script is next task 17.

### There is no database migration tooling

**Symptom.** You need a schema change and cannot find how to apply one.

**Cause.** There is none. `scripts/deploy.sh` L411-412 still carries an unresolved
`HUMAN ASSISTANCE NEEDED` placeholder asking for the migration tool and commands.

**What to do.** Understand the knock-on effect before proposing a schema change: it is why the
identity bridge resolves a user by the verified email claim instead of adding a UID column
(`D2`), and why several next tasks are blocked. Adopting a tool is part of next task 5.

### There is no root `.gitignore`

**Symptom.** `git status` lists `venv/`, `node_modules/`, build output — or worse, a populated
`.env`.

**Cause.** The root ignore file was deliberately withdrawn from the change set that introduced
the current security controls (`DEV-4`). `.git/info/exclude` carries clone-local rules for the
common build artifacts, but it is per-clone and not shared.

**What to do.** Add `.env` to `.git/info/exclude` as shown in [§1.5](#15-configuration), and
never stage it. Reintroducing a tracked ignore file is next task 12.

### Two referenced directories do not exist

`scripts/deploy.sh` L367-368 applies Kubernetes manifests from the directory named at L52,
which defaults to `k8s` — and `k8s/` is not in the repository. `.github/workflows/cd.yml` L49
and L62 reference `k8s/staging/` and `k8s/production/`. A `functions/` directory for the Cloud
Function source is also absent. Deployment steps touching those paths cannot succeed as
written; supply them out of band or expect the step to fail.

### Most security verification needs cloud tooling

Only four checks run locally: `pytest`, a plain `import` of a backend module, `py_compile`, and
`bash -n` on the shell scripts. Everything that confirms a deployed control — bucket policy,
Cloud SQL TLS mode, edge headers, the TLS redirect, the Cloud Function invoker, the Firestore
rules — needs `gcloud`, `gsutil`, `psql` or the `firebase` CLI. Expect to do that verification
against a deployed environment, not on your laptop.

### Database TLS is not visible on the engine object

**Symptom.** You set `db_sslmode`, inspect the engine, and cannot find it — and conclude the
setting is being ignored.

**Cause.** SQLAlchemy merges `connect_args` at *connect* time. They do not appear on the engine
object, and they are not in `dialect.create_connect_args`.

**What to do.** Assert on the `create_engine` call itself, or check the live server with
`SHOW ssl;` or `SELECT * FROM pg_stat_ssl;`. Note also that the setting is constrained to the
modes that cannot negotiate plaintext, so a typo is a start-up failure rather than a silent
downgrade (`R23`); what `require` does and does not guarantee is `D7`.

### On Windows, use Git Bash rather than WSL Bash for the shell scripts

**Symptom.** `bash -n scripts/deploy.sh` reports syntax errors that are not there.

**Cause.** The default `bash` may be WSL's, which cannot read Windows absolute paths; combined
with a CRLF working tree it reports phantom errors.

**What to do.** Use `C:\Program Files\Git\bin\bash.exe`. Both shell scripts pass `bash -n`
under it.

---

## 4. How to extend

### 4.1 Add a protected route

1. Create or extend a module under `backend/app/api/` and define an `APIRouter`.
2. Mount it from `include_routers()` in `backend/app/main.py` (L32-36). Routers are mounted
   without a prefix; the full path lives in the route decorator.
3. Attach the authentication dependency, importing `get_current_user` from
   `backend/app/core/security.py` and `User` from `backend/app/db/models.py`:

```python
from backend.app.core.security import get_current_user
from backend.app.db.models import User

@router.get('/workbooks/{workbook_id}/charts')
def get_charts(
    workbook_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ChartSchema]:
    ...
```

A dependency parameter does not appear in the request schema, so adding one changes no API
contract — no path, method, request body or response model is affected.

**Two standing constraints.** Handlers are thin delegators: do not put business logic in
them. And there is deliberately **no in-handler ownership check** anywhere in this codebase,
which means an authenticated user can still address another user's `workbook_id`. That is a
known, accepted residual — `D13`, and next task 2. Do not add one ad hoc to a single route;
it belongs with the owner-scoping work, applied consistently.

One thing to know about the resolved user: it is detached from any database session, so
reading an attribute that would need a further query raises `DetachedInstanceError` rather
than lazy-loading (`R22`). Read only the loaded columns, or re-attach the row to your own
request-scoped session.

### 4.2 Extend the Firestore rules

Edit [firestore.rules](../firestore.rules). No wiring change is needed:
[firebase.json](../firebase.json) already points at it, and `scripts/deploy.sh` L421 already
deploys it.

Validate on the emulator before you deploy. Interactively:

```bash
firebase emulators:start --only firestore
```

Or for a scripted run:

```bash
firebase emulators:exec --only firestore "<your assertion command>"
```

`firebase.json` declares no `emulators` block, so ports fall back to the CLI defaults — a
clash on a busy machine is possible (`D35`).

**Every rules change must satisfy three assertions**, and all three matter equally:

1. The owner **is admitted**.
2. A signed-in non-owner, non-collaborator **is denied**.
3. An unauthenticated caller **is denied**.

Assertion 1 is the one people skip, and skipping it is how a fail-closed rule set passes
review while denying every legitimate user.

**Two constraints to design within.**

- **Security rules are not filters.** A collection query is evaluated against its *potential*
  result set, so a query that could return a document violating the rules fails outright
  rather than returning the subset that complies. The current client subscribes to a single
  document and is therefore compatible; any future collection query must carry the same
  condition as the rule that governs it.
- **A collaborator may only write a fixed field list** (`firestore.rules` L35-46). Add a new
  collaborator-writable field to that list or the write is refused (`D34`).

Rules are reviewed by inspection rather than verified in CI (`D22`); adding machine-verified
tests is next task 13.

### 4.3 Retune the rate limits

Both thresholds and both switches are `Settings` fields consumed by
`backend/app/core/rate_limit.py`. **Change the configuration, not the module.** The module
parses both window expressions before installing anything, so a malformed threshold fails at
import rather than on the first request that would have been throttled (`D31`).

Three things to get right:

- **Validate the write threshold against real editing.** `PUT .../cells` fires on cell edits
  and autosave and is by far the highest-frequency endpoint. Exercise a genuine editing
  session before promoting a new value — a synthetic loop tells you the limit trips, not
  whether normal use stays under it.
- **The limiter counts every outcome, including 401s.** That is deliberate, for
  credential-stuffing defence, and it is another reason the write budget has to be generous.
- **Changes are read at middleware registration**, so they need a pod restart.

If you disable throttling, note that it is currently **silent**: `register_rate_limiting`
returns immediately and installs nothing (`backend/app/core/rate_limit.py` L234), with no
marker header and no per-request record, so a disabled control looks identical to an enforcing
one (residual 11, follow-up `F19`, next task 13).

### 4.4 Move the Content-Security-Policy from report-only to enforcing

Flip the `csp_report_only` setting. It is read at middleware registration, so a pod restart is
required. Keep Terraform's `csp_report_only` variable (`variables.tf` L272) equal to it, so the
application origin and the API enforce the same mode.

**Two things break a too-tight policy, and both fail quietly.** `connect-src` must admit the
API origin *and* the Firebase Authentication and Firestore endpoints — otherwise sign-in and
real-time synchronisation stop working with no server-side error to find. The canonical
directive set lives in `backend/app/core/security_headers.py` L17-34, and the exact values are
tabulated in [SECURITY.md](../SECURITY.md).

**Mind the asymmetry.** The toggle governs only the API middleware header. The `<meta>` policy
in `frontend/public/index.html` is static and always enforcing, so a policy that breaks the
compiled application has to be corrected by rebuilding and republishing the bundle — the
setting cannot defang it (`D9`).

### 4.5 Exercise a rollback

Settings fall into two propagation classes, and confusing them wastes a rollback window
(`D17`):

- **Read inside a per-request code path** → takes effect on the **next request**, no restart.
  This works because `get_settings()` is uncached. The authentication verifier and enforcement
  switches are in this class.
- **Read when middleware or the engine is constructed** → needs `kubectl rollout restart`. The
  origin allow-list, the throttling settings, the CSP mode and `db_sslmode` are in this class.

**Image rollback is not available.** Images are tagged `:latest` only and overwritten on each
deploy (`deploy.sh` L358-359), so there is no previous image to return to. Rollback is
therefore configuration toggles plus a tagged `git revert`, promoted through staging.

One ordering constraint that reverses on the way back: the uploads-bucket policy and the
object-access code must be reverted **together**. Reverting the code alone restores a call that
the bucket policy now rejects (`D4`).

And on the break-glass authentication toggle: **using it reopens the vulnerability in full.**
It defaults to the secure position, logs a warning per bypassed request and marks the response
so a bypass is auditable. If the problem you are solving is a client lockout, **revert the
client interceptor instead** — that restores the previous behaviour without reopening the API
(`D18`).

### 4.6 Add a security test

New tests go in `backend/tests/test_security.py` and use the fixtures in
`backend/tests/conftest.py`:

| Fixture | Use it for |
|---------|------------|
| `client` (L824) | A plain client with **no** identity override — the only correct choice for asserting 401 |
| `authenticated_client` (L859) | A client whose requests resolve to `fake_user` |
| `fake_user` (L841) | The identity `authenticated_client` resolves to |
| `db_session` (L734) | A session against the in-memory database |
| `settings` (L920) | The active settings object |
| `bearer_headers` (L695), `workbook_payload` (L639), `cell_payload` (L664), `collaborator_payload` (L681) | Building requests |
| `service_calls` (L888), `secret_manager_requests` (L906) | Asserting on what the code under test called |

**The one trap: the identity override is opt-in, not global.** The database-session override
is applied automatically, but `authenticated_client` is not — and with it installed *every*
request is admitted, so no 401 assertion can hold. A test asserting refusal must take
`client`. This split is deliberate (`D33`), and the pairing is meant to be legible in the test
module: refusal tests take `client`, admitted-path tests take `authenticated_client`.

Run your new tests with:

```bash
python -m pytest backend/tests/test_security.py -q
```

---

## 5. Suggested next tasks

Improvements found while building the current security controls that fell outside its scope.
Ordered security-first. Each carries its evidence so it can be picked up without rediscovery,
and a `Tracked as` column pointing at the [Security Decision Log](<./Security Decision Log.md>)
where one applies.

| # | Task | Evidence | Why it was out of scope | Tracked as |
|---|------|----------|-------------------------|------------|
| 1 | **Upgrade the backend runtime off Python 3.9.** Lead with this one: it is not stylistic but a **hard prerequisite**. Every dependency advisory currently reported has a fix version requiring Python 3.10 or newer, so on 3.9 **not one of them can be remediated** — no amount of dependency work closes anything until this lands. Turning the dependency-audit gate strict is an explicit exit criterion. | `infrastructure/docker/Dockerfile.backend` L2 `FROM python:3.9-slim`; Python 3.9 reached end of life on 31 October 2025; `google-auth` emits a `FutureWarning` on every import. | That Dockerfile is outside the allowed change set. | `F1`, `D19` |
| 2 | **Add owner scoping to the workbook, worksheet, cell and collaboration routes.** Any authenticated user can still address another user's `workbook_id`. | No handler in `backend/app/api/` filters by owner, and `Workbook.owner_id` (`backend/app/db/models.py` L24) is never consulted. | Needs the excluded domain service layer, and an in-handler guard would exceed the auth-dependency-only boundary. | `F2`, `D13` |
| 3 | **Upgrade off Node 14** (end of life 30 April 2023). | The four places listed in [§1.1](#11-runtimes): `infrastructure/docker/Dockerfile.frontend` L14, `.github/workflows/ci.yml` L15, `scripts/setup_dev_environment.sh` L11 and [README](../README.md) L26. The Cloud Functions runtime is *not* one of them any more — `infrastructure/terraform/variables.tf` L219-226 now requires `function_runtime` to be supplied explicitly and validates it, precisely so a decommissioned runtime cannot be inherited from the file. | Those four files are outside the allowed change set for a runtime change. | — |
| 4 | **Populate `ownerUid` and `collaboratorUids` when workbook documents are written**, so the Firestore rules admit legitimate browser clients instead of denying everyone. | `backend/app/services/real_time_sync.py` L11-14 writes to `workbooks/{workbook_id}` with only the dotted cell path and no ownership field. | `real_time_sync.py` is outside the allowed change set. | `F4`, `D12` |
| 5 | **Add an immutable UID column and key the identity bridge on it — and adopt migration tooling to do so.** Removes both the dependence on a mutable email claim and the ability of an unverified address to select a local account. | `backend/app/db/models.py` L10 (integer primary key, no `firebase_uid`); `scripts/deploy.sh` L411-412 unresolved migration-tool placeholder. | No migration tooling exists. | `F5`, `D2`, `R1` |
| 6 | **Add a collaborators table** so sharing can be persisted and authorized. | No collaborators model in `backend/app/db/models.py`, yet `POST /workbooks/{workbook_id}/share` exists (`backend/app/api/collaboration.py` L13); `backend/tests/test_collaboration.py` L41-59 asserts owner / shared-user / admin-override semantics against an `AccessControl` class that does not exist; and `backend/app/schema/workbook_schema.py` never defines the `CollaboratorSchema` that `collaboration.py` L5 imports. | Requires migration tooling. | `F6` |
| 7 | **Stop returning internal exception text to callers** (CWE-209). | `backend/app/api/cells.py` L23 puts `str(e)` in an HTTP 500 detail; `backend/app/api/collaboration.py` L32 puts `str(e)` in an HTTP 400 detail. | Handler bodies are business logic, outside the auth-dependency-only permission. | `F7` |
| 8 | **Add the missing `typing` imports to `backend/app/services/real_time_sync.py`.** | L10 annotates `Any` and L19 annotates `Callable`, and the module imports neither (L1-2) — the same defect class as the missing import that once blocked the security module entirely. | Not in the vulnerability list, and the file is outside the allowed change set. | — |
| 9 | **Repair `frontend/package.json` and commit a lock file.** **This blocks all frontend verification**, including the CI dependency audit. | The undeclared packages, the absent lock file and the unresolvable import paths, all detailed in [Common pitfalls](#the-frontend-cannot-install-build-or-type-check). Also settle the `react-router-dom` major version: `frontend/src/app.tsx` L2 uses the v5 API. | The manifest is explicitly excluded. | `F3`, `R18`, `D30` |
| 10 | **Repair `infrastructure/terraform/outputs.tf`** so `terraform validate` and `terraform fmt -check` pass. **This blocks machine validation of every infrastructure change.** | The seven undeclared references at L10, L11, L12, L24, L25, L26 and L32, listed in [Common pitfalls](#terraform-validate-does-not-pass-on-a-clean-checkout). | Outside the allowed change set. | — |
| 11 | **Adopt an image tagging strategy that permits rollback.** | Images are tagged `:latest` only and overwritten on each deploy (`scripts/deploy.sh` L358-359). | Tagging is a delivery-pipeline concern outside the change set; it is what forced rollback onto configuration toggles. | `D17` |
| 12 | **Reintroduce a tracked root ignore file** at `.gitignore`. Nothing tracked currently prevents a populated `.env` — holding `SECRET_KEY` and the database password — from being staged. | `git ls-files .gitignore` returns nothing: the repository root has no ignore file. The only exclusions in force are the per-clone rules in `.git/info/exclude`, which is not shared between clones and does not list `.env`. `.env.example` warns about this inline in its header (around L25-28). | The file is outside the authorized artifact map. | `F17`, `DEV-4` |
| 13 | **Harden the containers, and add image scanning, dependency automation and machine-verified rules tests.** Container work: a `.dockerignore` (`Dockerfile.backend` L14 `COPY . .` currently ships `.git`), a non-root `USER`, a `HEALTHCHECK` and base-image digest pinning. Pipeline work: no Trivy/Grype/Snyk/Clair step exists anywhere in `.github/`, there is no `dependabot.yml`, and the Firestore rules are reviewed by inspection only. | `infrastructure/docker/Dockerfile.backend` L14; absence of any scanning step in `.github/workflows/`; `D22` on rules validation. | Outside the allowed change set for the Dockerfiles, and rules tests would add a frontend test dependency. | `F13`, `F16`, `F19` |
| 14 | **Fix the CI test invocation and the CD trigger name — together.** | `.github/workflows/ci.yml` L36-39 runs `cd backend && npm test` against a directory with no `package.json`; `cd.yml` L4-5 waits on `Continuous Integration` while `ci.yml` L1 is `CI`. **The trap: renaming `ci.yml` alone would silently start firing continuous deployment.** Change both deliberately, in one change. | The workflow name mismatch was designated flag-only. | `D29` |
| 15 | **Replace the long-lived CI service-account key with Workload Identity Federation, and remove interactive auth from the deployment script.** | `.github/workflows/cd.yml` L34 uses `secrets.GCP_SA_KEY`; `scripts/deploy.sh` L63 calls `gcloud auth login` interactively. | `cd.yml` is outside the authorized change set, and federation is a pipeline redesign. | — |
| 16 | **Establish the backend package structure and the domain service layer.** Until this lands the API server cannot start at all. | Zero `__init__.py` files under `backend/`; `WorkbookService`, `WorksheetService`, `CellService` and `CollaborationService` are imported by the route modules (L6 of each) and do not exist; `backend/app/main.py` L8 imports an `init_db` that `backend/app/db/database.py` does not define; `Dockerfile.backend` L20 runs `uvicorn main:app`, the wrong module path. | Explicitly out of scope — these are functional gaps rather than vulnerabilities. | `D32` |
| 17 | **Repair `scripts/setup_dev_environment.sh`** to match this application, or delete it. | The Django commands at L54 and L60, the root manifest path at L22, and the three unresolved placeholders at L38-39, L44-45 and L49-50, detailed in [Common pitfalls](#the-setup-script-is-wrong-for-this-application). | Outside the allowed change set. | — |
| 18 | **Strengthen transport verification and reconsider revocation cost.** `sslmode=require` encrypts the channel but does not authenticate the database server; `verify-full` or the Cloud SQL Python Connector would. Separately, token verification checks revocation on every request, which costs a provider lookup per call. | `backend/app/db/database.py` L8-10; `backend/app/core/config.py` L68. | `verify-full` needs CA material distributed to the client and connection by DNS name; the Connector needs a new dependency and new connection code. | `F8`, `F9`, `D7`, `R4` |
| 19 | **Codify network segmentation and the encryption and audit controls.** | No VPC, subnet, firewall rule or Kubernetes `NetworkPolicy` exists in `infrastructure/terraform/`; no customer-managed encryption keys, key rotation, data masking or audit logging — despite `SECU-001-02` (Cloud KMS encryption) at L484 and `SECU-001-05` (audit logging) at L487 of [Software Requirements Specifications (SRS)](<./Software Requirements Specifications (SRS).md>), whose `SECU-001` block begins at L476. Cloud KMS at rest and TLS 1.3 in transit are also stated under `DATA SECURITY` in [Technical Specifications](<./Technical Specifications.md>) (L651-652). | Outside the allowed change set for Terraform. | `F16` |
| 20 | **Track the dependency advisories with no reachable fix, and migrate off Pydantic v1.** `ecdsa` has **no published fix on any runtime** and arrives transitively through the retained JWT library. The ASGI layer carries several advisories whose fixes all require Python 3.10 or newer and are therefore blocked by task 1. Pydantic v1 is a supported-version problem rather than a security one. Pinning also fixes versions but not file contents, so artifact hashes are still unpinned. | `backend/requirements.txt` L118 `ecdsa==0.19.2  # via python-jose`, L53 `python-jose[cryptography]==3.5.0` (retained, pinned above its advisory floor), L39 `starlette==0.49.3` (pinned transitively by FastAPI), L49 `pydantic==1.10.26`. For the full residual inventory read [SECURITY.md](../SECURITY.md). | The JWT library is retained by instruction; the ASGI pin follows FastAPI; the Pydantic migration is a framework change; the audit gate stays delta-only until task 1 lands. | `F11`, `F15`, `D14`, `D16`, `D19` |

### 5.1 Known defects that are **not** security issues

Recorded so that meeting one does not send you looking for a vulnerability. Both live in files
that were only ever opened for security changes, which is why they are still here.

- **A worksheet with populated cells cannot be serialised.**
  `backend/app/schema/workbook_schema.py` declares a worksheet's cells as a map keyed by cell
  reference (`WorksheetSchema`, L10), while the ORM stores a list of row/column rows
  (`backend/app/db/models.py` L46-52). `WorksheetSchema.from_orm` therefore fails validation
  on a non-empty worksheet. Choosing the key format is product design, and the projection
  belongs to the absent worksheet service. Residual 16 and next task 6.
- **The user a request resolves to is detached from its database session.** Reading an
  attribute that would need a further query raises `DetachedInstanceError` instead of
  lazy-loading. This is the deliberate cost of releasing the session deterministically
  (`R22`). Inert today, because no handler reads `current_user` at all — but see
  [§4.1](#41-add-a-protected-route) before you become the first.

Two defects that older notes and review threads describe as open have since been **closed**,
so do not go looking for them: the SQLAlchemy mapper configuration error is fixed by the
reverse relationship at `backend/app/db/models.py` L17 (`DEV-6`), and the leaked database
session in the identity lookup is fixed by the session ownership at
`backend/app/core/security.py` L450-468 (`DEV-5`).
