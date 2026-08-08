# Microsoft Excel Clone

A web-based clone of Microsoft Excel with essential spreadsheet functionalities.

## Features

- Create, edit, and save spreadsheets
- Basic cell formatting (bold, italic, underline, cell color)
- Formula support for basic arithmetic operations
- Data import/export (CSV)
- Responsive design for desktop and mobile use

## Technology Stack

- Frontend: React 18.2 with TypeScript 4.9.5, built by `react-scripts`. Redux Toolkit for
  state, Axios for HTTP, Chart.js with `react-chartjs-2` for charting, Tailwind CSS for
  styling, Formik with Yup for forms. Compiled to static files and published to a Google
  Cloud Storage bucket.
- Backend: Python 3.9 with FastAPI, served by `uvicorn` on port 8000, running on Google
  Kubernetes Engine. SQLAlchemy 1.4 as the ORM and Pydantic v1 for settings and schemas.
- Relational database: Cloud SQL for PostgreSQL 13.
- Real-time collaboration store: Firestore in native mode (the `(default)` database).
- File storage: Google Cloud Storage.
- Configuration and secrets: Google Cloud Secret Manager.
- Authentication: Firebase Authentication / Google Cloud Identity Platform for client
  sign-in, with server-side ID-token verification through the Firebase Admin SDK.
- Background jobs: Celery with Redis.
- Testing: `pytest` for the backend; Jest and React Testing Library for the frontend.

## Getting Started

### Prerequisites

- Node.js (v14 or later)
- npm (v6 or later)
- Python 3.9 (the documented backend runtime)
- Access to a Google Cloud project with the Secret Manager secrets the backend reads already
  provisioned. Every configuration key is listed in [`.env.example`](.env.example), and the
  provisioning steps are in the
  [Developer Onboarding guide](<documentation/Developer Onboarding.md>).

Needed for deployment and cloud-side verification, not for running the application locally:

- Google Cloud SDK (`gcloud`)
- Firebase CLI (also used to run the Firestore emulator)

### Installation

1. Clone the repository:
   ```
   git clone <repository-url>
   ```

2. Navigate to the project directory:
   ```
   cd <repository-directory>
   ```

3. Install dependencies:
   ```
   python3.9 -m venv venv
   source venv/bin/activate
   pip install -r backend/requirements.txt
   cd frontend && npm install && cd ..
   ```

4. Set up environment variables:
   Copy the template to a `.env` file in the root directory, then fill in the values. Every
   key is documented in [`.env.example`](.env.example).
   ```
   cp .env.example .env
   ```

5. Start the development servers, one per terminal:
   ```
   # Backend, from the repository root
   uvicorn --env-file .env backend.app.main:app --reload --host 0.0.0.0 --port 8000

   # Frontend, in a second terminal
   cd frontend && npm start
   ```

For the full clean-machine procedure, including the operator provisioning gates and the
known pitfalls, see the
[Developer Onboarding guide](<documentation/Developer Onboarding.md>).

## Usage

1. Open your web browser and navigate to `http://localhost:3000`
2. Sign in - authentication goes through Firebase
3. Create a new spreadsheet or open an existing one
4. Use the toolbar to format cells, enter formulas, or import/export data

The API is served separately on `http://localhost:8000`.

## Security

Authentication is enforced on every API route, HTTP security headers and per-client rate
limiting are applied to every response, uploaded files are reached through time-limited
signed URLs rather than public object URLs, and access to the real-time collaboration store
is governed by [`firestore.rules`](firestore.rules).

See [`SECURITY.md`](SECURITY.md) for the full security posture, the vulnerability reporting
process and the documented residual risks.

## API Documentation

The API documents itself. With the backend running, the interactive OpenAPI UI is served at
`http://localhost:8000/docs` and the ReDoc rendering at `http://localhost:8000/redoc`.

## Documentation

| Document | Purpose |
|---|---|
| [Developer Onboarding](<documentation/Developer Onboarding.md>) | Clean-machine setup, domain context, common pitfalls, how to extend, suggested next tasks |
| [`SECURITY.md`](SECURITY.md) | Security posture, controls, reporting process, residual risks |
| [`.env.example`](.env.example) | Every configuration key with a safe placeholder |
| [Security Decision Log](<documentation/Security Decision Log.md>) | Rationale for every non-trivial implementation decision |
| [Security Traceability Matrix](<documentation/Security Traceability Matrix.md>) | Bidirectional mapping of finding to artifact to verification |
| [Technical Specifications](<documentation/Technical Specifications.md>) | Architecture and system design |
| [Software Requirements Specification](<documentation/Software Requirements Specifications (SRS).md>) | Functional and non-functional requirements |

## Contributing

We welcome contributions to the Microsoft Excel Clone project. Start with the
[Developer Onboarding guide](<documentation/Developer Onboarding.md>), which covers the
setup, the domain context, the common pitfalls, how to extend the project and the current
list of suggested next tasks.

## License

This project is licensed under the MIT License.

## Acknowledgements

- [React](https://reactjs.org/)
- [TypeScript](https://www.typescriptlang.org/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [SQLAlchemy](https://www.sqlalchemy.org/)
- [Redux Toolkit](https://redux-toolkit.js.org/)
- [Tailwind CSS](https://tailwindcss.com/)
- [Chart.js](https://www.chartjs.org/)
- [Google Cloud Platform](https://cloud.google.com/)
