"""Tests for the API's authentication, authorization, transport and throttling controls.

``backend.app.main`` and the four route modules cannot be imported: they do
``from backend.app.db import get_db``, ``from backend.app.schema import ...`` and
``from backend.app.services import ...``, none of which resolves, because no package under
``backend/`` carries an ``__init__.py`` and the named service classes do not exist.

So the controls are verified three ways, and each is real verification rather than a
substitute for it:

* Behaviour is exercised against the middleware stack and the authentication dependency
  themselves, composed on a throwaway application in the order ``backend/app/main.py``
  registers them.
* The authentication seam is driven end to end: a verified token resolving a real database
  row through the production dependency and into a handler body, with the identity lookup
  pointed at an in-memory database by patching the ``get_db`` binding the lookup actually
  calls. See :class:`TestAuthenticatedIdentityResolution`, and
  :class:`TestOrmSeam` for the mapper and schema contracts it rests on.
* The shape of the files that cannot be imported - the authentication dependency on each
  handler, the unchanged route contracts, and the registration order in the entry point -
  is asserted by parsing their source, so a change to any of them fails a test. The same
  approach asserts the Terraform, Nginx, ``index.html`` and deployment files, and asserts
  them *against each other*, because the failure this suite exists to catch is the planes
  each reading correctly on their own while describing different things.

What is *not* claimed: nothing here observes a deployed resource or a live cloud response.
Where a control can only be confirmed by a deployment, the assertion is on the committed
configuration and says so.
"""

import ast
import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pydantic
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = REPOSITORY_ROOT / "backend" / "app"
TERRAFORM = REPOSITORY_ROOT / "infrastructure" / "terraform"

#: Route module, handler name, decorator method and path for each protected route.
ROUTE_CONTRACTS = [
    ("workbooks.py", "get_workbooks", "get", "/workbooks"),
    ("workbooks.py", "create_workbook", "post", "/workbooks"),
    ("worksheets.py", "get_worksheets", "get", "/workbooks/{workbook_id}/worksheets"),
    (
        "cells.py",
        "update_cells",
        "put",
        "/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells",
    ),
    ("collaboration.py", "share_workbook", "post", "/workbooks/{workbook_id}/share"),
]

#: The route modules, once each: two of the five contracts above live in the same module.
ROUTE_MODULES = sorted({contract[0] for contract in ROUTE_CONTRACTS})

#: The commit this change set is measured against. The reverse coverage claim in
#: ``documentation/Security Traceability Matrix.md`` is a statement about
#: ``git diff <this commit> --name-only``, so the guard that enforces the claim has to name the
#: same commit the document's own re-verification instruction names.
_TRACEABILITY_BASELINE_COMMIT = "45a6b7b"


#: Cardinal numbers as prose writes them, for the published counts in ``SECURITY.md``.
_NUMBER_WORDS = {
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
}
#: Throttling variables ``_build_application`` writes into the process environment, which
#: the uncached ``get_settings()`` re-reads on every call.
THROTTLING_ENVIRONMENT = (
    "rate_limit_default",
    "rate_limit_write",
    "rate_limit_enabled",
    "rate_limit_trusted_proxies",
)


@pytest.fixture(autouse=True)
def _isolate_throttling_environment():
    """Restore the throttling variables after every test in this module.

    ``_build_application`` sets them directly on ``os.environ`` because the middleware it
    composes reads them while the test's requests are in flight, so they cannot be scoped to
    the call. Without this they persist for the rest of the session and a later test reading
    a default gets whichever value the last composed application happened to use.
    """
    saved = {name: os.environ.get(name) for name in THROTTLING_ENVIRONMENT}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("no function named %r" % name)


def response_detail(response):
    """Return a JSON response's ``detail`` field, or None when it carries none."""
    body = response.json()
    return body.get("detail") if isinstance(body, dict) else None


def code_only(source):
    """Return ``source`` with whole-line ``#`` and ``//`` comments removed.

    Every assertion that a construct is ABSENT has to run against this rather than the raw
    text. The comment above a security change names the construct it replaced - that is the
    point of the comment - so a substring search over the raw file answers a question about
    the explanation instead of a question about the configuration. Stripping comment lines
    keeps the assertion pointed at the code, and keeps a future author free to explain a
    removal without the explanation itself breaking the test that proves the removal.

    Whole-line comments only: a trailing comment cannot introduce a construct name without
    also leaving real code on the line, and stripping to the first ``#`` would corrupt any
    literal containing one.
    """
    return "\n".join(
        line
        for line in source.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )


# ===========================================================================
# V2 - the security module imports at all
# ===========================================================================
class TestSecurityModuleImportability:
    """V2: the security module imports, so every control defined in it is reachable."""

    def test_security_module_imports(self):
        module = importlib.import_module("backend.app.core.security")
        assert module.get_current_user is not None

    def test_optional_is_resolvable_in_the_module_namespace(self):
        module = importlib.import_module("backend.app.core.security")
        assert module.Optional is not None

    @pytest.mark.parametrize(
        "module_name",
        [
            "backend.app.core.config",
            "backend.app.core.security",
            "backend.app.core.security_headers",
            "backend.app.core.rate_limit",
            "backend.app.db.database",
            "backend.app.db.models",
            "backend.app.services.file_storage",
        ],
    )
    def test_every_module_this_change_set_touches_imports(self, module_name):
        assert importlib.import_module(module_name) is not None


# ===========================================================================
# V1 - every route requires an authenticated caller
# ===========================================================================
class TestRouteAuthenticationDependency:
    """V1: every route handler declares ``get_current_user`` as a dependency."""

    @pytest.mark.parametrize("module_file, handler, method, path", ROUTE_CONTRACTS)
    def test_handler_depends_on_get_current_user(self, module_file, handler, method, path):
        tree = _parse(BACKEND_APP / "api" / module_file)
        function = _function(tree, handler)
        annotated = {
            argument.arg: argument
            for argument in function.args.args
        }
        assert "current_user" in annotated, (
            "%s has no current_user parameter" % handler
        )
        # The default must be Depends(get_current_user).
        offset = len(function.args.args) - len(function.args.defaults)
        default = function.args.defaults[
            list(annotated).index("current_user") - offset
        ]
        assert isinstance(default, ast.Call)
        assert getattr(default.func, "id", None) == "Depends"
        assert getattr(default.args[0], "id", None) == "get_current_user"
        assert getattr(annotated["current_user"].annotation, "id", None) == "User"

    @pytest.mark.parametrize("module_file, handler, method, path", ROUTE_CONTRACTS)
    def test_route_contract_is_unchanged(self, module_file, handler, method, path):
        tree = _parse(BACKEND_APP / "api" / module_file)
        function = _function(tree, handler)
        decorators = [
            decorator
            for decorator in function.decorator_list
            if isinstance(decorator, ast.Call)
        ]
        assert len(decorators) == 1
        decorator = decorators[0]
        assert decorator.func.attr == method
        assert decorator.args[0].value == path
        # The decorator carries no response_model and no status_code keyword.
        assert [keyword.arg for keyword in decorator.keywords] == []

    @pytest.mark.parametrize("module_file", ROUTE_MODULES)
    def test_the_dependency_is_imported_from_the_security_module(self, module_file):
        """The name in the signature has to resolve to the dependency, not merely be spelled
        like it.

        These modules cannot be imported, so the dependency is asserted from their source -
        which means the import is part of the assertion rather than an assumption. Rebinding
        ``get_current_user`` to something that is not the dependency leaves every signature
        reading correctly while no route is protected.
        """
        tree = _parse(BACKEND_APP / "api" / module_file)
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported[alias.asname or alias.name] = node.module
        assert imported.get("get_current_user") == "backend.app.core.security", imported
        assert imported.get("User") == "backend.app.db.models", imported
        # No later statement may rebind either name to something else.
        source = code_only((BACKEND_APP / "api" / module_file).read_text("utf-8"))
        for name in ("get_current_user", "User"):
            assert not re.search(r"(?m)^%s\s*=" % name, source), name

    @pytest.mark.parametrize("module_file, handler, method, path", ROUTE_CONTRACTS)
    def test_current_user_is_the_last_parameter(self, module_file, handler, method, path):
        """``current_user`` is the last parameter, so no other parameter's position
        depends on it."""
        tree = _parse(BACKEND_APP / "api" / module_file)
        function = _function(tree, handler)
        assert function.args.args[-1].arg == "current_user"

    def test_no_token_route_was_added(self):
        tree = _parse(BACKEND_APP / "main.py")
        source = code_only((BACKEND_APP / "main.py").read_text(encoding="utf-8"))
        assert "/token" not in source
        routers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "include_router"
        ]
        assert len(routers) == 4


class TestUnauthenticatedRequestsAreRefused:
    """V1 behaviour, exercised against the real dependency on a probe application."""

    @pytest.fixture
    def probe_client(self):
        from backend.app.core.security import get_current_user

        app = FastAPI()

        @app.get("/protected")
        def protected(current_user=Depends(get_current_user)):
            return {"reached": True}

        return TestClient(app, raise_server_exceptions=False)

    def test_request_without_a_credential_is_refused(self, probe_client):
        response = probe_client.get("/protected")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "reached" not in response.text

    def test_refusal_names_no_cause(self, probe_client):
        """The refusal comes from the bearer scheme, which names no cause either."""
        assert response_detail(probe_client.get("/protected")) == "Not authenticated"

    def test_an_empty_credential_is_recorded_server_side(self, captured_logs):
        """A present-but-empty credential is the one the application itself refuses, and it
        is the case worth a record: the scheme refuses an absent header before this module
        runs."""
        security = importlib.import_module("backend.app.core.security")
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            security._resolve_current_user("", {security._BYPASS_STATE_KEY: False})
        messages = [record.getMessage() for record in handler.records]
        assert any("bearer credential was empty" in message for message in messages), (
            messages
        )

    def test_extraction_refuses_before_the_dependency_body_runs(self, probe_client):
        """The OAuth2 scheme answers the challenge itself, so no verification is attempted."""
        from backend.app.core.security import oauth2_scheme

        assert oauth2_scheme.auto_error is True
        response = probe_client.get("/protected")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_malformed_authorization_header_is_refused(self, probe_client):
        response = probe_client.get(
            "/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "reached" not in response.text

    @pytest.mark.parametrize(
        "fault_name",
        [
            "InvalidIdTokenError",
            "ExpiredIdTokenError",
            "RevokedIdTokenError",
            "UserDisabledError",
        ],
    )
    def test_a_presented_bearer_token_that_fails_verification_never_reaches_the_handler(
        self, monkeypatch, probe_client, fault_name
    ):
        """A well-formed ``Authorization: Bearer`` value the provider refuses, driven through
        a request rather than against the verification helper alone: the handler body is not
        entered, the response is the frozen 401 with its challenge, and it names no cause.

        Verification is stubbed at :func:`_firebase_app` as well as at ``verify_id_token``,
        because reaching the Admin SDK would initialise a real application.
        """
        from firebase_admin import auth as firebase_auth

        security = importlib.import_module("backend.app.core.security")
        fault = {
            "InvalidIdTokenError": lambda: firebase_auth.InvalidIdTokenError("forged"),
            "ExpiredIdTokenError": lambda: firebase_auth.ExpiredIdTokenError(
                "expired", cause=None
            ),
            "RevokedIdTokenError": lambda: firebase_auth.RevokedIdTokenError("revoked"),
            "UserDisabledError": lambda: firebase_auth.UserDisabledError("disabled"),
        }[fault_name]()

        monkeypatch.setattr(
            security, "_firebase_app", lambda project_id: "verifying-app"
        )

        def _verify(token, app=None, check_revoked=False):
            raise fault

        monkeypatch.setattr(firebase_auth, "verify_id_token", _verify)

        response = probe_client.get(
            "/protected",
            headers={"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.forged.signature"},
        )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "reached" not in response.text
        assert response_detail(response) == "Could not validate credentials"
        for leak in ("forged", "expired", "revoked", "disabled", fault_name, "Traceback"):
            assert leak not in response.text, leak


# ===========================================================================
# V3 / M9 / M10 - what the dependency accepts and how it refuses
# ===========================================================================
class TestTokenVerificationOutcomes:
    """V3, M9 and M10: verification failures are separated by whose fault they are."""

    @pytest.fixture
    def security(self):
        return importlib.import_module("backend.app.core.security")

    @pytest.fixture
    def verified_claims(self, security, monkeypatch):
        """Return a callable that runs ``_verified_claims`` with verification stubbed."""
        from firebase_admin import auth as firebase_auth

        from backend.app.core.config import get_settings

        credentials_exception = HTTPException(
            status_code=401,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

        def _run(raised=None, claims=None, raised_at_initialisation=None):
            if raised_at_initialisation is not None:
                def _failing_app(project_id):
                    raise raised_at_initialisation

                monkeypatch.setattr(security, "_firebase_app", _failing_app)
            else:
                monkeypatch.setattr(
                    security, "_firebase_app", lambda project_id: "verifying-app"
                )

                def _verify(token, app=None, check_revoked=False):
                    assert check_revoked is True, "revocation must be checked"
                    if raised is not None:
                        raise raised
                    return claims

                monkeypatch.setattr(firebase_auth, "verify_id_token", _verify)
            return security._verified_claims(
                "a.token.value",
                "firebase",
                get_settings(),
                credentials_exception,
            )

        return _run

    def _caller_faults(self):
        from firebase_admin import auth as firebase_auth

        return [
            firebase_auth.InvalidIdTokenError("forged"),
            firebase_auth.ExpiredIdTokenError("expired", cause=None),
            firebase_auth.RevokedIdTokenError("revoked"),
            firebase_auth.UserDisabledError("disabled"),
            # F-C's sibling finding: the revocation check reads the user record, so a token
            # for a deleted account raises this. It reaches FirebaseError through
            # NotFoundError, so before it was listed here it fell to the provider clause and
            # was recorded at ERROR with a traceback that printed the account's Firebase UID.
            firebase_auth.UserNotFoundError(
                "No user record found for the provided user ID: cukj7KJMeMYFyw4NZLiAkQHUXFwL"
            ),
            ValueError("token is not a string"),
        ]

    def _provider_faults(self):
        from firebase_admin import auth as firebase_auth
        from firebase_admin import exceptions as firebase_exceptions
        from google.auth import exceptions as google_auth_exceptions

        return [
            firebase_auth.CertificateFetchError("keys unreachable", cause=None),
            firebase_exceptions.UnavailableError("provider down", cause=None),
            google_auth_exceptions.DefaultCredentialsError("no ADC"),
        ]

    def test_a_verified_token_yields_its_claims(self, verified_claims):
        assert verified_claims(claims={"email": "user@example.com"}) == {
            "email": "user@example.com"
        }

    @pytest.mark.parametrize("index", range(6))
    def test_caller_credential_faults_are_401(self, verified_claims, index):
        fault = self._caller_faults()[index]
        with pytest.raises(HTTPException) as raised:
            verified_claims(raised=fault)
        assert raised.value.status_code == 401
        assert raised.value.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("index", range(3))
    def test_provider_faults_are_401(self, verified_claims, index):
        """A token that could not be checked is refused, not answered as a server fault."""
        fault = self._provider_faults()[index]
        with pytest.raises(HTTPException) as raised:
            verified_claims(raised=fault)
        assert raised.value.status_code == 401
        assert raised.value.headers["WWW-Authenticate"] == "Bearer"
        assert "Retry-After" not in raised.value.headers

    def test_an_unconfigured_project_is_401(self, verified_claims):
        with pytest.raises(HTTPException) as raised:
            verified_claims(raised_at_initialisation=ValueError("no project"))
        assert raised.value.status_code == 401

    def test_unresolvable_credentials_are_401(self, verified_claims):
        from google.auth import exceptions as google_auth_exceptions

        with pytest.raises(HTTPException) as raised:
            verified_claims(
                raised_at_initialisation=google_auth_exceptions.DefaultCredentialsError(
                    "no ADC"
                )
            )
        assert raised.value.status_code == 401

    def test_the_module_declares_no_provider_outage_response(self, security):
        """The 503 shape the frozen response contract does not carry is absent."""
        source = BACKEND_APP / "core" / "security.py"
        text = source.read_text(encoding="utf-8")
        assert "503" not in text, source
        assert "Retry-After" not in text, source
        assert not hasattr(security, "AUTH_PROVIDER_UNAVAILABLE_DETAIL")

    @pytest.mark.parametrize("index", range(3))
    def test_a_provider_fault_is_separated_by_log_level(
        self, verified_claims, captured_logs, index
    ):
        """Status alone cannot distinguish the two faults, so the log level must."""
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            verified_claims(raised=self._provider_faults()[index])
        assert [r for r in handler.records if r.levelno >= logging.ERROR]

    @pytest.mark.parametrize("index", range(6))
    def test_a_caller_fault_is_recorded_at_warning_level_only(
        self, verified_claims, captured_logs, index
    ):
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            verified_claims(raised=self._caller_faults()[index])
        assert handler.records
        assert not [r for r in handler.records if r.levelno >= logging.ERROR]

    # --- F-A: a deleted account is a stale credential, not a provider fault -------------

    def test_a_deleted_account_is_a_caller_fault_not_a_provider_fault(
        self, verified_claims, captured_logs
    ):
        """The account named by an otherwise valid token has been removed.

        ``UserNotFoundError`` reaches ``FirebaseError`` through ``NotFoundError``, so listing
        it is the only thing that keeps it out of the provider clause - and the provider clause
        logs at ERROR with ``exc_info=True``, which made a routine rejection alertable and
        printed the account's Firebase UID into the log.
        """
        from firebase_admin import auth as firebase_auth

        handler = captured_logs("backend.app.core.security")
        deleted = firebase_auth.UserNotFoundError(
            "No user record found for the provided user ID: cukj7KJMeMYFyw4NZLiAkQHUXFwL"
        )
        with pytest.raises(HTTPException) as raised:
            verified_claims(raised=deleted)

        assert raised.value.status_code == 401
        assert raised.value.headers["WWW-Authenticate"] == "Bearer"
        assert handler.records
        assert [r for r in handler.records if r.levelno == logging.WARNING]
        assert not [r for r in handler.records if r.levelno >= logging.ERROR]
        for record in handler.records:
            assert record.exc_info is None, record.getMessage()

    def test_a_deleted_account_does_not_reach_the_log(
        self, verified_claims, captured_logs
    ):
        """The traceback carried the provider's message, and that message names the UID."""
        from firebase_admin import auth as firebase_auth

        handler = captured_logs("backend.app.core.security")
        uid = "cukj7KJMeMYFyw4NZLiAkQHUXFwL"
        with pytest.raises(HTTPException):
            verified_claims(
                raised=firebase_auth.UserNotFoundError(
                    "No user record found for the provided user ID: %s" % uid
                )
            )
        messages = [record.getMessage() for record in handler.records]
        assert messages
        for message in messages:
            assert uid not in message, message
            assert "No user record found" not in message, message
        # Only the exception type is named, which is what the module's contract promises.
        assert any("UserNotFoundError" in message for message in messages), messages

    def test_the_caller_fault_set_is_ordered_before_the_provider_clause(self, security):
        """Both clauses can match the same exception, so their ORDER is the classification.

        Asserted on the source because the ordering is not observable from the outside once it
        is wrong in the other direction - the request is still refused with 401 either way, and
        only the log level differs.
        """
        source = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        caller_clause = source.index("firebase_auth.UserNotFoundError")
        provider_clause = source.index("firebase_exceptions.FirebaseError,\n        google_auth_exceptions.GoogleAuthError,\n    ):\n        # SECURITY: a provider failure")
        assert caller_clause < provider_clause

    def test_a_provider_fault_is_logged_with_its_exception_context(
        self, verified_claims, captured_logs
    ):
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            verified_claims(raised=self._provider_faults()[1])
        errors = [r for r in handler.records if r.levelno >= logging.ERROR]
        assert errors, [r.getMessage() for r in handler.records]
        assert errors[0].exc_info is not None

    def test_a_caller_fault_logs_no_token_value(self, verified_claims, captured_logs):
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            verified_claims(raised=self._caller_faults()[0])
        messages = [record.getMessage() for record in handler.records]
        assert messages
        assert all("a.token.value" not in message for message in messages), messages

    def test_the_legacy_verifier_still_refuses_a_bad_local_token(self, security):
        from backend.app.core.config import get_settings

        credentials_exception = HTTPException(status_code=401, detail="no")
        with pytest.raises(HTTPException) as raised:
            security._verified_claims(
                "not-a-jwt",
                "legacy_jwt",
                get_settings(),
                credentials_exception,
            )
        assert raised.value.status_code == 401

    def test_the_legacy_verifier_refuses_a_token_that_never_expires(self, security):
        """python-jose validates ``exp`` when it is present and asks for nothing when it is
        absent, so a locally-issued credential carrying no expiry was admitted for ever - the
        opposite of the lifetime ``ACCESS_TOKEN_EXPIRE_MINUTES`` exists to govern."""
        from jose import jwt

        from backend.app.core.config import get_settings

        settings = get_settings()
        never_expires = jwt.encode(
            {"sub": "1"}, settings.SECRET_KEY, algorithm=settings.ALGORITHM
        )
        assert "exp" not in jwt.get_unverified_claims(never_expires)
        credentials_exception = HTTPException(
            status_code=401,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
        with pytest.raises(HTTPException) as raised:
            security._verified_claims(
                never_expires, "legacy_jwt", settings, credentials_exception
            )
        assert raised.value.status_code == 401
        assert raised.value.headers["WWW-Authenticate"] == "Bearer"

    def test_the_legacy_verifier_still_admits_a_token_that_does_expire(self, security):
        """The requirement is that an expiry EXISTS, not that it is shorter. Nothing the
        application issues is affected: ``create_access_token`` always sets ``exp``."""
        from backend.app.core.config import get_settings
        from backend.app.core.security import create_access_token

        settings = get_settings()
        claims = security._verified_claims(
            create_access_token({"sub": "1"}),
            "legacy_jwt",
            settings,
            HTTPException(status_code=401, detail="no"),
        )
        assert claims["sub"] == "1"
        assert "exp" in claims

    def test_the_expiry_requirement_is_a_decode_option_not_a_later_check(self, security):
        """Enforced where the signature is checked, so no other caller of the decode can
        inherit the permissive form."""
        source = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        assert '"require_exp": True' in code_only(source)


class TestFirebaseIssuerPinning:
    """V3: verification is pinned to one configured Firebase project.

    An unconstrained issuer accepts a token minted by any Firebase project, so a caller could
    present a valid token from a project they own and be resolved against this application's
    users. Every other test in this module replaces the app resolver, so these drive it
    directly. None of them reaches Google: the guard refuses before any client is built, and
    the two lookup paths are exercised against a stubbed Admin SDK, which is what keeps the
    process free of an initialised application.
    """

    @pytest.fixture
    def security(self):
        return importlib.import_module("backend.app.core.security")

    def test_an_unconfigured_project_is_refused_before_any_app_is_built(self, security):
        """``PROJECT_ID`` is required and ``firebase_project_id`` may override it, so an empty
        value reaching here means neither was set."""
        with pytest.raises(ValueError) as raised:
            security._firebase_app("")
        assert "pinned to one project" in str(raised.value)

    def test_the_app_name_carries_the_project(self, security):
        """Apps are keyed by project, so a changed ``firebase_project_id`` cannot be served by
        the app that was initialised for the previous one."""
        assert security._firebase_app_name("project-a") == "%s:project-a" % (
            security.FIREBASE_APP_NAME_PREFIX
        )
        assert security._firebase_app_name("project-b") != security._firebase_app_name(
            "project-a"
        )

    def test_the_ambient_suffix_names_the_app_for_an_unnamed_project(self, security):
        """The suffix exists so the name is always well formed; :func:`_firebase_app` refuses
        an empty project before this is reached."""
        assert security._firebase_app_name("") == "%s:%s" % (
            security.FIREBASE_APP_NAME_PREFIX,
            security.FIREBASE_AMBIENT_PROJECT_APP_SUFFIX,
        )

    def test_an_initialised_app_is_reused_rather_than_rebuilt(self, security, monkeypatch):
        """A per-request initialisation would build one Admin SDK app per request."""
        import firebase_admin

        looked_up = []

        def _get_app(name):
            looked_up.append(name)
            return "existing-app"

        def _initialize_app(*args, **kwargs):  # pragma: no cover - reaching this is the bug
            raise AssertionError("an already-initialised app was re-initialised")

        monkeypatch.setattr(firebase_admin, "get_app", _get_app)
        monkeypatch.setattr(firebase_admin, "initialize_app", _initialize_app)

        assert security._firebase_app("project-a") == "existing-app"
        assert looked_up == [security._firebase_app_name("project-a")]

    def test_a_first_call_initialises_the_app_for_that_project_alone(
        self, security, monkeypatch
    ):
        """The project is passed as an explicit option rather than left to the ambient
        environment, and Application Default Credentials are used, so no key file or
        credential path is named in code."""
        import firebase_admin
        from firebase_admin import credentials

        built = {}

        def _get_app(name):
            raise ValueError("no app named %s" % name)

        def _initialize_app(credential, options=None, name=None):
            built["credential"] = credential
            built["options"] = options
            built["name"] = name
            return "new-app"

        monkeypatch.setattr(firebase_admin, "get_app", _get_app)
        monkeypatch.setattr(firebase_admin, "initialize_app", _initialize_app)

        assert security._firebase_app("project-a") == "new-app"
        assert built["options"] == {"projectId": "project-a"}
        assert built["name"] == security._firebase_app_name("project-a")
        assert isinstance(built["credential"], credentials.ApplicationDefault)

    def test_no_credential_file_is_named_in_the_module(self, security):
        """Application Default Credentials are resolved from the runtime, so a path to a
        service-account key has no place in this module."""
        source = code_only(
            (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        )
        for construct in (
            "credentials.Certificate",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "service-account",
            ".json",
        ):
            assert construct not in source, construct

    def test_the_resolver_leaves_no_application_registered(self, security):
        """The suite must not initialise a real Admin SDK app: one would outlive the test that
        built it and be reused by every later call."""
        import firebase_admin

        with pytest.raises(ValueError):
            security._firebase_app("")
        assert list(firebase_admin._apps) == []


class TestAuthenticatedIdentityResolution:
    """M6: the seam, end to end - a verified token resolving a real row into a handler.

    Everything else in this module drives one side of the seam: token verification with the
    lookup absent, or the lookup with verification absent. Nothing joined them, so the suite
    could pass while the path a real request takes was broken - which it was, because the mapper
    could not configure.

    These tests drive the production dependency itself. They patch the module-level ``get_db``
    that ``_resolve_current_user`` calls, because that is the binding the lookup actually uses:
    ``app.dependency_overrides[get_db]`` is accepted by FastAPI and then never consulted on this
    path, so a fixture built that way reads as working while the lookup still talks to the
    production engine.
    """

    @pytest.fixture
    def security(self):
        return importlib.import_module("backend.app.core.security")

    @pytest.fixture
    def verified_token(self, security, monkeypatch):
        """Return a callable that makes verification succeed with the given claims."""
        from firebase_admin import auth as firebase_auth

        def _accept(**claims):
            # A verified Firebase token carries email_verified true; the identity path
            # requires it, so it is the default here and a test overrides it explicitly.
            claims.setdefault("email_verified", True)
            monkeypatch.setattr(
                security, "_firebase_app", lambda project_id: "verifying-app"
            )

            def _verify(token, app=None, check_revoked=False):
                assert check_revoked is True, "revocation must be checked"
                return claims

            monkeypatch.setattr(firebase_auth, "verify_id_token", _verify)

        return _accept

    @staticmethod
    def _user(**overrides):
        from backend.app.db.models import User

        fields = {
            "id": 1,
            "email": "owner@example.com",
            "name": "Owner",
            "created_at": datetime(2024, 1, 1),
        }
        fields.update(overrides)
        return User(**fields)

    def test_a_verified_token_resolves_the_seeded_user(
        self, security, verified_token, authentication_database
    ):
        authentication_database(self._user())
        verified_token(email="owner@example.com")

        state = {security._BYPASS_STATE_KEY: False}
        caller = security._resolve_current_user("a.token.value", state)

        assert caller.id == 1
        assert caller.email == "owner@example.com"
        assert caller.name == "Owner"
        assert state[security._BYPASS_STATE_KEY] is False

    def test_the_resolved_user_reaches_the_handler_through_the_real_dependency(
        self, security, verified_token, authentication_database
    ):
        """The whole path: bearer header, verification, lookup, handler body."""
        authentication_database(self._user(id=7, email="editor@example.com", name="Editor"))
        verified_token(email="editor@example.com")

        app = FastAPI()

        @app.get("/protected")
        def protected(current_user=Depends(security.get_current_user)):
            return {"id": current_user.id, "email": current_user.email}

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            "/protected", headers={"Authorization": "Bearer a.token.value"}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"id": 7, "email": "editor@example.com"}

    def test_the_resolved_user_is_readable_after_its_session_closed(
        self, security, verified_token, authentication_database
    ):
        """The lookup closes its Session before returning, so the row must be detached.

        Reading a loaded column on a detached instance is what the handler does. It works only
        because the lookup expunges the row while its values are still loaded; without that the
        first attribute read on the event loop would raise ``DetachedInstanceError``.
        """
        from sqlalchemy import inspect as sqlalchemy_inspect

        authentication_database(self._user())
        verified_token(email="owner@example.com")

        caller = security._resolve_current_user(
            "a.token.value", {security._BYPASS_STATE_KEY: False}
        )
        assert sqlalchemy_inspect(caller).detached is True
        assert caller.email == "owner@example.com"

    def test_a_verified_token_naming_no_local_user_is_refused(
        self, security, verified_token, authentication_database, captured_logs
    ):
        """A valid Firebase identity with no row is a 401, not a 500 and not an admission."""
        authentication_database(self._user())
        verified_token(email="stranger@example.com")
        handler = captured_logs("backend.app.core.security")

        with pytest.raises(HTTPException) as raised:
            security._resolve_current_user(
                "a.token.value", {security._BYPASS_STATE_KEY: False}
            )
        assert raised.value.status_code == 401
        assert raised.value.headers["WWW-Authenticate"] == "Bearer"

        messages = [record.getMessage() for record in handler.records]
        assert any("matches no local user" in message for message in messages), messages
        assert all("stranger@example.com" not in message for message in messages), messages

    def test_an_unexpected_database_error_still_releases_the_session(
        self, security, verified_token, failing_authentication_database
    ):
        """The ``finally`` closes the generator on the error path as well as the other two.

        Without it a failing query would leak a pooled connection per request, which is the
        failure mode that turns one database fault into an exhausted pool.
        """
        from sqlalchemy.exc import OperationalError

        verified_token(email="owner@example.com")

        with pytest.raises(OperationalError):
            security._resolve_current_user(
                "a.token.value", {security._BYPASS_STATE_KEY: False}
            )
        assert failing_authentication_database == [True], "the Session was not closed"

    def test_the_lookup_uses_the_module_level_binding(self, security):
        """Why the fixture patches the module rather than installing a dependency override.

        If the lookup ever moved to a FastAPI dependency this assertion would fail, which is the
        signal to change the fixture - rather than leaving an override in place that FastAPI
        accepts and never consults, reading as verification while verifying nothing.
        """
        source = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        resolve = _function(_parse(BACKEND_APP / "core" / "security.py"), "_resolve_current_user")
        calls = [
            node.func.id
            for node in ast.walk(resolve)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert "get_db" in calls, calls
        assert "from backend.app.db.database import get_db" in source
        assert "Depends(get_db)" not in source


class TestIdentityClaimValidation:
    """V3: a claim the identity column cannot hold never reaches the query."""

    @pytest.fixture
    def resolve(self):
        security = importlib.import_module("backend.app.core.security")
        exception = HTTPException(status_code=401, detail="no")

        def _resolve(claims, verifier="firebase"):
            return security._resolve_identity(claims, verifier, exception)

        return _resolve

    def test_a_verified_string_email_selects_the_email_column(self, resolve):
        from backend.app.db.models import User

        column, value = resolve(
            {"email": "user@example.com", "email_verified": True}
        )
        assert column is User.email
        assert value == "user@example.com"

    @pytest.mark.parametrize(
        "claims",
        [
            {},
            {"email": None, "email_verified": True},
            {"email": "", "email_verified": True},
            {"email": 7, "email_verified": True},
            {"email": "a\x00b", "email_verified": True},
        ],
    )
    def test_an_unusable_email_claim_is_refused(self, resolve, claims):
        with pytest.raises(HTTPException) as raised:
            resolve(claims)
        assert raised.value.status_code == 401

    @pytest.mark.parametrize(
        "verified",
        [None, False, "true", 1, "", 0],
    )
    def test_an_unverified_email_claim_is_refused(self, resolve, verified):
        """C3: signature verification proves the issuing project, not address ownership.

        Anyone able to self-register with the project could otherwise sign up using a known
        local user's address and be handed that user's row. Only the boolean ``True`` is
        accepted, so a truthy string or a 1 does not stand in for it.
        """
        claims = {"email": "victim@example.com"}
        if verified is not None:
            claims["email_verified"] = verified
        with pytest.raises(HTTPException) as raised:
            resolve(claims)
        assert raised.value.status_code == 401

    def test_the_unverified_refusal_is_recorded_without_the_address(
        self, resolve, captured_logs
    ):
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            resolve({"email": "victim@example.com", "email_verified": False})
        messages = [record.getMessage() for record in handler.records]
        assert any("not verified" in message for message in messages), messages
        assert all("victim@example.com" not in message for message in messages)

    def test_the_legacy_verifier_needs_no_email_verified_claim(self, resolve):
        """The legacy path resolves ``sub`` against the integer primary key, so the email
        claim and its verification state play no part in it."""
        from backend.app.db.models import User

        column, value = resolve({"sub": "7"}, verifier="legacy_jwt")
        assert column is User.id
        assert value == 7

    def test_a_numeric_sub_selects_the_id_column_for_the_legacy_verifier(self, resolve):
        from backend.app.db.models import User

        column, value = resolve({"sub": "42"}, verifier="legacy_jwt")
        assert column is User.id
        assert value == 42

    @pytest.mark.parametrize("claims", [{}, {"sub": None}, {"sub": True}, {"sub": "abc"}])
    def test_an_unusable_sub_claim_is_refused(self, resolve, claims):
        with pytest.raises(HTTPException) as raised:
            resolve(claims, verifier="legacy_jwt")
        assert raised.value.status_code == 401

    @pytest.mark.parametrize(
        "claim",
        [
            1.9,  # int() truncated this to user 1
            2.0,  # a float is not the integer the column holds, however round
            " 1 ",  # int() discarded the whitespace and resolved user 1
            "+1",  # int() discarded the sign and resolved user 1
            "01",  # int() ignored the leading zero and resolved user 1
            "-0",  # int() resolved user 0
            "1_0",  # int() read the underscore as a separator and resolved user 10
            "1e2",  # not an integer literal at all
            [1],  # not a scalar
        ],
    )
    def test_a_non_canonical_sub_claim_is_refused(self, resolve, claim):
        """N1: ``int()`` converted several non-identifiers into a *different* user's id."""
        with pytest.raises(HTTPException) as raised:
            resolve({"sub": claim}, verifier="legacy_jwt")
        assert raised.value.status_code == 401

    def test_a_refused_sub_claim_is_recorded_without_its_value(
        self, resolve, captured_logs
    ):
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            resolve({"sub": 1.9}, verifier="legacy_jwt")
        messages = [record.getMessage() for record in handler.records]
        assert any("not a user identifier" in message for message in messages), messages
        assert all("1.9" not in message for message in messages), messages


class TestAuthenticationEnforcementSwitch:
    """M10: the break-glass switch relaxes verification and never admits no credential."""

    @pytest.fixture
    def security(self):
        return importlib.import_module("backend.app.core.security")

    def test_bearer_extraction_refuses_a_request_carrying_no_credential(self, security):
        assert security.oauth2_scheme.auto_error is True

    def test_the_public_contract_is_unchanged(self, security):
        """The frozen signature is ``(token: str = Depends(oauth2_scheme)) -> User``.

        H8: the parameter had been retyped ``Optional[str]``, which is what allowed a
        credential-less request to reach the dependency at all. The annotation and the
        default are asserted, not only the parameter name.
        """
        import inspect

        from backend.app.db.models import User

        signature = inspect.signature(security.get_current_user)
        assert list(signature.parameters) == ["token"]
        token = signature.parameters["token"]
        assert token.annotation is str
        assert token.default.dependency is security.oauth2_scheme
        assert signature.return_annotation is User

    @pytest.mark.parametrize("enforcement", ["true", "false"])
    def test_an_empty_credential_is_refused_whatever_the_switch_says(
        self, security, set_settings, enforcement
    ):
        """H8: no configuration value may admit a request carrying no credential."""
        set_settings(auth_enforcement_enabled=enforcement)
        state = {security._BYPASS_STATE_KEY: False}
        with pytest.raises(HTTPException) as raised:
            security._resolve_current_user("", state)
        assert raised.value.status_code == 401
        assert raised.value.headers["WWW-Authenticate"] == "Bearer"
        assert state[security._BYPASS_STATE_KEY] is False

    def test_the_module_offers_no_anonymous_caller(self, security):
        """H8: the placeholder ``User`` the switch used to admit is gone, so no route
        handler can be handed a caller that belongs to no row."""
        assert not hasattr(security, "_anonymous_caller")

    def test_a_credential_less_request_is_refused_through_the_dependency(
        self, security, set_settings
    ):
        """The same guarantee end to end: the 401 arrives with its challenge and the
        handler is never reached, with the switch off."""
        set_settings(auth_enforcement_enabled="false")
        app = FastAPI()

        @app.get("/protected")
        def protected(current_user=Depends(security.get_current_user)):
            return {"reached": True}

        response = TestClient(app, raise_server_exceptions=False).get("/protected")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "reached" not in response.text

    @pytest.mark.parametrize("enforcement", ["true", "false"])
    def test_a_missing_credential_is_refused_whatever_the_switch_says(
        self, security, set_settings, enforcement
    ):
        """Extraction refuses first, so the switch cannot open a no-credential path."""
        set_settings(auth_enforcement_enabled=enforcement)
        app = FastAPI()

        @app.get("/protected")
        def protected(current_user=Depends(security.get_current_user)):
            return {"reached": True}

        response = TestClient(app, raise_server_exceptions=False).get("/protected")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "reached" not in response.text

    def test_no_placeholder_caller_can_be_admitted(self, security):
        """The transient no-identity ``User`` the disabled switch used to admit is gone."""
        assert not hasattr(security, "_anonymous_caller")
        source = BACKEND_APP / "core" / "security.py"
        assert "_anonymous_caller" not in source.read_text(encoding="utf-8")

    @staticmethod
    def _readable_token(**claims):
        """Return a signed token whose claims are readable and whose signature is worthless.

        Signed with a key unrelated to anything this application trusts, so a request it
        admits was admitted on unverified claims and on nothing else.
        """
        from jose import jwt

        return jwt.encode(claims, "a-key-nothing-here-trusts", algorithm="HS256")

    @staticmethod
    def _user(**overrides):
        from backend.app.db.models import User

        fields = {
            "id": 11,
            "email": "owner@example.com",
            "name": "Owner",
            "created_at": datetime(2024, 1, 1),
        }
        fields.update(overrides)
        return User(**fields)

    def test_a_presented_token_is_admitted_on_unverified_claims_while_disabled(
        self, security, set_settings, authentication_database, captured_logs, monkeypatch
    ):
        """The switch's whole purpose, driven end to end through the real dependency.

        The presented token's signature is worthless and ``verify_id_token`` is stubbed to
        refuse anything it is given, so the admission can only have come from reading the
        claims unverified. The response carries the bypass marker and the bypass is recorded,
        which is what makes a relaxed deployment distinguishable from an enforcing one.
        """
        from firebase_admin import auth as firebase_auth

        verifications = []

        def _refuse(token, app=None, check_revoked=False):
            verifications.append(token)
            raise firebase_auth.InvalidIdTokenError("verification must not be consulted")

        monkeypatch.setattr(firebase_auth, "verify_id_token", _refuse)
        authentication_database(self._user(id=11, email="owner@example.com"))
        set_settings(auth_enforcement_enabled="false")
        handler = captured_logs("backend.app.core.security")

        app = FastAPI()

        @app.get("/protected")
        def protected(current_user=Depends(security.get_current_user)):
            return {"reached": True, "id": current_user.id, "email": current_user.email}

        app.add_middleware(security.AuthEnforcementBypassMarkerMiddleware)
        response = TestClient(app, raise_server_exceptions=False).get(
            "/protected",
            headers={
                "Authorization": "Bearer %s"
                % self._readable_token(email="owner@example.com", email_verified=True)
            },
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "reached": True,
            "id": 11,
            "email": "owner@example.com",
        }
        assert verifications == [], "the token was put through verification"
        assert (
            response.headers[security.AUTH_ENFORCEMENT_BYPASS_HEADER]
            == security.AUTH_ENFORCEMENT_BYPASS_HEADER_VALUE
        )
        messages = [
            record.getMessage()
            for record in handler.records
            if record.levelno >= logging.WARNING
        ]
        assert any(
            "Authentication enforcement is disabled" in message for message in messages
        ), messages

    def test_a_presented_token_carrying_no_readable_claims_is_refused_while_disabled(
        self, security, set_settings, authentication_database, captured_logs
    ):
        """Relaxed verification is not absent verification of the token's shape: a value
        whose claims cannot be read at all is refused, and the refusal is recorded without
        the value reaching the log."""
        authentication_database(self._user())
        set_settings(auth_enforcement_enabled="false")
        handler = captured_logs("backend.app.core.security")

        app = FastAPI()

        @app.get("/protected")
        def protected(current_user=Depends(security.get_current_user)):
            return {"reached": True}

        app.add_middleware(security.AuthEnforcementBypassMarkerMiddleware)
        response = TestClient(app, raise_server_exceptions=False).get(
            "/protected", headers={"Authorization": "Bearer not-a-readable-token"}
        )

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "reached" not in response.text
        assert security.AUTH_ENFORCEMENT_BYPASS_HEADER.lower() not in {
            name.lower() for name in response.headers
        }
        messages = [record.getMessage() for record in handler.records]
        assert any("no readable claims" in message for message in messages), messages
        assert all("not-a-readable-token" not in message for message in messages), messages

    def test_the_bypass_still_requires_a_stored_user(
        self, security, set_settings, authentication_database
    ):
        """The switch skips signature verification; it does not skip the identity lookup.

        Unverified claims naming an address this database does not hold are refused, so a
        bypassed deployment can be impersonated only as a user that already exists. The
        token the caller presented is what the unverified branch reads, and the refusal
        comes from the identity lookup rather than from a caller the switch invented.
        """
        set_settings(auth_enforcement_enabled="false")
        authentication_database()  # an empty identity store: no row for any address
        state = {security._BYPASS_STATE_KEY: False}
        read = {}

        def _unverified(token, exception):
            read["token"] = token
            return {"email": "stranger@example.com", "email_verified": True}

        original = security._unverified_claims
        security._unverified_claims = _unverified
        try:
            with pytest.raises(HTTPException) as raised:
                security._resolve_current_user("a.token.value", state)
        finally:
            security._unverified_claims = original
        assert read["token"] == "a.token.value"
        assert raised.value.status_code == 401
        assert state[security._BYPASS_STATE_KEY] is False

    def test_the_bypass_marker_is_emitted_for_an_admitted_bypassed_request(
        self, security
    ):
        app = FastAPI()

        async def bypassing_dependency():
            security._auth_enforcement_bypassed.set(True)
            return "anonymous"

        @app.get("/bypassed")
        def bypassed(caller=Depends(bypassing_dependency)):
            return {"caller": caller}

        @app.get("/enforced")
        def enforced():
            return {"ok": True}

        app.add_middleware(security.AuthEnforcementBypassMarkerMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        header = security.AUTH_ENFORCEMENT_BYPASS_HEADER
        assert client.get("/bypassed").headers[header] == (
            security.AUTH_ENFORCEMENT_BYPASS_HEADER_VALUE
        )
        assert header.lower() not in {
            name.lower() for name in client.get("/enforced").headers
        }


# ===========================================================================
# V12 - token lifetime comes from configuration
# ===========================================================================
class TestAccessTokenLifetime:
    """V12: a minted token's lifetime comes from ``ACCESS_TOKEN_EXPIRE_MINUTES``."""

    @staticmethod
    def _minutes_until_expiry(token):
        from jose import jwt

        claims = jwt.get_unverified_claims(token)
        expiry = datetime.utcfromtimestamp(claims["exp"])
        return round((expiry - datetime.utcnow()).total_seconds() / 60)

    def test_the_default_lifetime_follows_the_setting(self, set_settings):
        from backend.app.core.security import create_access_token

        set_settings(ACCESS_TOKEN_EXPIRE_MINUTES=45)
        assert self._minutes_until_expiry(create_access_token({"sub": "1"})) == 45

    def test_an_explicit_delta_still_wins(self, set_settings):
        from backend.app.core.security import create_access_token

        set_settings(ACCESS_TOKEN_EXPIRE_MINUTES=45)
        token = create_access_token({"sub": "1"}, expires_delta=timedelta(minutes=5))
        assert self._minutes_until_expiry(token) == 5

    def test_the_signature_is_unchanged(self):
        import inspect

        from backend.app.core.security import create_access_token

        signature = inspect.signature(create_access_token)
        assert list(signature.parameters) == ["data", "expires_delta"]
        assert signature.parameters["expires_delta"].default is None


# ===========================================================================
# V5 / V6 / N1 / N4 / M13 / M15 - the configuration contract
# ===========================================================================
class TestConfigurationContract:
    """The settings surface refuses a value that would weaken a control."""

    @pytest.fixture
    def Settings(self):
        return importlib.import_module("backend.app.core.config").Settings

    # The six field names that existed before this change set, and the eleven the plan adds.
    # Together they are the whole authorized configuration surface.
    ORIGINAL_FIELDS = (
        "PROJECT_ID",
        "DATABASE_URL",
        "REDIS_URL",
        "SECRET_KEY",
        "ALGORITHM",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
    )
    ADDED_FIELDS = (
        "ALLOWED_ORIGINS",
        "gcs_bucket_name",
        "signed_url_expiry_minutes",
        "db_sslmode",
        "auth_token_verifier",
        "auth_enforcement_enabled",
        "rate_limit_enabled",
        "rate_limit_default",
        "rate_limit_write",
        "csp_report_only",
        "firebase_project_id",
    )

    def test_the_six_original_field_names_are_preserved(self, Settings):
        for name in self.ORIGINAL_FIELDS:
            assert name in Settings.__fields__

    def test_the_surface_is_exactly_the_authorized_seventeen_fields(self, Settings):
        """M1: three further fields had been declared beyond the authorized surface.

        Pinned as an equality rather than a count, so both a new field and a removed one
        fail here instead of drifting.
        """
        assert set(Settings.__fields__) == set(self.ORIGINAL_FIELDS + self.ADDED_FIELDS)
        assert len(Settings.__fields__) == 17

    @pytest.mark.parametrize(
        "name",
        [
            "signer_service_account",
            "rate_limit_storage_uri",
            "rate_limit_trusted_proxies",
            "rate_limit_trusted_proxy_hops",
        ],
    )
    def test_a_withdrawn_field_is_not_declared(self, Settings, name):
        """M1: each of these was declared beyond the authorized surface and is withdrawn."""
        assert name not in Settings.__fields__

    def test_no_module_reads_a_withdrawn_field(self):
        """A withdrawn field must not survive as a silently-missing attribute read."""
        sources = list(BACKEND_APP.rglob("*.py"))
        assert sources
        for source in sources:
            text = source.read_text(encoding="utf-8")
            for name in (
                "signer_service_account",
                "rate_limit_storage_uri",
                "rate_limit_trusted_proxies",
            ):
                assert name not in text, "{0} still names {1}".format(source, name)

    def test_the_application_declares_no_unread_api_origin(self, Settings):
        """M13: the field was declared, validated and never read by any Python code.

        The API origin belongs to the static delivery paths, where ``connect-src``
        governs what the browser may call. Checked as an absent field and as the absence
        of any attribute read, rather than as the absence of the string, so prose may
        still name the Terraform variable it belongs to.
        """
        assert "api_origin" not in Settings.__fields__
        sources = list(BACKEND_APP.rglob("*.py"))
        assert sources
        for source in sources:
            text = source.read_text(encoding="utf-8")
            for read in (".api_origin", '"api_origin"', "'api_origin'"):
                assert read not in text, "{0} reads {1}".format(source, read)

    @pytest.mark.parametrize(
        "value",
        ["", " ", "gs://uploads-bucket", "uploads-bucket/object.zip", "Uploads-Bucket",
         " uploads-bucket ", "ab", "-uploads", "uploads-"],
    )
    def test_an_unusable_uploads_bucket_name_is_refused(self, Settings, value):
        """It was the one security-relevant field accepted unvalidated, so an unset value was
        handed to ``client.bucket("")`` and surfaced as an opaque storage error on the first
        upload instead of at start-up - which is where every other field of this set fails.

        The grammar is the one ``infrastructure/terraform/variables.tf`` applies to a bucket
        input, so a name this refuses Terraform refuses too.
        """
        with pytest.raises(pydantic.ValidationError):
            Settings(gcs_bucket_name=value)

    def test_an_absent_uploads_bucket_name_is_refused(self, Settings, monkeypatch):
        """Absence is the case that needs refusing, and a validator that does not run on the
        default would skip exactly that case."""
        monkeypatch.delenv("gcs_bucket_name", raising=False)
        with pytest.raises(pydantic.ValidationError):
            Settings()

    @pytest.mark.parametrize(
        "value", ["excel-clone-user-uploads", "uploads.example.com", "a_b-c.d"]
    )
    def test_a_usable_uploads_bucket_name_is_accepted(self, Settings, value):
        assert Settings(gcs_bucket_name=value).gcs_bucket_name == value

    @pytest.mark.parametrize(
        "value", ["", " ", "  ", "\t", "\n", " excel-clone-test ", "excel-clone-test "]
    )
    def test_an_unusable_project_identifier_is_refused(self, Settings, value):
        """It was the one *required* field accepted unvalidated, so a blank value reached the
        secret paths as ``projects//secrets/...`` and a padded one as
        ``projects/ id /secrets/...`` - both failing on the first provider call instead of at
        construction, which is where every other field of this set fails.

        Whitespace is refused rather than trimmed: the value is compared for equality with
        ``firebase_project_id`` and interpolated into three resource paths, so a silently
        trimmed value would make the accepted spelling differ from the configured one.
        """
        with pytest.raises(pydantic.ValidationError, match="PROJECT_ID"):
            Settings(PROJECT_ID=value)

    def test_an_absent_project_identifier_is_refused(self, Settings, monkeypatch):
        """It has no default, so absence is already a required-field error. Asserted so that
        the validator added beside it cannot accidentally supply one."""
        monkeypatch.delenv("PROJECT_ID", raising=False)
        with pytest.raises(pydantic.ValidationError, match="PROJECT_ID"):
            Settings()

    def test_a_usable_project_identifier_is_accepted_unchanged(self, Settings):
        """And is carried through verbatim, because the Secret Manager path is built from it."""
        settings = Settings(PROJECT_ID="excel-clone-prod")
        assert settings.PROJECT_ID == "excel-clone-prod"

    def test_the_refused_project_identifier_never_reaches_a_secret_path(self, Settings):
        """The consequence the refusal exists to prevent, stated as the assertion.

        ``__init__`` builds ``projects/{PROJECT_ID}`` and then reads three secrets under it. The
        validator runs before ``__init__``'s body, so a blank value cannot reach that path -
        which is what turns a confusing provider error into a start-up refusal.
        """
        source = (BACKEND_APP / "core" / "config.py").read_text(encoding="utf-8")
        assert 'project_path = f"projects/{self.PROJECT_ID}"' in source
        with pytest.raises(pydantic.ValidationError) as refusal:
            Settings(PROJECT_ID="")
        assert "projects//secrets" in str(refusal.value)

    def test_the_verifier_project_may_only_name_the_deployment_project(self, Settings):
        """MJ-3: one effective verifier project, so no plane can accept another issuer."""
        with pytest.raises(pydantic.ValidationError):
            Settings(firebase_project_id="another-project")

    def test_the_verifier_project_may_state_the_deployment_project(self, Settings):
        settings = Settings(firebase_project_id=Settings().PROJECT_ID)
        assert settings.firebase_project_id == settings.PROJECT_ID

    def test_an_empty_verifier_project_resolves_to_the_deployment_project(self, Settings):
        settings = Settings(firebase_project_id="")
        assert (settings.firebase_project_id or settings.PROJECT_ID) == settings.PROJECT_ID

    def test_the_write_budget_admits_the_rate_the_client_can_produce(self, Settings):
        """MJ-4: the client's coalescing window is what sets the aggregate to clear.

        One PUT per worksheet per CELL_WRITE_COALESCE_MS is 60 a minute per worksheet.
        The shared write budget must exceed that for two worksheets edited at once, so a
        normal editing session cannot exhaust it, and must stay under the global ceiling so
        the write tier is still the tighter of the two.
        """
        import re

        client = (
            REPOSITORY_ROOT / "frontend" / "src" / "services" / "api.ts"
        ).read_text(encoding="utf-8")
        window = re.search(r"CELL_WRITE_COALESCE_MS\s*=\s*(\d+)", client)
        assert window, "the client's coalescing window could not be read"
        per_worksheet_per_minute = 60_000 // int(window.group(1))
        assert per_worksheet_per_minute == 60

        settings = Settings()
        write = int(settings.rate_limit_write.split("/")[0])
        ceiling = int(settings.rate_limit_default.split("/")[0])
        assert write > 2 * per_worksheet_per_minute, settings.rate_limit_write
        assert write < ceiling, (settings.rate_limit_write, settings.rate_limit_default)

    @pytest.mark.parametrize("value", ["firebase", "legacy_jwt"])
    def test_supported_verifiers_are_accepted(self, Settings, value):
        assert Settings(auth_token_verifier=value).auth_token_verifier == value

    @pytest.mark.parametrize("value", ["firebse", "legacy-jwt", "", "FIREBASE"])
    def test_an_unsupported_verifier_is_refused(self, Settings, value):
        """N1: only the two supported spellings are accepted, so a misspelling is refused."""
        with pytest.raises(pydantic.ValidationError):
            Settings(auth_token_verifier=value)

    @pytest.mark.parametrize("value", [0, -1, 1441, 100000])
    def test_an_out_of_range_token_lifetime_is_refused(self, Settings, value):
        """N4: the honoured lifetime setting is bounded, so a value outside 1..1440 is
        refused."""
        with pytest.raises(pydantic.ValidationError):
            Settings(ACCESS_TOKEN_EXPIRE_MINUTES=value)

    @pytest.mark.parametrize("value", [1, 15, 1440])
    def test_an_in_range_token_lifetime_is_accepted(self, Settings, value):
        assert Settings(ACCESS_TOKEN_EXPIRE_MINUTES=value).ACCESS_TOKEN_EXPIRE_MINUTES == value

    @pytest.mark.parametrize("value", ["allow", "prefer", "", "REQUIRE", "verify_ca"])
    def test_a_database_mode_that_can_negotiate_plaintext_is_refused(self, Settings, value):
        """V6: the mode reaches psycopg2 unchecked, so it is constrained here.

        ``allow`` and ``prefer`` are the dangerous pair: each of them negotiates plaintext
        whenever the server offers it, with no error to observe. ``disable`` is not listed
        because it is expressible for a local endpoint - the topology tests below are what
        constrain it.
        """
        with pytest.raises(pydantic.ValidationError):
            Settings(db_sslmode=value)

    def test_the_one_deployable_database_mode_is_accepted(self, Settings):
        assert Settings(db_sslmode="require").db_sslmode == "require"

    @pytest.mark.parametrize("value", ["verify-ca", "verify-full"])
    def test_a_database_mode_the_deployment_cannot_complete_is_refused(self, Settings, value):
        """M4: what the settings accept and what the deployment can complete must be one set.

        The Cloud SQL instance is provisioned with no private network, so the only route to it is
        the Cloud SQL Auth Proxy, whose local listener carries no certificate for the instance
        name. Accepting a certificate-verifying mode here would admit a value that fails closed
        at pod start-up, which ``scripts/deploy.sh`` refuses in the manifests for the same reason.
        """
        with pytest.raises(pydantic.ValidationError):
            Settings(db_sslmode=value)

    def test_the_accepted_mode_set_is_exactly_the_deployable_one(self):
        """One vocabulary, so the two planes cannot drift apart silently.

        Two modes are expressible and no more: ``require``, which is the only encrypting
        mode this topology can complete, and ``disable``, which the constructor admits only
        for an endpoint that never leaves the machine. The certificate-verifying modes are
        absent because the proxy's local listener carries no certificate for the instance
        name, and ``allow`` and ``prefer`` are absent because each negotiates plaintext
        silently whenever the server offers it.
        """
        from backend.app.core import config

        assert config.DatabaseSslMode.__args__ == ("disable", "require")
        assert config.ENCRYPTING_DATABASE_SSL_MODES == frozenset({"require"})
        assert config.UNENCRYPTED_DATABASE_SSL_MODE == "disable"

        deploy = (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        assert "verify-ca|verify-full" in deploy, "deploy.sh no longer refuses both modes"

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://excel:secret@127.0.0.1:5432/excel",
            "postgresql://excel:secret@localhost:5432/excel",
            "postgresql://excel:secret@[::1]:5432/excel",
            "postgresql+psycopg2://excel:secret@localhost:5432/excel",
        ],
    )
    def test_the_unencrypted_mode_is_accepted_for_a_local_endpoint(
        self, Settings, monkeypatch, url
    ):
        """C3: the Cloud SQL Auth Proxy presents a plain TCP listener on loopback.

        A client asking for TLS on that listener cannot connect at all, so the mode has to
        be expressible - but only for a connection that never leaves the machine.
        """
        monkeypatch.setenv("DATABASE_URL", url)
        assert Settings(db_sslmode="disable").db_sslmode == "disable"

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://excel:secret@10.0.0.5:5432/excel",
            "postgresql://excel:secret@db.internal:5432/excel",
            "postgresql://excel:secret@34.120.0.1:5432/excel",
        ],
    )
    def test_the_unencrypted_mode_is_refused_for_a_remote_endpoint(
        self, Settings, monkeypatch, url
    ):
        """C3: the plaintext mode must not be able to describe a network connection.

        The check runs in the constructor rather than in a field validator, because the
        ``DATABASE_URL`` a validator sees is the environment's and the constructor replaces
        it with the secret's value - so a validator could be satisfied by a loopback
        placeholder while the resolved connection crossed a network in cleartext.
        """
        monkeypatch.setenv("DATABASE_URL", url)
        with pytest.raises(ValueError) as raised:
            Settings(db_sslmode="disable")
        assert "local endpoint" in str(raised.value)

    def test_the_refusal_never_discloses_the_database_url(self, Settings, monkeypatch):
        """The URL carries the database password, so it must not reach the message."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://excel:s3cr3t@10.0.0.5:5432/excel")
        with pytest.raises(ValueError) as raised:
            Settings(db_sslmode="disable")
        assert "s3cr3t" not in str(raised.value)

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://excel:secret@10.0.0.5:5432/excel",
            "postgresql://excel:secret@127.0.0.1:5432/excel",
        ],
    )
    def test_an_encrypting_mode_needs_no_topology(self, Settings, monkeypatch, url):
        """An encrypting mode is admissible for any endpoint, local or remote."""
        monkeypatch.setenv("DATABASE_URL", url)
        assert Settings(db_sslmode="require").db_sslmode == "require"

    # Origins both planes must refuse. Each is also a rejected vector in the accompanying
    # Terraform validation, so this list and that grammar are one contract.
    REJECTED_ORIGINS = [
        "*",
        "https://*.example.com",
        "http://app.example.com",
        "https://app.example.com/api",
        "https://app.example.com/",
        "app.example.com",
        "https://user:pass@app.example.com",
        "",
        " https://app.example.com",
        "https://app.example.com ",
        "https://APP.example.com",
        "https://app.example.com.",
        "https://app_example.com",
        "https://-app.example.com",
        "https://app.example.com:0",
        "https://app.example.com:65536",
        "https://app.example.com?q=1",
        "https://app.example.com#f",
    ]

    # Origins both planes must accept.
    ACCEPTED_ORIGINS = [
        "https://app.example.com",
        "https://app.example.com:8443",
        "http://localhost:3000",
        "http://127.0.0.1",
        "http://[::1]:3000",
    ]

    @pytest.mark.parametrize("origin", REJECTED_ORIGINS)
    def test_a_weak_cors_origin_is_refused(self, Settings, origin):
        """V5/M5: the credentialed policy was built from an unvalidated list, and the
        validator later accepted values the Terraform grammar refuses."""
        with pytest.raises(pydantic.ValidationError):
            Settings(ALLOWED_ORIGINS=[origin])

    @pytest.mark.parametrize("origin", ACCEPTED_ORIGINS)
    def test_an_exact_origin_is_accepted(self, Settings, origin):
        assert Settings(ALLOWED_ORIGINS=[origin]).ALLOWED_ORIGINS == [origin]

    @pytest.mark.parametrize(
        "origins",
        [
            ["https://APP.example.com"],
            ["HTTPS://app.example.com"],
            ["https://App.Example.Com"],
            ["https://app.example.com:0"],
            ["https://app.example.com:65536"],
            ["https://app.example.com:99999"],
            ["https://app.example.com:"],
            ["https://app.example.com:http"],
            ["https://app_1.example.com"],
            ["https://-app.example.com"],
            ["https://app-.example.com"],
            ["https://app..example.com"],
            ["https://app.example.com."],
        ],
    )
    def test_an_origin_outside_the_shared_grammar_is_refused(self, Settings, origins):
        """M6: these are the shapes the two planes used to disagree about.

        An upper-case host passed the backend and was rejected by Terraform, and it can
        never match a request either way because a browser lower-cases the host before it
        sends the Origin header. An out-of-range port passed both planes and matched
        nothing. Every vector here is also rejected by the Terraform ``allowed_origins``
        validation, which is what makes the grammar one grammar.
        """
        with pytest.raises(pydantic.ValidationError):
            Settings(ALLOWED_ORIGINS=origins)

    @pytest.mark.parametrize(
        "origin",
        [
            "https://app.example.com",
            "https://app.example.com:8443",
            "https://app.example.com:65535",
            "https://a.b.c.example.com",
            "https://my-app.example.com",
            "http://localhost:3000",
            "http://127.0.0.1:8000",
            "http://[::1]:3000",
        ],
    )
    def test_an_origin_inside_the_shared_grammar_is_accepted(self, Settings, origin):
        """M6: the accepted vectors the Terraform validation also accepts."""
        assert Settings(ALLOWED_ORIGINS=[origin]).ALLOWED_ORIGINS == [origin]

    def test_loopback_http_stays_usable_for_development(self, Settings):
        assert Settings(
            ALLOWED_ORIGINS=["http://localhost:3000", "https://app.example.com"]
        ).ALLOWED_ORIGINS == ["http://localhost:3000", "https://app.example.com"]

    def test_the_backend_and_terraform_share_one_origin_grammar(self):
        """M5: three different origin grammars were advertised as one.

        Each expression ``config.py`` compiles must appear character for character in the
        Terraform validation blocks, and must appear twice - once for ``allowed_origins``
        and once for ``api_origin`` - so neither variable can drift from the other.
        """
        config = importlib.import_module("backend.app.core.config")
        terraform = (TERRAFORM / "variables.tf").read_text(encoding="utf-8")
        for pattern in (
            config.ORIGIN_PATTERN,
            config.HTTPS_ORIGIN_PATTERN,
            config.LOOPBACK_HTTP_ORIGIN_PATTERN,
        ):
            # Terraform escapes a backslash inside its double-quoted regex string.
            expression = pattern.pattern.replace("\\", "\\\\")
            assert terraform.count(expression) >= 2, pattern.pattern

    @pytest.mark.parametrize("origin", REJECTED_ORIGINS + ACCEPTED_ORIGINS)
    def test_the_two_planes_agree_on_every_vector(self, Settings, origin):
        """M5: the same value must be accepted by both planes or refused by both."""
        config = importlib.import_module("backend.app.core.config")
        grammar_accepts = bool(
            config.ORIGIN_PATTERN.match(origin)
            and (
                config.HTTPS_ORIGIN_PATTERN.match(origin)
                or config.LOOPBACK_HTTP_ORIGIN_PATTERN.match(origin)
            )
        )
        try:
            Settings(ALLOWED_ORIGINS=[origin])
            settings_accepts = True
        except pydantic.ValidationError:
            settings_accepts = False
        assert grammar_accepts == settings_accepts

    @pytest.mark.parametrize("value", [0, -1, 10081])
    def test_an_out_of_range_signed_url_lifetime_is_refused(self, Settings, value):
        with pytest.raises(pydantic.ValidationError):
            Settings(signed_url_expiry_minutes=value)

    @pytest.mark.parametrize("value", ["none", "None", "RS256", "ES256", "", "hs256"])
    def test_an_unsafe_signing_algorithm_is_refused(self, Settings, value):
        """M3: an unconstrained value admitted ``none``, which strips verification from the
        legacy token path, and asymmetric names whose verification key would be the shared
        secret."""
        with pytest.raises(pydantic.ValidationError):
            Settings(ALGORITHM=value)

    @pytest.mark.parametrize("value", ["HS256", "HS384", "HS512"])
    def test_the_hmac_algorithms_are_accepted(self, Settings, value):
        assert Settings(ALGORITHM=value).ALGORITHM == value

    def test_the_accepted_algorithms_are_one_list(self, Settings):
        """The exported set and the field's type must not drift apart."""
        from backend.app.core.config import SIGNING_ALGORITHMS

        assert SIGNING_ALGORITHMS == frozenset({"HS256", "HS384", "HS512"})

    @pytest.mark.parametrize(
        "value",
        ["short", "x" * 31, "a" * 64, "abababababababababababababababababab"],
    )
    def test_a_weak_signing_key_is_refused(self, Settings, set_settings, value):
        """M3: any value at all was accepted, so a placeholder secret produced forgeable
        tokens. Both the length floor and the distinct-character floor are exercised: the
        last two values are long enough and still nearly entropy-free.

        The check runs in ``__init__`` after the Secret Manager assignment, which is why the
        value is supplied through the environment the stub serves rather than as a keyword.
        """
        set_settings(SECRET_KEY=value)
        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings()

    def test_a_strong_signing_key_is_accepted(self, Settings, set_settings):
        set_settings(SECRET_KEY="Kx7-qP2m_Ze9RvL4tB6nD8sW1yC3jH5gA0uF")
        assert len(Settings().SECRET_KEY) >= 32

    def test_the_signing_key_is_checked_after_the_secret_manager_read(
        self, Settings, set_settings
    ):
        """The three fetched fields overwrite whatever the environment supplied, so a field
        validator would certify a value this process never uses. This asserts the check sees
        the fetched value: the stub serves it from the environment, and a weak value there is
        refused even though the class-level default is absent."""
        set_settings(SECRET_KEY="weak")
        with pytest.raises(ValueError, match="at least 32 characters"):
            Settings()

    @pytest.mark.parametrize(
        "value",
        ["redis://cache.example.com:6379/0", "redis://10.0.0.5:6379"],
    )
    def test_a_cleartext_redis_url_is_refused(self, Settings, set_settings, value):
        """H2/H4: Redis carries the throttling counters and the Celery payloads."""
        set_settings(REDIS_URL=value)
        with pytest.raises(ValueError, match="REDIS_URL"):
            Settings()

    @pytest.mark.parametrize(
        "value",
        [
            "rediss://cache.example.com:6379/0",
            "redis://localhost:6379",
            "redis://127.0.0.1:6379/1",
            "memory://",
        ],
    )
    def test_an_encrypted_or_loopback_redis_url_is_accepted(
        self, Settings, set_settings, value
    ):
        set_settings(REDIS_URL=value)
        assert Settings().REDIS_URL == value

    def test_the_window_store_is_derived_from_the_broker_url(self, Settings):
        """There is no second store setting to keep in step with ``REDIS_URL``."""
        from backend.app.core import rate_limit

        assert "rate_limit_storage_uri" not in Settings.__fields__
        source = code_only((BACKEND_APP / "core" / "rate_limit.py").read_text("utf-8"))
        assert "resolve_storage(settings.REDIS_URL)" in source
        assert rate_limit._IN_PROCESS_STORAGE_URI == "memory://"

    def test_an_unreachable_store_degrades_to_in_process_counting(self):
        """A counter store that does not answer must not take the API down with it."""
        from limits.storage import MemoryStorage

        from backend.app.core.rate_limit import resolve_storage

        store, uri = resolve_storage("redis://127.0.0.1:1/0")
        assert isinstance(store, MemoryStorage)
        assert uri == "memory://"

    def test_no_forwarded_header_can_name_the_metered_client(self, Settings):
        """No header value ever becomes a bucket name, and there is still no trusted-proxy
        list to configure.

        **Corrected.** The earlier form asserted that the resolver's body mentioned no header
        at all, on the reasoning that a resolver which ignores forwarded headers cannot be
        misled by one. QA testing disproved it: uvicorn trusts ``X-Forwarded-For`` from a
        loopback peer by DEFAULT and rewrites ``scope["client"]`` from it before any
        application middleware runs, so ignoring the header meant faithfully metering an
        address the caller had chosen - eight requests rotating the header drew no 429 at
        all. The resolver now reads the headers precisely so it can tell a rewritten peer
        from a transport one, which makes the old assertion the wrong thing to require. What
        matters is the outcome, so that is what is asserted here and behaviourally in
        ``TestThrottlingIdentity``.
        """
        assert "rate_limit_trusted_proxies" not in Settings.__fields__
        assert "rate_limit_trusted_proxy_hops" not in Settings.__fields__

        from backend.app.core.rate_limit import (
            _FORWARDED_CLIENT_KEY,
            _UNKNOWN_CLIENT_KEY,
            resolve_client_key,
        )

        spoofed = "203.0.113.77"
        key = resolve_client_key(
            {
                "type": "http",
                "client": (spoofed, 0),
                "headers": [(b"x-forwarded-for", spoofed.encode("latin-1"))],
            }
        )
        assert key == _FORWARDED_CLIENT_KEY
        assert spoofed not in key
        # The two shared buckets are the only names a caller can steer a request into.
        assert _FORWARDED_CLIENT_KEY != _UNKNOWN_CLIENT_KEY

    def test_the_size_ceilings_are_named_constants_with_their_delivered_values(self):
        """The two ceilings are module constants rather than settings, which is what
        keeps the settings surface to the fields the plan authorises."""
        from backend.app.core import rate_limit
        from backend.app.services import file_storage

        assert rate_limit._MAX_REQUEST_BODY_BYTES == 10 * 1024 * 1024
        assert file_storage.MAX_OBJECT_BYTES == 50 * 1024 * 1024


# ===========================================================================
# V6 - the database connection requires TLS
# ===========================================================================
class TestDatabaseTransportSecurity:
    """V6: the engine is created with the configured TLS mode and bounded timeouts.

    The engine is built once at module import from a value that cannot be re-read without
    reloading the module - and reloading it would hand every other module a stale
    ``get_db`` - so the contract is asserted on the source that builds it, plus on the
    value that source reads.
    """

    @pytest.fixture
    def create_engine_call(self):
        tree = _parse(BACKEND_APP / "db" / "database.py")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "create_engine"
            ):
                return node
        raise AssertionError("backend/app/db/database.py calls no create_engine")

    def test_the_engine_is_built_with_a_transport_argument(self, create_engine_call):
        keywords = {keyword.arg: keyword.value for keyword in create_engine_call.keywords}
        assert "connect_args" in keywords, "the engine is built with no connect_args"
        connect_args = keywords["connect_args"]
        assert isinstance(connect_args, ast.Dict)
        keys = [key.value for key in connect_args.keys]
        assert "sslmode" in keys, keys

    def test_the_transport_mode_comes_from_configuration(self, create_engine_call):
        keywords = {keyword.arg: keyword.value for keyword in create_engine_call.keywords}
        connect_args = keywords["connect_args"]
        value = connect_args.values[[key.value for key in connect_args.keys].index("sslmode")]
        assert isinstance(value, ast.Attribute)
        assert value.attr == "db_sslmode"

    def test_the_configured_mode_cannot_negotiate_plaintext(self):
        """The resolved pair is either an encrypting mode, or plaintext staying local.

        C3 widened the vocabulary to include ``disable`` so the Cloud SQL Auth Proxy's plain
        TCP loopback listener can be described. The invariant that replaces the old
        enumeration is the pair: whenever the mode performs no encryption, the endpoint must
        not be on a network.
        """
        from urllib.parse import urlsplit

        from backend.app.core.config import (
            ENCRYPTING_DATABASE_SSL_MODES,
            LOCAL_DATABASE_HOSTS,
            UNENCRYPTED_DATABASE_SSL_MODE,
            get_settings,
        )

        settings = get_settings()
        if settings.db_sslmode in ENCRYPTING_DATABASE_SSL_MODES:
            return
        assert settings.db_sslmode == UNENCRYPTED_DATABASE_SSL_MODE
        host = urlsplit(settings.DATABASE_URL).hostname
        assert host is None or host in LOCAL_DATABASE_HOSTS, host

    def test_the_topology_gate_runs_after_the_secret_is_resolved(self):
        """C3: a field validator would see the environment's URL, not the secret's.

        Asserted on the constructor's statement order rather than on behaviour, because a
        refactor that moved the check into a validator would still pass every behavioural
        test in an environment whose ``DATABASE_URL`` variable and ``DATABASE_URL`` secret
        happen to agree.
        """
        tree = _parse(BACKEND_APP / "core" / "config.py")
        constructors = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ]
        assert constructors, "backend/app/core/config.py declares no __init__"
        body = constructors[0].body

        def _is_database_url_assignment(statement):
            return isinstance(statement, ast.Assign) and any(
                getattr(target, "attr", None) == "DATABASE_URL" for target in statement.targets
            )

        def _is_topology_gate(statement):
            return (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and getattr(statement.value.func, "attr", None)
                == "_assert_database_transport_topology"
            )

        assignment_positions = [i for i, node in enumerate(body) if _is_database_url_assignment(node)]
        gate_positions = [i for i, node in enumerate(body) if _is_topology_gate(node)]
        assert assignment_positions, "the constructor never resolves DATABASE_URL"
        assert gate_positions, "the constructor never calls the transport topology gate"
        assert min(gate_positions) > max(assignment_positions), (
            "the transport topology gate must run after DATABASE_URL is overwritten with the "
            "secret's value"
        )

    def test_the_engine_and_the_mode_come_from_one_settings_read(self):
        """Two reads would mean two Secret Manager fetches and two chances to disagree."""
        source = (BACKEND_APP / "db" / "database.py").read_text(encoding="utf-8")
        assert source.count("get_settings()") == 1, source


class TestDatabaseConnectionPool:
    """The pool is sized, bounded and health-checked explicitly rather than left on defaults.

    Runtime verification measured what the defaults produced. 600 requests at a concurrency of
    120 returned 284 HTTP 500s - 47 per cent - each after the pool's full 30-second timeout, with
    322 ``QueuePool limit of size 5 overflow 10 reached`` records in the server log and
    ``pg_stat_activity`` showing all 15 pooled connections idle INSIDE an open transaction while
    only one or two executed anything. Separately, terminating this engine's backends server-side
    produced exactly one user-visible 500 before SQLAlchemy invalidated the pool.

    Asserted on the live engine, because the pool is a property of the object rather than of the
    call: unlike ``connect_args``, which SQLAlchemy defers to connect time, these arguments are
    resolved when the pool is constructed and are readable here.
    """

    @pytest.fixture
    def pool(self):
        from backend.app.db.database import engine

        return engine.pool

    def test_the_pool_is_sized_from_the_published_constants(self, pool):
        from backend.app.db import database

        assert pool.size() == database.DB_POOL_SIZE
        assert pool._max_overflow == database.DB_MAX_OVERFLOW
        assert (
            database.DB_POOL_CAPACITY
            == database.DB_POOL_SIZE + database.DB_MAX_OVERFLOW
        )

    def test_a_starved_request_is_not_held_for_the_default_timeout(self, pool):
        """Ten seconds and a fault report, rather than thirty seconds and a 500."""
        from backend.app.db import database

        assert pool._timeout == database.DB_POOL_TIMEOUT_SECONDS
        assert database.DB_POOL_TIMEOUT_SECONDS < 30

    def test_a_connection_the_server_closed_is_not_handed_to_a_caller(self, pool):
        """``pool_pre_ping`` is what makes a failover invisible instead of a burst of 500s."""
        from backend.app.db import database

        assert pool._pre_ping is True
        assert pool._recycle == database.DB_POOL_RECYCLE_SECONDS
        assert database.DB_POOL_RECYCLE_SECONDS > 0

    def test_the_concurrency_bound_equals_the_pool_capacity(self):
        """The invariant that makes pool exhaustion unreachable, asserted as an equality.

        One request in flight needs at most one pooled connection at a time: the identity lookup
        in ``core/security.py`` closes its own Session before the handler's ``Depends(get_db)``
        Session issues a query. So while requests in flight are bounded by the pool's capacity,
        connection demand cannot exceed it. Raising either number without the other breaks that,
        which is why the two are pinned together rather than merely documented.
        """
        from backend.app.core import rate_limit
        from backend.app.db import database

        assert rate_limit._MAX_CONCURRENT_REQUESTS == database.DB_POOL_CAPACITY

    def test_the_identity_lookup_releases_its_connection_before_the_handler_queries(self):
        """The premise of the invariant above, asserted on the source that establishes it."""
        source = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        lookup = source[source.index("def _resolve_current_user") :]
        opened = lookup.index("db_context = get_db()")
        closed = lookup.index("db_context.close()")
        returned = lookup.index("return user", closed)
        assert opened < closed < returned, (
            "_resolve_current_user must close its own Session before it returns, or a request "
            "would hold two pooled connections at once and the concurrency bound would no "
            "longer keep demand inside the pool"
        )


# ===========================================================================
# M3 - the URL the engine is built from selects a driver those arguments fit
# ===========================================================================
class TestDatabaseDriverContract:
    """M3: ``DATABASE_URL`` was any string, and the value the engine used was never checked.

    ``Settings.__init__`` calls ``super().__init__()`` first, so Pydantic validated the
    ENVIRONMENT value and then the constructor overwrote it with the Secret Manager value, which
    Pydantic does not re-validate on assignment. The engine was therefore built from an unchecked
    URL, and because SQLAlchemy defers ``connect_args`` to connect time, a URL selecting another
    dialect constructed an engine at import and failed only on its first connection - reported as
    an invalid ``sslmode`` from inside a request rather than as a configuration fault at start-up.
    """

    @pytest.fixture
    def config(self):
        return importlib.import_module("backend.app.core.config")

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://excel_app:pw@127.0.0.1:5432/main-database",
            "postgresql+psycopg2://excel_app:pw@127.0.0.1:5432/main-database",
            "postgresql+psycopg2://excel_app:pw@db.internal/main-database",
        ],
    )
    def test_a_supported_synchronous_postgresql_url_is_accepted(self, config, url):
        assert config.validate_database_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite://",  # builds an engine, then fails on connect with an invalid sslmode
            "sqlite:///./local.db",
            "postgres://excel_app:pw@127.0.0.1:5432/main-database",  # SQLAlchemy 1.4 refuses it
            "postgresql+asyncpg://excel_app:pw@127.0.0.1:5432/main-database",  # async driver
            "postgresql+psycopg://excel_app:pw@127.0.0.1:5432/main-database",  # psycopg 3
            "mysql+pymysql://excel_app:pw@127.0.0.1:3306/main-database",
            "postgresql+psycopg2:///main-database",  # no host
            "postgresql+psycopg2://excel_app:pw@127.0.0.1:5432/",  # no database
            "postgresql+psycopg2://excel_app:pw@127.0.0.1:5432",  # no database
            "",
            "   ",
            "not-a-url",
        ],
    )
    def test_an_unsupported_url_is_refused(self, config, url):
        with pytest.raises(ValueError):
            config.validate_database_url(url)

    def test_a_rejection_never_quotes_the_url(self, config):
        """The URL carries the database password, so no message may echo it."""
        secret = "postgresql+asyncpg://excel_app:sup3r-s3cret@db.internal:5432/main-database"
        with pytest.raises(ValueError) as raised:
            config.validate_database_url(secret)
        assert "sup3r-s3cret" not in str(raised.value)
        assert secret not in str(raised.value)

    def test_the_environment_value_is_validated_at_construction(self, config, set_settings):
        set_settings(DATABASE_URL="sqlite://")
        with pytest.raises(pydantic.ValidationError):
            config.Settings()

    def test_the_fetched_secret_is_validated_after_it_is_assigned(self, config, monkeypatch):
        """The engine is built from the SECRET, not from the environment value."""
        import conftest

        class _UnsupportedUrlClient(conftest.StubSecretManagerServiceClient):
            def access_secret_version(self, request=None):
                name = (request or {})["name"]
                if name.endswith("/secrets/DATABASE_URL/versions/latest"):
                    return conftest._StubSecretVersion("sqlite://")
                return super().access_secret_version(request)

        monkeypatch.setattr(
            config.secretmanager, "SecretManagerServiceClient", _UnsupportedUrlClient
        )
        with pytest.raises(ValueError):
            config.Settings()

    def test_the_engine_module_refuses_an_unsupported_dialect(self):
        """The guard that holds when neither validator ran, asserted on its own logic.

        The engine is built once at module import, so the module cannot be re-imported with a
        different URL without handing every other module a stale ``get_db``. The condition itself
        is therefore exercised directly against the constants the module declares.
        """
        from sqlalchemy.engine import make_url

        from backend.app.db import database

        assert database.REQUIRED_DATABASE_BACKEND == "postgresql"
        assert database.REQUIRED_DATABASE_DRIVER == "psycopg2"

        source = (BACKEND_APP / "db" / "database.py").read_text(encoding="utf-8")
        assert "raise RuntimeError" in source
        assert source.index("make_url") < source.index("create_engine(")

        for rejected in ("sqlite://", "postgresql+asyncpg://u:p@h/d", "mysql+pymysql://u:p@h/d"):
            url = make_url(rejected)
            assert (
                url.get_backend_name() != database.REQUIRED_DATABASE_BACKEND
                or url.get_driver_name() != database.REQUIRED_DATABASE_DRIVER
            ), rejected

        accepted = make_url("postgresql+psycopg2://u:p@h:5432/d")
        assert accepted.get_backend_name() == database.REQUIRED_DATABASE_BACKEND
        assert accepted.get_driver_name() == database.REQUIRED_DATABASE_DRIVER

    def test_the_live_engine_uses_the_required_dialect(self):
        """The engine the application actually holds, not just the guard protecting it."""
        from backend.app.db import database

        assert database.engine.dialect.name == "postgresql"
        assert database.engine.dialect.driver == "psycopg2"


# ===========================================================================
# V4 / N3 - object storage
# ===========================================================================
class _FakeBlob:
    def __init__(self, name="workbooks/report.xlsx", signing_fails=False):
        self.name = name
        self.generation = 1729
        self.signing_fails = signing_fails
        self.uploaded = None
        self.made_public = False
        self.signed_with = None
        self.deleted_generation = None
        self.size = None
        self.downloaded = False

    def reload(self):
        return None

    def download_as_bytes(self):
        self.downloaded = True
        return b"content"

    def upload_from_string(self, content, content_type=None):
        self.uploaded = (content, content_type)

    def make_public(self):  # pragma: no cover - must never run
        self.made_public = True
        raise AssertionError("make_public must never be called")

    def generate_signed_url(self, **kwargs):
        self.signed_with = kwargs
        if self.signing_fails:
            raise RuntimeError("signing refused")
        return "https://storage.googleapis.com/signed-object"

    def delete(self, if_generation_match=None):
        self.deleted_generation = if_generation_match


class _FakeBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, name):
        return self._blob


class _FakeSigningCredentials:
    valid = True
    token = "an-access-token"
    service_account_email = "runtime@excel-clone-test.iam.gserviceaccount.com"


class TestObjectStorageAccess:
    """V4: an upload sets no public ACL and returns a time-limited signed URL."""

    @pytest.fixture
    def service_for(self):
        from backend.app.services.file_storage import FileStorageService

        def _build(blob, max_object_bytes=50 * 1024 * 1024):
            service = FileStorageService.__new__(FileStorageService)
            service._bucket = _FakeBucket(blob)
            service._signed_url_expiration = timedelta(minutes=15)
            service._max_object_bytes = max_object_bytes
            service._signing_credentials = _FakeSigningCredentials()
            service._auth_request = None
            return service

        return _build

    def test_no_public_acl_is_ever_granted(self, service_for):
        blob = _FakeBlob()
        service_for(blob).upload_file(b"content", blob.name)
        assert blob.made_public is False

    def test_the_source_contains_no_public_acl_call(self):
        source = code_only(
            (BACKEND_APP / "services" / "file_storage.py").read_text(encoding="utf-8")
        )
        assert "make_public" not in source
        assert "public_url" not in source

    def test_the_returned_url_is_a_bounded_v4_signature(self, service_for):
        blob = _FakeBlob()
        url = service_for(blob).upload_file(b"content", blob.name)
        assert url == "https://storage.googleapis.com/signed-object"
        assert blob.signed_with["version"] == "v4"
        assert blob.signed_with["method"] == "GET"
        assert blob.signed_with["expiration"] == timedelta(minutes=15)

    def test_the_signature_is_bound_to_the_generation_just_written(self, service_for):
        blob = _FakeBlob()
        service_for(blob).upload_file(b"content", blob.name)
        assert blob.signed_with["generation"] == blob.generation

    def test_signing_names_the_runtimes_own_identity(self, service_for):
        """M1: the signer address comes from the runtime's credentials, not from a setting.

        One service account is both the API runtime identity and the signer, and it holds
        ``roles/iam.serviceAccountTokenCreator`` on itself, so the address to sign as is
        the one already attached to the process.
        """
        blob = _FakeBlob()
        service_for(blob).upload_file(b"content", blob.name)
        assert (
            blob.signed_with["service_account_email"]
            == _FakeSigningCredentials.service_account_email
        )
        assert blob.signed_with["access_token"] == "an-access-token"

    def test_an_object_that_cannot_be_signed_is_removed(self, service_for):
        blob = _FakeBlob(signing_fails=True)
        with pytest.raises(RuntimeError):
            service_for(blob).upload_file(b"content", blob.name)
        assert blob.deleted_generation == blob.generation


class TestObjectSizeCeiling:
    """M2 and M6: the ceiling is configuration, and its refusal discloses no object name."""

    #: A name carrying another user's address, so a refusal quoting it would disclose it.
    SENSITIVE_NAME = "uploads/jane.doe@example.com/Q4 pay.xlsx"

    @pytest.fixture
    def service_for(self):
        from backend.app.services.file_storage import FileStorageService

        def _build(blob, max_object_bytes):
            service = FileStorageService.__new__(FileStorageService)
            service._bucket = _FakeBucket(blob)
            service._signed_url_expiration = timedelta(minutes=15)
            service._max_object_bytes = max_object_bytes
            service._signing_credentials = _FakeSigningCredentials()
            service._auth_request = None
            return service

        return _build

    def test_an_object_over_the_configured_ceiling_is_refused(self, service_for):
        blob = _FakeBlob(name=self.SENSITIVE_NAME)
        with pytest.raises(ValueError, match="over the 1024-byte maximum"):
            service_for(blob, 1024).upload_file(b"x" * 2048, blob.name)
        assert blob.uploaded is None, "the object must not be written before it is measured"

    def test_the_ceiling_follows_the_setting_rather_than_a_constant(self, service_for):
        """The same content refused at one ceiling must be written at a wider one."""
        content = b"x" * 2048
        narrow = _FakeBlob()
        with pytest.raises(ValueError):
            service_for(narrow, 1024).upload_file(content, narrow.name)
        wide = _FakeBlob()
        assert service_for(wide, 65536).upload_file(content, wide.name)
        assert wide.uploaded[0] == content

    def test_the_ceiling_is_one_named_value_the_instance_reads(self):
        """The ceiling is a named module constant rather than a literal buried in the
        check, and an instance defaults to it, so there is one value to change."""
        from backend.app.services import file_storage

        assert file_storage.MAX_OBJECT_BYTES == 50 * 1024 * 1024
        source = code_only(
            (BACKEND_APP / "services" / "file_storage.py").read_text(encoding="utf-8")
        )
        assert "self._max_object_bytes = MAX_OBJECT_BYTES" in source

    def test_the_refusal_discloses_no_object_name(self, service_for):
        """M2: the name is caller-supplied and this exception's text reaches the caller,
        because the route handlers answer with ``str(e)``. A refusal quoting the name
        published whatever was in a workbook title and confirmed a guessed name."""
        from backend.app.services import file_storage

        blob = _FakeBlob(name=self.SENSITIVE_NAME)
        with pytest.raises(ValueError) as raised:
            service_for(blob, 1024).upload_file(b"x" * 2048, blob.name)
        message = str(raised.value)
        assert "jane.doe@example.com" not in message
        assert "Q4 pay" not in message
        assert file_storage._object_log_identifier(self.SENSITIVE_NAME) in message

    def test_an_object_of_unknown_size_is_refused_without_its_name(self, service_for):
        """A size Cloud Storage did not report is not treated as within the limit."""
        blob = _FakeBlob(name=self.SENSITIVE_NAME)
        service = service_for(blob, 1024)
        with pytest.raises(ValueError) as raised:
            service._require_within_size_limit(None, self.SENSITIVE_NAME)
        message = str(raised.value)
        assert "reported no size" in message
        assert "jane.doe@example.com" not in message

    def test_a_download_over_the_ceiling_is_refused_before_the_transfer(self, service_for):
        blob = _FakeBlob(name=self.SENSITIVE_NAME)
        blob.size = 4096
        with pytest.raises(ValueError, match="over the 1024-byte maximum"):
            service_for(blob, 1024).download_file(blob.name)
        assert blob.downloaded is False


class TestStorageLogSanitisation:
    """N3: the cleanup log identifies an object by a digest of its name, never by the
    name."""

    ADVERSARIAL_NAME = "uploads/jane.doe@example.com/Q4 pay.xlsx\nWARN forged line"

    @pytest.fixture
    def file_storage(self):
        return importlib.import_module("backend.app.services.file_storage")

    def test_the_identifier_is_a_stable_digest(self, file_storage):
        import re

        identifier = file_storage._object_log_identifier(self.ADVERSARIAL_NAME)
        assert re.fullmatch(r"sha256:[0-9a-f]{16}", identifier)
        assert identifier == file_storage._object_log_identifier(self.ADVERSARIAL_NAME)
        assert identifier != file_storage._object_log_identifier("another/name")

    def test_a_nameless_object_is_labelled(self, file_storage):
        assert file_storage._object_log_identifier(None) == "unnamed"

    def test_the_cleanup_log_carries_the_digest_and_not_the_name(
        self, file_storage, captured_logs
    ):
        from backend.app.services.file_storage import FileStorageService

        handler = captured_logs("backend.app.services.file_storage")

        class _UndeletableBlob(_FakeBlob):
            def delete(self, if_generation_match=None):
                raise RuntimeError("cleanup refused")

        blob = _UndeletableBlob(name=self.ADVERSARIAL_NAME)
        service = FileStorageService.__new__(FileStorageService)
        service._delete_uploaded_generation(blob)

        messages = "\n".join(record.getMessage() for record in handler.records)
        assert "remains stored" in messages
        assert file_storage._object_log_identifier(self.ADVERSARIAL_NAME) in messages
        assert str(blob.generation) in messages
        for fragment in ("jane.doe@example.com", "Q4 pay", "forged line"):
            assert fragment not in messages, messages


# ===========================================================================
# V5 / V7 / V9 / M8 / M14 - the response pipeline
# ===========================================================================
def _entry_point_cors_keywords():
    """Return the keyword nodes of the ``CORSMiddleware`` registration in ``main.py``.

    ``backend.app.main`` cannot be imported, so the cross-origin policy the application
    actually serves is read from the syntax tree of the file that configures it.

    Raises:
        AssertionError: If the entry point does not register exactly one
            ``CORSMiddleware``, which would mean the policy is configured somewhere this
            module does not read.
    """
    tree = _parse(BACKEND_APP / "main.py")
    registrations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "add_middleware"
        and node.args
        and getattr(node.args[0], "id", None) == "CORSMiddleware"
    ]
    assert len(registrations) == 1, registrations
    return {keyword.arg: keyword.value for keyword in registrations[0].keywords}


def _entry_point_name_value(name):
    """Resolve a bare name the entry point passes to a middleware keyword.

    Only the one module ``main.py`` imports the name from is imported. ``main.py`` itself
    cannot be imported and neither can most of what it imports, so importing the single
    module that binds the name keeps the value read from the entry point rather than
    restated here.

    Raises:
        AssertionError: If the entry point passes a name it does not import, which this
            module cannot resolve and must therefore not silently ignore.
    """
    for node in _parse(BACKEND_APP / "main.py").body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return getattr(importlib.import_module(node.module), alias.name)
    raise AssertionError("main.py passes the name %r, which it does not import" % name)


def _resolve_entry_point_value(keyword, node):
    """Resolve one ``CORSMiddleware`` keyword to the value the entry point gives it.

    A ``settings.<FIELD>`` reference resolves through the live settings, a bare name through
    the module the entry point imports it from, a list elementwise, and anything else as a
    literal.

    Raises:
        AssertionError: If the keyword is written in a form this function cannot resolve. It
            raises rather than skipping, because a silently unresolved keyword would leave
            that part of the policy unasserted.
    """
    if isinstance(node, ast.Attribute) and getattr(node.value, "id", None) == "settings":
        from backend.app.core.config import get_settings

        return getattr(get_settings(), node.attr)
    if isinstance(node, ast.Name):
        return _entry_point_name_value(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_resolve_entry_point_value(keyword, element) for element in node.elts]
    try:
        return ast.literal_eval(node)
    except ValueError:
        raise AssertionError(
            "main.py passes %s=%s to CORSMiddleware, which this module cannot resolve"
            % (keyword, ast.dump(node)[:120])
        )


def _entry_point_cors_options():
    """Return the entry point's cross-origin policy as the values it resolves to.

    Every keyword is resolved from ``main.py`` itself, so ``_build_application`` composes the
    policy the application serves rather than a copy of it kept in this module.
    """
    return {
        name: _resolve_entry_point_value(name, node)
        for name, node in _entry_point_cors_keywords().items()
    }


def _main_constant(name):
    """Return a module-level integer constant from ``main.py``, which cannot be imported.

    Read from the source rather than restated here, so the mirror below cannot quietly
    describe different settings from the ones the application ships.
    """
    for node in _parse(BACKEND_APP / "main.py").body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return node.value.value
    raise AssertionError("main.py defines no constant named %r" % name)


def main_compression_minimum_size():
    """The smallest response ``main.py`` compresses."""
    return _main_constant("COMPRESSION_MINIMUM_SIZE")


def main_compression_level():
    """The deflate level ``main.py`` compresses at."""
    return _main_constant("COMPRESSION_LEVEL")


def _build_application(
    default_limit="600/minute",
    write_limit="300/minute",
    throttling_enabled=True,
):
    """Compose the middleware stack in exactly the order ``main.py`` registers it.

    The cross-origin policy is read from ``main.py`` rather than restated here, so a policy
    weakened in the entry point is the policy every test below exercises.
    """
    from backend.app.core.rate_limit import register_rate_limiting
    from backend.app.core.security import AuthEnforcementBypassMarkerMiddleware
    from backend.app.core.security_headers import (
        SecurityHeadersMiddleware,
        ServerErrorBoundaryMiddleware,
    )

    os.environ["rate_limit_default"] = default_limit
    os.environ["rate_limit_write"] = write_limit
    os.environ["rate_limit_enabled"] = "true" if throttling_enabled else "false"

    app = FastAPI()

    @app.get("/probe")
    def probe():
        return {"ok": True}

    @app.post("/probe")
    def probe_write():
        return {"ok": True}

    @app.get("/second-probe")
    def second_probe():
        return {"ok": True}

    @app.get("/parameterised/{identifier}")
    def parameterised(identifier: str):
        return {"ok": identifier}

    @app.get("/refused")
    def refused():
        raise HTTPException(
            status_code=401, detail="no", headers={"WWW-Authenticate": "Bearer"}
        )

    @app.get("/failing")
    def failing():
        raise RuntimeError("a deliberately unhandled failure")

    @app.post("/consuming")
    async def consuming(request: Request):
        """Reads its request body, which is what the cell-update and share routes do."""
        return {"read": len(await request.body())}

    @app.get("/bulky")
    def bulky():
        # Comfortably above the compression threshold, and repetitive enough that a
        # compressed body is unmistakably smaller than the one that went in.
        return {"rows": ["a padded and highly repetitive row value" for _ in range(400)]}

    app.add_middleware(ServerErrorBoundaryMiddleware)
    app.add_middleware(AuthEnforcementBypassMarkerMiddleware)
    app.add_middleware(
        GZipMiddleware,
        minimum_size=main_compression_minimum_size(),
        compresslevel=main_compression_level(),
    )
    app.add_middleware(CORSMiddleware, **_entry_point_cors_options())
    register_rate_limiting(app)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _expected_header_names():
    from backend.app.core.security_headers import (
        CONTENT_SECURITY_POLICY_HEADER,
        STATIC_SECURITY_HEADERS,
    )

    return [CONTENT_SECURITY_POLICY_HEADER] + list(STATIC_SECURITY_HEADERS)


class TestSecurityResponseHeaders:
    """V7 and M8: every response carries the canonical header set, a 500 included."""

    @pytest.fixture
    def client(self):
        return TestClient(_build_application(), raise_server_exceptions=False)

    def test_headers_on_a_successful_response(self, client):
        response = client.get("/probe")
        assert response.status_code == 200
        for name in _expected_header_names():
            assert name in response.headers, name

    def test_headers_on_a_handled_refusal(self, client):
        response = client.get("/refused")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        for name in _expected_header_names():
            assert name in response.headers, name

    def test_an_unhandled_exception_becomes_a_headed_500(self, client):
        response = client.get("/failing")
        assert response.status_code == 500
        for name in _expected_header_names():
            assert name in response.headers, name

    def test_the_500_reveals_nothing_about_the_failure(self, client):
        response = client.get("/failing")
        assert response.json() == {"detail": "Internal Server Error"}
        assert "RuntimeError" not in response.text
        assert "deliberately unhandled" not in response.text

    def test_the_unhandled_failure_is_recorded_with_its_traceback(self, captured_logs):
        handler = captured_logs("backend.app.core.security_headers")
        TestClient(_build_application(), raise_server_exceptions=False).get("/failing")
        errors = [r for r in handler.records if r.levelno >= logging.ERROR]
        assert errors, [r.getMessage() for r in handler.records]
        assert errors[0].exc_info is not None

    def test_headers_on_a_cors_preflight(self, client):
        response = client.options(
            "/probe",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        for name in _expected_header_names():
            assert name in response.headers, name

    def test_the_report_only_switch_selects_the_other_header_name(self, set_settings):
        from backend.app.core.security_headers import (
            CONTENT_SECURITY_POLICY_HEADER,
            CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER,
        )

        set_settings(csp_report_only="true")
        response = TestClient(
            _build_application(), raise_server_exceptions=False
        ).get("/probe")
        assert CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER in response.headers
        assert CONTENT_SECURITY_POLICY_HEADER not in response.headers

    def test_the_policy_denies_framing_and_pins_script_sources(self):
        from backend.app.core.security_headers import (
            CONTENT_SECURITY_POLICY,
            STATIC_SECURITY_HEADERS,
        )

        assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
        assert "script-src 'self'" in CONTENT_SECURITY_POLICY
        assert "object-src 'none'" in CONTENT_SECURITY_POLICY
        assert STATIC_SECURITY_HEADERS["X-Frame-Options"] == "DENY"
        assert STATIC_SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
        assert "max-age=63072000" in STATIC_SECURITY_HEADERS[
            "Strict-Transport-Security"
        ]


class TestSecurityLogSanitisation:
    """M4: the request line reaches a log record escaped rather than verbatim."""

    #: A path whose decoded form ends the record and starts a forged one.
    FORGED_PATH = "/probe\r\nWARNING:root:forged-record injected by the caller"

    @staticmethod
    def _scope(method="GET", path="/probe"):
        return {"type": "http", "method": method, "path": path}

    def test_a_plain_request_line_survives_unchanged(self):
        from backend.app.core.security_headers import describe_request

        assert describe_request(self._scope("PUT", "/workbooks/1/cells")) == (
            "PUT /workbooks/1/cells"
        )

    @pytest.mark.parametrize("character", ["\r", "\n", "\x00", "\x1b", "\u2028"])
    def test_a_record_separator_cannot_reach_the_log(self, character):
        from backend.app.core.security_headers import describe_request

        described = describe_request(self._scope(path="/probe%sinjected" % character))
        assert character not in described
        assert "injected" in described

    def test_the_description_is_bounded(self):
        from backend.app.core.security_headers import describe_request

        described = describe_request(self._scope(path="/" + "a" * 5000))
        assert len(described) <= 200

    def test_an_unhandled_failure_logs_the_escaped_request_line(self, captured_logs):
        """The boundary middleware records the request it was serving, and that line is
        client-controlled: it must arrive at the record already escaped."""
        from backend.app.core.security_headers import ServerErrorBoundaryMiddleware

        handler = captured_logs("backend.app.core.security_headers")

        async def failing_app(scope, receive, send):
            raise RuntimeError("deliberate")

        middleware = ServerErrorBoundaryMiddleware(failing_app)
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            middleware(self._scope(path=self.FORGED_PATH), receive, send)
        )
        assert sent[0]["status"] == 500
        messages = [record.getMessage() for record in handler.records]
        assert messages
        for message in messages:
            assert "\r" not in message and "\n" not in message
            assert "%0D%0A" in message

    def test_the_bypass_audit_record_is_escaped_too(self, captured_logs):
        """The bypass warning names the admitted request, from the same source."""
        security = importlib.import_module("backend.app.core.security")
        handler = captured_logs("backend.app.core.security")
        token = security._request_description.set(
            importlib.import_module(
                "backend.app.core.security_headers"
            ).describe_request(self._scope(path=self.FORGED_PATH))
        )
        try:
            description = security._request_description.get()
        finally:
            security._request_description.reset(token)
        assert "\r" not in description and "\n" not in description
        assert "%0D%0A" in description
        assert handler.records == []


class TestCrossOriginPolicy:
    """V5: the preflight admits only a listed origin and only the declared headers."""

    @pytest.fixture
    def client(self):
        return TestClient(_build_application(), raise_server_exceptions=False)

    def test_an_allowed_origin_is_echoed_with_credentials(self, client):
        response = client.options(
            "/probe",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.headers["access-control-allow-origin"] == "https://app.example.com"
        assert response.headers["access-control-allow-credentials"] == "true"

    def test_a_disallowed_origin_is_refused_at_the_preflight(self, client):
        response = client.options(
            "/probe",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers

    def test_a_disallowed_origin_gets_no_allow_origin_on_a_simple_request(self, client):
        response = client.get("/probe", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    def test_the_method_and_header_lists_are_finite(self, client):
        response = client.options(
            "/probe",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.headers["access-control-allow-methods"] == "GET, POST, PUT, OPTIONS"
        allowed = response.headers["access-control-allow-headers"]
        assert "*" not in allowed
        assert "Authorization" in allowed and "Content-Type" in allowed

    def test_an_arbitrary_requested_header_is_not_reflected(self, client):
        response = client.options(
            "/probe",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Anything",
            },
        )
        assert "x-anything" not in response.headers.get(
            "access-control-allow-headers", ""
        ).lower()

    # -- The policy the ENTRY POINT configures ------------------------------------------
    # The behavioural tests above run against a stack this module composes, so on their own
    # they cannot see a wildcard reintroduced in backend/app/main.py. These read that file.
    def test_the_entry_point_admits_no_wildcard_in_any_of_the_three(self):
        """A wildcard in any of the three is the V5 defect, and `allow_origins=["*"]` with
        `allow_credentials=True` is worse than the unresolvable field the fix replaced."""
        for name, value in _entry_point_cors_options().items():
            values = value if isinstance(value, (list, tuple)) else [value]
            assert "*" not in values, (name, value)

    def test_the_entry_point_takes_its_origins_from_the_settings_field(self):
        """A literal origin list in the entry point would bypass ``Settings`` entirely, and
        with it the validator that refuses a weak origin."""
        origins = _entry_point_cors_keywords()["allow_origins"]
        assert isinstance(origins, ast.Attribute), ast.dump(origins)
        assert origins.attr == "ALLOWED_ORIGINS"
        assert getattr(origins.value, "id", None) == "settings"

    def test_the_entry_point_declares_the_finite_method_and_header_lists(self):
        """The four methods the routes use and the two headers the client sends."""
        options = _entry_point_cors_options()
        assert options["allow_methods"] == ["GET", "POST", "PUT", "OPTIONS"]
        assert options["allow_headers"] == ["Authorization", "Content-Type"]

    def test_the_entry_point_keeps_credentials_enabled(self):
        """The API accepts a bearer credential cross-origin, so the browser must be told the
        response may be read - and this is the keyword that makes a wildcard dangerous."""
        assert _entry_point_cors_options()["allow_credentials"] is True

    def test_the_entry_point_exposes_only_the_throttling_retry_header(self):
        """A cross-origin caller cannot read a response header that is neither CORS-safelisted
        nor exposed, and the client seam reads ``Retry-After`` to honour a throttling refusal.
        Exposing anything beyond it would widen what a cross-origin page may read."""
        from backend.app.core.rate_limit import RETRY_AFTER_HEADER

        assert _entry_point_cors_options()["expose_headers"] == [RETRY_AFTER_HEADER]

    def test_the_entry_point_configures_nothing_this_module_ignores(self):
        """Every keyword the entry point passes is resolved and asserted above. A sixth one
        would be part of the served policy that no test reads."""
        assert set(_entry_point_cors_keywords()) == {
            "allow_origins",
            "allow_credentials",
            "allow_methods",
            "allow_headers",
            "expose_headers",
        }


# ===========================================================================
# Observability - every security record is filterable and single-line
# ===========================================================================
class TestLoggingConfiguration:
    """F-C: nothing configured the loggers, so the security records were unusable.

    Every control in ``backend/app/core`` records what it refused through the standard
    library's loggers, and no handler, level or format was ever installed for them. The
    records reached ``logging.lastResort``, which writes the bare message: no level, no
    timestamp, no logger name - so a throttling degradation read exactly like ordinary output
    and severity-based alerting had nothing to filter on - and which drops everything below
    ``WARNING``, so the record naming the window store never appeared at all.

    Each test restores the logging state it found, because the root logger is process-wide.
    """

    @pytest.fixture
    def logging_state(self):
        """Install the configuration against a captured stream and restore it afterwards.

        The handler is found by the tag the bootstrap sets on it, not by position.
        ``configure_logging`` is idempotent - it re-uses its tagged handler rather than adding a
        second one - so once any earlier test in the session has configured logging,
        ``root.handlers[-1]`` is somebody else's handler, in a pytest run pytest's own capture
        handler. Re-pointing that one leaves the managed handler still writing to stderr and
        this fixture's buffer empty, which failed two tests under any order that ran another
        logging test first. Found by execution rather than by review: the pair
        ``test_the_server_access_logger_is_escaped_too`` then
        ``test_every_record_carries_a_timestamp_and_its_logger_name`` reproduces it every time,
        and shuffle seeds 20260809 and 8675309 each hit it.

        The handler's original stream is restored as well as the handler list, because the
        handler is process-wide: restoring only the list leaves the shared handler pointed at a
        buffer this test has finished with, which is the same defect in the other direction.
        """
        import io
        import logging as logging_module

        from backend.app.core.logging_config import (
            _MANAGED_HANDLER_ATTRIBUTE,
            configure_logging,
        )

        root = logging_module.getLogger()
        application = logging_module.getLogger("backend")
        previous_handlers = list(root.handlers)
        previous_level = root.level
        previous_application_level = application.level
        handler = None
        previous_stream = None
        try:
            configure_logging()
            managed = [
                candidate
                for candidate in root.handlers
                if getattr(candidate, _MANAGED_HANDLER_ATTRIBUTE, False)
            ]
            assert len(managed) == 1, root.handlers
            handler = managed[0]
            previous_stream = handler.stream
            buffer = io.StringIO()
            handler.stream = buffer
            yield buffer
        finally:
            if handler is not None:
                handler.stream = previous_stream
            root.handlers = previous_handlers
            root.setLevel(previous_level)
            application.setLevel(previous_application_level)

    @staticmethod
    def _emit(level, message, *args, **kwargs):
        import logging as logging_module

        getattr(logging_module.getLogger("backend.app.core.security"), level)(
            message, *args, **kwargs
        )

    @pytest.mark.parametrize(
        "level, expected",
        [("warning", "WARNING"), ("error", "ERROR"), ("info", "INFO")],
    )
    def test_every_record_states_its_level(self, logging_state, level, expected):
        self._emit(level, "a security record")
        assert expected in logging_state.getvalue()

    def test_an_informational_record_is_no_longer_discarded(self, logging_state):
        """``lastResort`` filters at WARNING, so the record naming the counting store - the
        one an operator needs to know whether quotas are shared or per worker - vanished."""
        self._emit("info", "Request throttling enabled: counted in %s", "memory://")
        assert "memory://" in logging_state.getvalue()

    def test_every_record_carries_a_timestamp_and_its_logger_name(self, logging_state):
        self._emit("warning", "a security record")
        emitted = logging_state.getvalue()
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", emitted), emitted
        assert "backend.app.core.security" in emitted

    def test_a_logged_value_cannot_forge_a_second_record(self, logging_state):
        """CWE-117. The address a request is metered against can be supplied in a header, and
        a value carrying a newline would otherwise write what reads as another entry."""
        self._emit(
            "warning", "peer=%s refused", "1.2.3.4\tFORGED\nWARNING: fabricated entry"
        )
        emitted = logging_state.getvalue().strip()
        assert len(emitted.splitlines()) == 1, emitted
        assert "\\x09" in emitted
        assert "\\x0a" in emitted

    def test_a_traceback_keeps_the_newlines_that_make_it_readable(self, logging_state):
        """Escaping is confined to the message: the exception text is generated by the
        interpreter, not supplied by a caller, and a one-line traceback is unusable."""
        try:
            raise ValueError("a provider fault")
        except ValueError:
            self._emit("error", "verification could not be completed", exc_info=True)
        emitted = logging_state.getvalue()
        assert "Traceback" in emitted
        assert len(emitted.strip().splitlines()) > 1

    def test_configuring_twice_does_not_duplicate_every_record(self, logging_state):
        """The entry point is imported once per process in production and more than once
        across a test session, and a duplicated handler doubles every line."""
        import logging as logging_module

        from backend.app.core.logging_config import configure_logging

        configure_logging()
        managed = [
            handler
            for handler in logging_module.getLogger().handlers
            if getattr(handler, "_excel_clone_managed", False)
        ]
        assert len(managed) == 1

    def test_the_server_access_logger_is_escaped_too(self):
        """It records the peer address and the request line, both caller-influenced, and the
        server gives it its own handler with propagation off - so the root handler's formatter
        never sees it and a filter is the only reach."""
        import logging as logging_module

        from backend.app.core.logging_config import (
            ControlCharacterEscapingFilter,
            configure_logging,
        )

        configure_logging()
        for name in ("uvicorn.access", "uvicorn.error"):
            logger = logging_module.getLogger(name)
            assert any(
                isinstance(existing, ControlCharacterEscapingFilter)
                for existing in logger.filters
            ), name

    def test_the_entry_point_configures_logging_before_anything_logs(self):
        """Asserted on the entry point's syntax tree: the call must precede the middleware
        registrations, or the records they emit at import predate the configuration."""
        tree = _parse(BACKEND_APP / "main.py")
        positions = {}
        for index, node in enumerate(tree.body):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            name = getattr(node.value.func, "attr", None) or getattr(
                node.value.func, "id", None
            )
            if name in ("configure_logging", "add_middleware", "register_rate_limiting"):
                positions.setdefault(name, index)
        assert "configure_logging" in positions
        assert positions["configure_logging"] < positions["add_middleware"]
        assert positions["configure_logging"] < positions["register_rate_limiting"]



class TestRequestThrottling:
    """V9, M14 and N2: both tiers bound request volume per client on every route."""

    def test_the_write_tier_refuses_past_its_threshold(self):
        client = TestClient(
            _build_application(default_limit="1000/minute", write_limit="2/minute"),
            raise_server_exceptions=False,
        )
        statuses = [client.post("/probe").status_code for _ in range(4)]
        assert statuses == [200, 200, 429, 429]

    def test_a_refusal_tells_the_client_when_to_retry(self):
        client = TestClient(
            _build_application(default_limit="1000/minute", write_limit="1/minute"),
            raise_server_exceptions=False,
        )
        client.post("/probe")
        response = client.post("/probe")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1

    def test_reads_are_not_metered_by_the_write_tier(self):
        client = TestClient(
            _build_application(default_limit="1000/minute", write_limit="1/minute"),
            raise_server_exceptions=False,
        )
        assert [client.get("/probe").status_code for _ in range(5)] == [200] * 5

    def test_the_global_ceiling_refuses_past_its_threshold(self):
        client = TestClient(
            _build_application(default_limit="3/minute", write_limit="1000/minute"),
            raise_server_exceptions=False,
        )
        assert [client.get("/probe").status_code for _ in range(5)] == [
            200,
            200,
            200,
            429,
            429,
        ]

    def test_the_ceiling_is_one_budget_across_different_endpoints(self):
        """M2: the ceiling was scoped per route, so each endpoint had its own budget.

        Three requests spread over three different routes must exhaust a 3-a-minute
        ceiling, leaving the fourth refused whichever route it addresses.
        """
        client = TestClient(
            _build_application(default_limit="3/minute", write_limit="1000/minute"),
            raise_server_exceptions=False,
        )
        assert client.get("/probe").status_code == 200
        assert client.get("/second-probe").status_code == 200
        assert client.post("/probe").status_code == 200
        assert client.get("/probe").status_code == 429
        assert client.get("/second-probe").status_code == 429
        assert client.post("/probe").status_code == 429

    def test_the_ceiling_is_one_budget_across_path_parameters(self):
        """M2: one route's differing path parameters received separate budgets."""
        client = TestClient(
            _build_application(default_limit="2/minute", write_limit="1000/minute"),
            raise_server_exceptions=False,
        )
        assert client.get("/parameterised/first").status_code == 200
        assert client.get("/parameterised/second").status_code == 200
        assert client.get("/parameterised/third").status_code == 429

    def test_a_path_matching_no_route_is_counted_too(self):
        """M2: an unmatched path was exempted, so path scanning was free.

        The ceiling runs before routing, so the two 404s below consume the budget and the
        third request is refused with 429 rather than answered with a 404.
        """
        client = TestClient(
            _build_application(default_limit="2/minute", write_limit="1000/minute"),
            raise_server_exceptions=False,
        )
        assert client.get("/no-such-path").status_code == 404
        assert client.get("/another-missing-path").status_code == 404
        assert client.get("/probe").status_code == 429

    def test_one_request_consumes_one_unit_of_each_budget(self):
        """A write must not spend two units of the ceiling by passing through both tiers."""
        client = TestClient(
            _build_application(default_limit="4/minute", write_limit="1000/minute"),
            raise_server_exceptions=False,
        )
        assert [client.post("/probe").status_code for _ in range(4)] == [200] * 4
        assert client.post("/probe").status_code == 429

    def test_an_exhausted_write_budget_leaves_reads_serving(self):
        """The two tiers hold separate counters, so a write refusal is not a read refusal."""
        client = TestClient(
            _build_application(default_limit="1000/minute", write_limit="1/minute"),
            raise_server_exceptions=False,
        )
        assert client.post("/probe").status_code == 200
        assert client.post("/probe").status_code == 429
        assert client.get("/probe").status_code == 200

    @pytest.mark.parametrize(
        "default_limit, write_limit, method",
        [("1000/minute", "1/minute", "post"), ("1/minute", "1000/minute", "get")],
    )
    def test_a_refusal_reaches_the_browser_with_its_cors_headers(
        self, default_limit, write_limit, method
    ):
        """M14: a 429 reaches the browser carrying its CORS headers."""
        client = TestClient(
            _build_application(default_limit=default_limit, write_limit=write_limit),
            raise_server_exceptions=False,
        )
        request = getattr(client, method)
        headers = {"Origin": "https://app.example.com"}
        request("/probe", headers=headers)
        response = request("/probe", headers=headers)
        assert response.status_code == 429
        assert response.headers["access-control-allow-origin"] == "https://app.example.com"
        for name in _expected_header_names():
            assert name in response.headers, name

    def test_an_exhausted_ceiling_replaces_the_preflight(self):
        """A preflight is metered, so an exhausted ceiling refuses it like any other request.

        This assertion is the reverse of what it used to be, and the reversal is the fix.
        ``CORSMiddleware`` used to wrap the tiers, so it answered every preflight itself
        without the ceiling ever seeing one - runtime verification measured eight consecutive
        preflights answered ``200`` against a ceiling of three a minute. Because
        ``Authorization`` is not CORS-safelisted, a browser sends a preflight per authenticated
        call, so that was an unmetered surface amounting to about half a browser client's
        requests.

        The 429 carries ``Retry-After``, the security headers and the echoed origin. What it
        cannot do is present itself to the page as a 429: a preflight answered with any
        non-2xx status is a CORS failure by specification, so the browser reports a failed
        request. Bounding the surface is the point; reporting it is not available.
        """
        client = TestClient(
            _build_application(default_limit="1/minute", write_limit="1000/minute"),
            raise_server_exceptions=False,
        )
        client.get("/probe")
        response = client.options(
            "/probe",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 429
        assert response.headers["retry-after"]
        assert response.headers["access-control-allow-origin"] == "https://app.example.com"
        for name in _expected_header_names():
            assert name in response.headers, name

    def test_a_preflight_within_the_ceiling_is_still_answered_by_the_cors_policy(self):
        """Metering a preflight must not stop it working: the allow-list still answers it."""
        client = TestClient(
            _build_application(default_limit="1000/minute", write_limit="1000/minute"),
            raise_server_exceptions=False,
        )
        response = client.options(
            "/probe",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://app.example.com"
        assert response.headers["access-control-allow-methods"] == "GET, POST, PUT, OPTIONS"

    def test_disabling_throttling_announces_itself(self, captured_logs):
        """N2: a deployment serving with no throttling records a warning naming the
        consequence, and records the state on the application."""
        handler = captured_logs("backend.app.core.rate_limit")
        app = _build_application(throttling_enabled=False)
        warnings = [
            record.getMessage()
            for record in handler.records
            if record.levelno == logging.WARNING
        ]
        assert any("DISABLED" in message for message in warnings), warnings
        assert app.state.rate_limit_enabled is False
        assert not hasattr(app.state, "rate_limit_tiers")

    def test_enabling_throttling_is_recorded_too(self):
        app = _build_application()
        assert app.state.rate_limit_enabled is True
        assert set(app.state.rate_limit_tiers) == {"ceiling", "write"}


class TestThrottlingStorageDegradation:
    """M3: a window-store failure admitted the request unmetered.

    The tier must keep enforcing on a process-local counter instead, so a broker outage
    weakens the guarantee from shared to per worker rather than removing it.
    """

    @pytest.fixture
    def tier_for(self):
        rate_limit = importlib.import_module("backend.app.core.rate_limit")

        def _build(storage, expression="2/minute"):
            windows = rate_limit.parse_many(expression)
            return rate_limit._FixedWindowTier("test-scope", windows, storage)

        return _build

    @staticmethod
    def _broken_storage():
        """A store that passes its start-up check and then fails every command.

        Built on the real in-memory store so it satisfies the ``limits`` strategy's type
        check, which is how a shared Redis store that becomes unreachable after start-up
        presents itself.
        """
        from limits.storage import MemoryStorage

        class _BrokenStorage(MemoryStorage):
            def incr(self, *args, **kwargs):
                raise RuntimeError("the window store stopped answering")

            def get(self, *args, **kwargs):
                raise RuntimeError("the window store stopped answering")

            def get_expiry(self, *args, **kwargs):
                raise RuntimeError("the window store stopped answering")

        return _BrokenStorage()

    def test_a_failing_store_still_enforces_the_window(self, tier_for, captured_logs):
        handler = captured_logs("backend.app.core.rate_limit")
        tier = tier_for(self._broken_storage())
        assert tier.exhausted_window("203.0.113.9") is None
        assert tier.exhausted_window("203.0.113.9") is None
        exhausted = tier.exhausted_window("203.0.113.9")
        assert exhausted is not None
        assert tier.degraded is True
        messages = " ".join(
            record.getMessage()
            for record in handler.records
            if record.levelno == logging.WARNING
        )
        assert "counting in process memory" in messages, messages

    def test_a_failing_store_is_never_a_free_pass(self, tier_for):
        """The very first request after the failure is counted, not waved through."""
        tier = tier_for(self._broken_storage(), expression="1/minute")
        assert tier.exhausted_window("203.0.113.9") is None
        assert tier.exhausted_window("203.0.113.9") is not None

    def test_the_degradation_is_reported_once_not_per_request(
        self, tier_for, captured_logs
    ):
        handler = captured_logs("backend.app.core.rate_limit")
        tier = tier_for(self._broken_storage(), expression="100/minute")
        for _ in range(5):
            tier.exhausted_window("203.0.113.9")
        degradations = [
            record
            for record in handler.records
            if "counting in process memory" in record.getMessage()
        ]
        assert len(degradations) == 1, degradations

    def test_a_healthy_store_is_not_reported_as_degraded(self, tier_for):
        from limits.storage import MemoryStorage

        tier = tier_for(MemoryStorage())
        assert tier.exhausted_window("203.0.113.9") is None
        assert tier.degraded is False

    def test_an_unreachable_store_falls_back_at_registration(self, captured_logs):
        """A store that cannot be built at start-up must not stop the API from serving."""
        rate_limit = importlib.import_module("backend.app.core.rate_limit")
        handler = captured_logs("backend.app.core.rate_limit")
        storage, uri = rate_limit.resolve_storage("redis://127.0.0.1:1/0")
        assert uri == "memory://"
        assert storage.check() is True
        messages = " ".join(record.getMessage() for record in handler.records)
        assert "counting per process" in messages, messages

    def test_an_empty_broker_url_counts_in_process(self):
        rate_limit = importlib.import_module("backend.app.core.rate_limit")
        _, uri = rate_limit.resolve_storage("")
        assert uri == "memory://"

    def test_concurrent_requests_share_one_replacement_counter(self, tier_for):
        """The substitution is documented as happening ONCE, and requests are served from a
        thread pool. Unserialised, two threads meeting the same store failure each built their
        own replacement counter and the last assignment won - so whatever the other had already
        counted was discarded, and the quota it had been enforcing silently reset.

        Sixteen threads are released together against a store that always fails; exactly one
        replacement may be constructed.
        """
        import threading

        rate_limit = importlib.import_module("backend.app.core.rate_limit")
        tier = tier_for(self._broken_storage(), expression="1000/minute")
        built = []
        original = rate_limit.FixedWindowRateLimiter

        def counting(storage):
            built.append(storage)
            return original(storage)

        rate_limit.FixedWindowRateLimiter = counting
        try:
            barrier = threading.Barrier(16)
            failures = []

            def worker():
                barrier.wait()
                try:
                    tier.exhausted_window("203.0.113.9")
                except Exception as exc:  # pragma: no cover - a regression would land here
                    failures.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            rate_limit.FixedWindowRateLimiter = original

        assert failures == []
        assert len(built) == 1, "%d replacement counters were constructed" % len(built)
        assert tier.degraded is True


class TestThrottlingIdentity:
    """The quota identity is the socket peer and cannot be chosen by the caller.

    A forwarded chain is evidence only of what the caller wrote, so honouring one lets a
    client reaching the API directly name any address it likes and rotate through
    addresses to escape its budget. The peer address cannot be forged that way.
    """

    @staticmethod
    def _scope(peer, forwarded=None):
        headers = []
        if forwarded is not None:
            headers.append((b"x-forwarded-for", forwarded.encode("latin-1")))
        return {"type": "http", "client": (peer, 51000), "headers": headers}

    @pytest.fixture
    def resolve(self):
        from backend.app.core.rate_limit import resolve_client_key

        return resolve_client_key

    def test_the_socket_peer_is_the_identity(self, resolve):
        assert resolve(self._scope("203.0.113.9")) == "203.0.113.9"

    @pytest.mark.parametrize(
        "forwarded",
        [
            "9.9.9.9",
            "1.2.3.4, 5.6.7.8",
            "1.1.1.1, 2.2.2.2, 3.3.3.3, 4.4.4.4",
            "not-an-address",
            "",
        ],
    )
    def test_no_forwarded_chain_can_choose_the_quota_key(self, resolve, forwarded):
        assert resolve(self._scope("203.0.113.9", forwarded)) == "203.0.113.9"

    def test_the_resolver_takes_the_scope_alone(self, resolve):
        """The withdrawn trusted-proxy list was its second parameter."""
        assert len(inspect.signature(resolve).parameters) == 1

    def test_a_request_with_no_address_shares_one_budget(self, resolve):
        assert resolve({"type": "http", "headers": []}) == "unknown"

    def test_an_empty_peer_address_shares_that_budget_too(self, resolve):
        assert resolve({"type": "http", "client": ("", 0), "headers": []}) == "unknown"

    # --- F-G: a peer the server took from a header is not an identity ------------------

    @staticmethod
    def _rewritten(address):
        """A scope shaped as uvicorn leaves one after rewriting the peer from the chain.

        Its ``ProxyHeadersMiddleware`` has no port to supply - a forwarded chain records
        addresses without them - so it writes zero, which no accepted connection carries.
        """
        return {
            "type": "http",
            "client": (address, 0),
            "headers": [(b"x-forwarded-for", address.encode("latin-1"))],
        }

    @pytest.mark.parametrize(
        "address",
        [
            "203.0.113.77",
            "not-an-ip",
            "A" * 200,
            "<script>alert(1)</script>",
            "../../etc/passwd",
            "1.2.3.4\tFORGED-LOG-LINE",
            "2001:db8::1",
        ],
    )
    def test_a_rewritten_peer_is_metered_as_one_client(self, resolve, address):
        """Whatever the header named, the request counts against the one shared bucket."""
        from backend.app.core.rate_limit import _FORWARDED_CLIENT_KEY

        assert resolve(self._rewritten(address)) == _FORWARDED_CLIENT_KEY

    def test_rotating_the_header_moves_between_no_buckets(self, resolve):
        """The measured bypass was eight requests each naming a different address."""
        keys = {resolve(self._rewritten("203.0.113.%d" % n)) for n in range(1, 9)}
        assert len(keys) == 1

    @pytest.mark.parametrize(
        "peer", ["not-an-ip", "A" * 200, "<script>alert(1)</script>", "../../etc/passwd"]
    )
    def test_a_peer_that_is_not_an_address_cannot_become_a_bucket(self, resolve, peer):
        """A value that is not an IP address is metered against the shared budget rather
        than filling the window store with a bucket of its own."""
        assert resolve({"type": "http", "client": (peer, 51000), "headers": []}) == "unknown"

    def test_one_address_is_one_bucket_however_it_is_spelled(self, resolve):
        """Textual variants of one IPv6 address must not each get their own budget."""
        spellings = ["2001:db8::1", "2001:0db8::0001", "2001:0DB8:0000:0000:0000:0000:0000:0001"]
        keys = {resolve({"type": "http", "client": (s, 51000), "headers": []}) for s in spellings}
        assert keys == {"2001:db8::1"}

    def test_rotation_through_the_real_rewriting_layer_is_refused(self):
        """End to end through uvicorn's own ``ProxyHeadersMiddleware``, which is enabled by
        default and is what made the header authoritative in the first place.

        Asserted against the real rewriting code rather than a hand-shaped scope, because the
        finding was that the application trusted whatever that code produced.
        """
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        served = ProxyHeadersMiddleware(
            _build_application(default_limit="3/minute", write_limit="1000/minute"),
            trusted_hosts="127.0.0.1",
        )
        client = TestClient(
            served, client=("127.0.0.1", 51000), raise_server_exceptions=False
        )
        statuses = [
            client.get(
                "/probe", headers={"X-Forwarded-For": "203.0.113.%d" % n}
            ).status_code
            for n in range(1, 6)
        ]
        assert statuses == [200, 200, 200, 429, 429]

    def test_the_transport_peer_is_still_metered_per_client(self):
        """The rewrite is what collapses buckets, not the mere presence of the header: with
        the peer preserved, two clients keep their own budgets."""
        served = _build_application(default_limit="2/minute", write_limit="1000/minute")
        first = TestClient(
            served, client=("203.0.113.10", 51000), raise_server_exceptions=False
        )
        second = TestClient(
            served, client=("203.0.113.11", 51000), raise_server_exceptions=False
        )
        assert [first.get("/probe").status_code for _ in range(3)] == [200, 200, 429]
        assert second.get("/probe").status_code == 200


class TestRequestBodyCeiling:
    """M14: a request body over the ceiling is refused with 413, both framings.

    Request-count throttling bounds how many requests a client may make; it does not bound
    the work one request may demand. The cell-update and share routes accept JSON lists with
    no declared maximum cardinality, so an unbounded body is a memory and database cost a
    single request can impose.

    Both framings are exercised because they take different paths through the middleware: a
    declared ``Content-Length`` over the ceiling is refused before the body is read at all,
    while a body that declares no length is bounded as the application reads it. The probe
    route reads its body, which is what the two list-accepting routes do.
    """

    @pytest.fixture
    def ceiling(self):
        from backend.app.core import rate_limit

        return rate_limit._MAX_REQUEST_BODY_BYTES

    @pytest.fixture
    def client(self):
        return TestClient(_build_application(), raise_server_exceptions=False)

    #: Bytes per chunk. Anything smaller than the ceiling needs several.
    CHUNK = 65536

    @classmethod
    def _chunked(cls, total):
        """Yield ``total`` bytes in chunks, which sends no ``Content-Length`` at all."""

        def _body():
            sent = 0
            while sent < total:
                size = min(cls.CHUNK, total - sent)
                yield b"y" * size
                sent += size

        return _body()

    def test_a_declared_oversize_body_is_refused(self, client, ceiling):
        response = client.post("/consuming", content=b"x" * (ceiling + 1))
        assert response.status_code == 413
        assert response.json() == {
            "error": "Request body exceeds the maximum of %d bytes" % ceiling
        }

    def test_an_undeclared_oversize_body_is_refused_while_it_streams(
        self, client, ceiling
    ):
        """No ``Content-Length`` is sent, so the declared-size check cannot refuse this one
        and the streamed byte count is what bounds it."""
        response = client.post("/consuming", content=self._chunked(ceiling + 65536))
        assert response.status_code == 413
        assert response.json() == {
            "error": "Request body exceeds the maximum of %d bytes" % ceiling
        }

    def test_a_body_within_the_ceiling_is_served(self, client):
        assert client.post("/consuming", content=b"z" * 4096).json() == {"read": 4096}

    def test_an_undeclared_body_within_the_ceiling_is_served(self, client):
        """The streaming path must pass a legitimate chunked body through untouched."""
        assert client.post("/consuming", content=self._chunked(4096)).json() == {
            "read": 4096
        }

    @pytest.mark.parametrize("framing", ["declared", "chunked"])
    def test_the_refusal_carries_the_canonical_security_headers(
        self, client, ceiling, framing
    ):
        """The limiter is registered inside the header wrapper, so its refusal is headed."""
        body = (
            b"x" * (ceiling + 1)
            if framing == "declared"
            else self._chunked(ceiling + 65536)
        )
        response = client.post("/consuming", content=body)
        assert response.status_code == 413
        for name in _expected_header_names():
            assert name in response.headers, name

    @pytest.mark.parametrize("framing", ["declared", "chunked"])
    def test_the_refusal_reaches_the_browser_with_its_cors_headers(
        self, client, ceiling, framing
    ):
        """Registered outside CORS, a 413 would reach the browser as an opaque cross-origin
        failure rather than as the status and reason it carries."""
        body = (
            b"x" * (ceiling + 1)
            if framing == "declared"
            else self._chunked(ceiling + 65536)
        )
        response = client.post(
            "/consuming", content=body, headers={"Origin": "https://app.example.com"}
        )
        assert response.status_code == 413
        assert (
            response.headers["access-control-allow-origin"] == "https://app.example.com"
        )

    def test_the_refusal_carries_no_bypass_marker(self, client, ceiling):
        """The marker records that an authentication decision was taken with verification
        relaxed. A body is refused before any authentication decision is reached, so there is
        no bypass to report - which is why this middleware is registered inside the marker."""
        security = importlib.import_module("backend.app.core.security")
        response = client.post("/consuming", content=b"x" * (ceiling + 1))
        assert response.status_code == 413
        assert security.AUTH_ENFORCEMENT_BYPASS_HEADER.lower() not in {
            name.lower() for name in response.headers
        }

    def test_a_declared_oversize_body_is_refused_before_the_application_is_entered(self):
        """A declared ``Content-Length`` over the ceiling is refused without the application
        being entered at all, so a body already known to be over it costs no routing, no
        dependency resolution and no read."""
        from backend.app.core.rate_limit import _RequestBodySizeLimitMiddleware

        entered = []
        app = FastAPI()

        @app.post("/counted")
        async def counted(request: Request):
            entered.append(True)
            return {"read": len(await request.body())}

        app.add_middleware(_RequestBodySizeLimitMiddleware, max_body_bytes=1024)
        client = TestClient(app, raise_server_exceptions=False)

        assert client.post("/counted", content=b"a" * 512).json() == {"read": 512}
        assert entered == [True], "the probe never recorded an entry"

        del entered[:]
        assert client.post("/counted", content=b"a" * 2048).status_code == 413
        assert entered == [], "the application was entered for a body declared over the ceiling"

    def test_the_refusal_does_not_depend_on_the_application_handling_the_disconnect(self):
        """The stream is cut by reporting a disconnect, which the application may raise on.

        In the entry point's stack the error boundary sits inside this middleware and answers
        that as a 500, which the refusal then replaces. Here nothing does, so the exception
        propagates into the middleware instead - and the refusal still reaches the client with
        the status naming the cause rather than an unexplained error.
        """
        from backend.app.core.rate_limit import _RequestBodySizeLimitMiddleware

        app = FastAPI()

        @app.post("/consuming")
        async def consuming(request: Request):
            return {"read": len(await request.body())}

        app.add_middleware(_RequestBodySizeLimitMiddleware, max_body_bytes=1024)
        client = TestClient(app, raise_server_exceptions=False)

        def small_chunks(total):
            sent = 0
            while sent < total:
                size = min(256, total - sent)
                yield b"a" * size
                sent += size

        refused = client.post("/consuming", content=small_chunks(4096))
        assert refused.status_code == 413
        assert refused.json() == {
            "error": "Request body exceeds the maximum of 1024 bytes"
        }
        assert client.post("/consuming", content=small_chunks(512)).json() == {
            "read": 512
        }

    def test_the_limiter_is_registered_with_the_module_ceiling(self):
        """The registration passes the named constant, so the middleware every request
        traverses is bounded by the delivered value rather than by its own default.

        It is also handed the origin allow-list, because the ceiling is registered inside
        ``CORSMiddleware`` and a 413 therefore never travels back out through it: without the
        allow-list a browser reports the refusal as an opaque cross-origin failure and the
        caller cannot tell an oversized body from an outage. Asserted as the exact keyword set
        so a third argument cannot arrive unnoticed.
        """
        app = _build_application()
        installed = [
            middleware
            for middleware in app.user_middleware
            if middleware.cls.__name__ == "_RequestBodySizeLimitMiddleware"
        ]
        assert len(installed) == 1, app.user_middleware
        from backend.app.core import rate_limit
        from backend.app.core.config import get_settings

        assert installed[0].kwargs == {
            "max_body_bytes": rate_limit._MAX_REQUEST_BODY_BYTES,
            "allowed_origins": frozenset(get_settings().ALLOWED_ORIGINS),
        }

    def test_a_narrower_ceiling_refuses_a_body_the_default_would_accept(self):
        """The maximum is a constructor argument, so a caller composing its own stack can
        narrow it - and this is what proves the refusal is metered against that argument
        rather than against the module constant."""
        from backend.app.core.rate_limit import _RequestBodySizeLimitMiddleware

        app = FastAPI()

        @app.post("/consuming")
        async def consuming(request: Request):
            return {"read": len(await request.body())}

        app.add_middleware(_RequestBodySizeLimitMiddleware, max_body_bytes=1024)
        client = TestClient(app, raise_server_exceptions=False)

        assert client.post("/consuming", content=b"a" * 512).json() == {"read": 512}
        refused = client.post("/consuming", content=b"a" * 2048)
        assert refused.status_code == 413
        assert refused.json() == {
            "error": "Request body exceeds the maximum of 1024 bytes"
        }

    def test_an_unparseable_declared_length_is_bounded_by_the_stream_instead(self):
        """A ``Content-Length`` that is not a non-negative integer is not trusted, so such a
        request is bounded by what it actually sends rather than by what it claims."""
        from backend.app.core.rate_limit import _declared_body_size

        for value in (b"not-a-number", b"-1", b""):
            assert (
                _declared_body_size(
                    {"type": "http", "headers": [(b"content-length", value)]}
                )
                is None
            ), value
        assert (
            _declared_body_size(
                {"type": "http", "headers": [(b"content-length", b"17")]}
            )
            == 17
        )


class TestMiddlewareRegistrationOrder:
    """M14 and the marker's registration contract, asserted against the entry point.

    The throttling tiers sit OUTSIDE ``CORSMiddleware``, which is what lets the ceiling meter
    a CORS preflight: CORS answers a preflight itself and never calls the application inside
    it, so a preflight registered the other way round was never counted at all. A response the
    tiers produce themselves therefore does not travel out through CORS, and carries the
    cross-origin headers ``rate_limit._cross_origin_headers`` supplies instead - asserted by
    :class:`TestThrottledResponsesRemainReadableCrossOrigin`.
    """

    def test_the_composed_stack_has_the_required_order(self):
        app = _build_application()
        assert [middleware.cls.__name__ for middleware in app.user_middleware] == [
            "SecurityHeadersMiddleware",
            "_RateLimitMiddleware",
            "_RequestBodySizeLimitMiddleware",
            "_ConcurrencyLimitMiddleware",
            "CORSMiddleware",
            "GZipMiddleware",
            "AuthEnforcementBypassMarkerMiddleware",
            "ServerErrorBoundaryMiddleware",
        ]

    def test_the_entry_point_registers_them_in_that_order(self):
        """The entry point registers the middleware in the order the stack requires."""
        tree = _parse(BACKEND_APP / "main.py")
        registrations = []
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
            if name == "add_middleware":
                registrations.append(call.args[0].id)
            elif name in ("register_rate_limiting", "configure_cors", "configure_logging"):
                registrations.append(name)
        assert registrations == [
            "configure_logging",
            "ServerErrorBoundaryMiddleware",
            "AuthEnforcementBypassMarkerMiddleware",
            "GZipMiddleware",
            "configure_cors",
            "register_rate_limiting",
            "SecurityHeadersMiddleware",
        ]

    def test_the_throttling_tiers_wrap_the_cross_origin_policy(self):
        """The ordering that makes a preflight countable, stated as its own requirement.

        ``CORSMiddleware`` short-circuits a preflight, so every tier inside it is bypassed for
        that request. This is the assertion that fails if the two are ever swapped back.
        """
        classes = [
            middleware.cls.__name__ for middleware in _build_application().user_middleware
        ]
        assert classes.index("_RateLimitMiddleware") < classes.index("CORSMiddleware")
        assert classes.index("_RequestBodySizeLimitMiddleware") < classes.index(
            "CORSMiddleware"
        )
        assert classes.index("SecurityHeadersMiddleware") < classes.index(
            "_RateLimitMiddleware"
        )

    def test_the_concurrency_bound_sits_inside_both_throttling_tiers(self):
        """A client already over its quota must be refused without occupying a slot."""
        classes = [
            middleware.cls.__name__ for middleware in _build_application().user_middleware
        ]
        assert classes.index("_RateLimitMiddleware") < classes.index(
            "_ConcurrencyLimitMiddleware"
        )
        assert classes.index("_ConcurrencyLimitMiddleware") < classes.index(
            "CORSMiddleware"
        )

    def test_compression_sits_inside_the_cross_origin_policy_and_the_tiers(self):
        """A response CORS or a tier produces itself must not be rewritten by compression."""
        classes = [
            middleware.cls.__name__ for middleware in _build_application().user_middleware
        ]
        assert classes.index("CORSMiddleware") < classes.index("GZipMiddleware")
        assert classes.index("_RateLimitMiddleware") < classes.index("GZipMiddleware")
        assert classes.index("SecurityHeadersMiddleware") < classes.index("GZipMiddleware")

    def test_compression_sits_outside_the_marker_and_the_error_boundary(self):
        """A marked response and an error-boundary response are compressed like any other."""
        classes = [
            middleware.cls.__name__ for middleware in _build_application().user_middleware
        ]
        assert classes.index("GZipMiddleware") < classes.index(
            "AuthEnforcementBypassMarkerMiddleware"
        )
        assert classes.index("GZipMiddleware") < classes.index(
            "ServerErrorBoundaryMiddleware"
        )

    def test_no_base_http_middleware_sits_between_the_marker_and_the_router(self):
        from starlette.middleware.base import BaseHTTPMiddleware

        app = _build_application()
        classes = [middleware.cls for middleware in app.user_middleware]
        marker_index = next(
            index
            for index, cls in enumerate(classes)
            if cls.__name__ == "AuthEnforcementBypassMarkerMiddleware"
        )
        for cls in classes[marker_index:]:
            assert not issubclass(cls, BaseHTTPMiddleware), cls

    def test_the_entry_point_configures_logging_before_it_registers_anything(self):
        """A registration that reports itself has to be able to reach the log first.

        ``register_rate_limiting`` emits the record naming the window store it counts in at
        registration time, so a logging bootstrap placed after it would rescue every later
        record and lose that one.
        """
        tree = _parse(BACKEND_APP / "main.py")
        positions = []
        for index, node in enumerate(tree.body):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            name = getattr(node.value.func, "attr", None) or getattr(
                node.value.func, "id", None
            )
            if name in ("configure_logging", "register_rate_limiting", "add_middleware"):
                positions.append((index, name))
        assert positions, "main.py registers nothing"
        assert positions[0][1] == "configure_logging", positions

    def test_the_marker_survives_the_error_boundary(self):
        security = importlib.import_module("backend.app.core.security")
        app = _build_application()

        async def bypassing_dependency():
            security._auth_enforcement_bypassed.set(True)
            return "anonymous"

        @app.get("/bypassed-then-failing")
        def bypassed_then_failing(caller=Depends(bypassing_dependency)):
            raise RuntimeError("failure after admission")

        response = TestClient(app, raise_server_exceptions=False).get(
            "/bypassed-then-failing"
        )
        assert response.status_code == 500
        assert response.headers[security.AUTH_ENFORCEMENT_BYPASS_HEADER] == "true"


class TestPreflightIsThrottled:
    """A CORS preflight is a metered request, not an exempt one.

    Runtime verification measured the previous behaviour exactly: with the ceiling set to
    three a minute, eight consecutive preflights from an allowed origin all answered ``200``
    and eight from a disallowed origin all answered ``400``, while the same eight ``OPTIONS``
    requests WITHOUT an ``Origin`` header - not preflights, so never short-circuited by
    ``CORSMiddleware`` - were metered normally. Because ``Authorization`` is not a
    CORS-safelisted request header, a browser sends a preflight for every authenticated call,
    so the exempt surface was roughly half of a browser client's requests.
    """

    PREFLIGHT = {
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "GET",
    }
    REFUSED_PREFLIGHT = {
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "GET",
    }

    def test_a_preflight_from_an_allowed_origin_is_counted(self):
        client = TestClient(
            _build_application(default_limit="3/minute"), raise_server_exceptions=False
        )
        codes = [
            client.options("/probe", headers=self.PREFLIGHT).status_code
            for _ in range(6)
        ]
        assert codes == [200, 200, 200, 429, 429, 429], codes

    def test_a_preflight_from_a_disallowed_origin_is_counted(self):
        """A refused preflight is still work the server did, so it still spends budget."""
        client = TestClient(
            _build_application(default_limit="3/minute"), raise_server_exceptions=False
        )
        codes = [
            client.options("/probe", headers=self.REFUSED_PREFLIGHT).status_code
            for _ in range(6)
        ]
        assert codes == [400, 400, 400, 429, 429, 429], codes

    def test_an_options_request_that_is_not_a_preflight_is_still_counted(self):
        """The control: this was already metered, and must remain so."""
        client = TestClient(
            _build_application(default_limit="3/minute"), raise_server_exceptions=False
        )
        codes = [client.options("/probe").status_code for _ in range(6)]
        assert codes == [405, 405, 405, 429, 429, 429], codes

    def test_a_throttled_preflight_still_carries_the_security_headers(self):
        client = TestClient(
            _build_application(default_limit="1/minute"), raise_server_exceptions=False
        )
        client.options("/probe", headers=self.PREFLIGHT)
        throttled = client.options("/probe", headers=self.PREFLIGHT)
        assert throttled.status_code == 429
        for name in _expected_header_names():
            assert name.lower() in throttled.headers, name
        assert throttled.headers["retry-after"]


class TestThrottledResponsesRemainReadableCrossOrigin:
    """A refusal the tiers produce is the one response CORS never sees, so it restates itself.

    The tiers wrap ``CORSMiddleware`` so that a preflight is counted, and the consequence is
    that a 429, 413 or 503 they generate does not travel back out through CORS. Without the
    subset restated here a browser reports the refusal as an opaque cross-origin failure and
    the page cannot tell a throttle from an outage.
    """

    ALLOWED = "https://app.example.com"

    def _throttled(self, origin=None):
        client = TestClient(
            _build_application(default_limit="1/minute"), raise_server_exceptions=False
        )
        headers = {"Origin": origin} if origin else {}
        assert client.get("/probe", headers=headers).status_code == 200
        response = client.get("/probe", headers=headers)
        assert response.status_code == 429
        return response

    def test_an_allowed_origin_is_echoed_on_the_refusal(self):
        response = self._throttled(self.ALLOWED)
        assert response.headers["access-control-allow-origin"] == self.ALLOWED
        assert response.headers["access-control-allow-credentials"] == "true"
        assert "Origin" in response.headers["vary"]

    def test_the_refusal_exposes_the_header_that_says_when_to_retry(self):
        """``Retry-After`` is not CORS-safelisted, so an unexposed one is invisible to a page.

        Measured through a real browser: with the header unexposed the client seam's
        ``retryAfterSeconds`` is always undefined for a cross-origin caller, so the throttling
        advice this API publishes could not be honoured and the user was told only that they
        were rate limited, never for how long.
        """
        from backend.app.core.rate_limit import RETRY_AFTER_HEADER

        response = self._throttled(self.ALLOWED)
        assert response.headers["retry-after"]
        assert (
            response.headers["access-control-expose-headers"].lower()
            == RETRY_AFTER_HEADER.lower()
        )

    def test_the_cross_origin_policy_exposes_the_same_header(self):
        """The entry point's expose_headers list and the refusal's must name the same header."""
        from backend.app.core.rate_limit import RETRY_AFTER_HEADER

        client = TestClient(_build_application(), raise_server_exceptions=False)
        response = client.get("/probe", headers={"Origin": self.ALLOWED})
        assert response.status_code == 200
        assert (
            response.headers["access-control-expose-headers"].lower()
            == RETRY_AFTER_HEADER.lower()
        )
        main = (BACKEND_APP / "main.py").read_text(encoding="utf-8")
        assert "expose_headers=[RETRY_AFTER_HEADER]" in main, (
            "main.py must expose the same named constant the refusal emits, so the two "
            "cannot drift apart"
        )

    def test_a_disallowed_origin_is_not_echoed_on_the_refusal(self):
        """The refusal must not become the one response that admits a refused origin."""
        response = self._throttled("https://evil.example")
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers
        # Still varied, so a shared cache cannot serve this answer to a different origin.
        assert response.headers["vary"] == "Origin"

    def test_a_same_origin_refusal_carries_no_cross_origin_headers(self):
        response = self._throttled()
        assert "access-control-allow-origin" not in response.headers
        assert "vary" not in response.headers

    def test_a_refused_body_size_is_readable_cross_origin(self):
        """The 413 is produced by the same tier stack and needs the same treatment."""
        from backend.app.core.rate_limit import _RequestBodySizeLimitMiddleware

        app = FastAPI()

        @app.post("/probe")
        def probe():
            return {"ok": True}

        app.add_middleware(
            _RequestBodySizeLimitMiddleware,
            max_body_bytes=8,
            allowed_origins=frozenset({self.ALLOWED}),
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/probe", content=b"x" * 64, headers={"Origin": self.ALLOWED}
        )
        assert response.status_code == 413
        assert response.headers["access-control-allow-origin"] == self.ALLOWED
        assert response.headers["access-control-allow-credentials"] == "true"
        assert "Origin" in response.headers["vary"]

    def test_the_echo_matches_the_list_the_cross_origin_policy_uses(self):
        """One allow-list, read once. Two copies of the rule could disagree."""
        from backend.app.core.config import get_settings
        from backend.app.core.rate_limit import _cross_origin_headers

        allowed = frozenset(get_settings().ALLOWED_ORIGINS)
        assert allowed, "the test environment declares no allowed origins"
        for origin in allowed:
            scope = {"type": "http", "headers": [(b"origin", origin.encode("latin-1"))]}
            assert _cross_origin_headers(scope, allowed)[
                "Access-Control-Allow-Origin"
            ] == origin
        refused = {
            "type": "http",
            "headers": [(b"origin", b"https://not-in-the-list.example")],
        }
        assert "Access-Control-Allow-Origin" not in _cross_origin_headers(refused, allowed)
        assert _cross_origin_headers({"type": "http", "headers": []}, allowed) == {}


class TestConcurrencyIsBounded:
    """In-flight requests are bounded and the excess is shed, not queued into a 500.

    Composed directly on the middleware with a small bound and a short wait, because the shipped
    values - twenty slots and a five-second wait - are chosen so that this never fires under any
    load a test could generate in-process.
    """

    @staticmethod
    def _application(max_concurrent_requests, wait_seconds, hold_seconds=0.0):
        from backend.app.core.rate_limit import _ConcurrencyLimitMiddleware

        app = FastAPI()

        @app.get("/slow")
        async def slow():
            if hold_seconds:
                await asyncio.sleep(hold_seconds)
            return {"ok": True}

        app.add_middleware(
            _ConcurrencyLimitMiddleware,
            max_concurrent_requests=max_concurrent_requests,
            wait_seconds=wait_seconds,
            allowed_origins=frozenset({"https://app.example.com"}),
        )
        return app

    def test_requests_within_the_bound_are_served(self):
        client = TestClient(
            self._application(2, 5.0), raise_server_exceptions=False
        )
        assert [client.get("/slow").status_code for _ in range(6)] == [200] * 6

    def test_the_excess_is_shed_with_503_and_retry_after(self):
        """Held requests fill the bound; the next one is refused rather than made to wait."""
        import httpx

        app = self._application(2, 0.2, hold_seconds=1.5)

        async def drive():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                responses = await asyncio.gather(
                    *[client.get("/slow") for _ in range(5)]
                )
                return responses

        responses = asyncio.new_event_loop().run_until_complete(drive())
        statuses = sorted(response.status_code for response in responses)
        assert statuses.count(200) == 2, statuses
        assert statuses.count(503) == 3, statuses
        shed = [response for response in responses if response.status_code == 503][0]
        assert shed.headers["retry-after"] == "1"
        assert "at capacity" in shed.json()["error"]

    def test_a_shed_request_is_readable_cross_origin(self):
        import httpx

        app = self._application(1, 0.2, hold_seconds=1.5)

        async def drive():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await asyncio.gather(
                    *[
                        client.get(
                            "/slow", headers={"Origin": "https://app.example.com"}
                        )
                        for _ in range(3)
                    ]
                )

        responses = asyncio.new_event_loop().run_until_complete(drive())
        shed = [response for response in responses if response.status_code == 503]
        assert shed, [response.status_code for response in responses]
        assert (
            shed[0].headers["access-control-allow-origin"] == "https://app.example.com"
        )
        assert "Origin" in shed[0].headers["vary"]

    def test_a_slot_is_released_when_the_application_raises(self):
        """A leaked slot would shrink capacity permanently, one error at a time."""
        from backend.app.core.rate_limit import _ConcurrencyLimitMiddleware

        app = FastAPI()

        @app.get("/failing")
        def failing():
            raise RuntimeError("a deliberately unhandled failure")

        app.add_middleware(
            _ConcurrencyLimitMiddleware, max_concurrent_requests=1, wait_seconds=0.2
        )
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(4):
            assert client.get("/failing").status_code == 500
        # The slot survived four failures, so a request that needs it still gets it.
        assert client.get("/failing").status_code == 500

    def test_the_shipped_bound_is_the_pool_capacity(self):
        from backend.app.core.rate_limit import _ConcurrencyLimitMiddleware
        from backend.app.db.database import DB_POOL_CAPACITY

        middleware = _ConcurrencyLimitMiddleware(app=None)
        assert middleware.max_concurrent_requests == DB_POOL_CAPACITY


class TestApplicationLogging:
    """The application's own records reach the log under a bare ASGI server.

    Runtime verification measured the previous behaviour: the root logger sat at level 30
    with no handlers, so the record naming the window store the throttling tiers count in was
    discarded, and an operator could not tell a deployment enforcing the configured quota
    from one enforcing that quota multiplied by the worker count.
    """

    @staticmethod
    @contextlib.contextmanager
    def bare_logging_tree():
        """Present the logging tree as a bare uvicorn leaves it, and restore it afterwards.

        A context manager rather than a fixture, deliberately. pytest's logging plugin adds a
        handler to the ROOT logger at the start of every test phase, so a fixture that cleared
        root during setup would find it re-added for the call phase - and the tree would look
        configured, which is a state a separate test asserts on purpose.

        The root LEVEL is reset as well as its handlers. A bare uvicorn leaves the root logger
        at Python's default of ``WARNING``, and the bootstrap now sets that level, so a run
        that has already called it would otherwise leave the root at ``INFO`` and this would
        present a configured tree while claiming to present a bare one.
        """
        application = logging.getLogger("backend")
        root = logging.getLogger()
        saved_handlers = list(application.handlers)
        saved_level = application.level
        saved_propagate = application.propagate
        saved_root_handlers = list(root.handlers)
        saved_root_level = root.level
        application.handlers = []
        application.setLevel(logging.NOTSET)
        root.handlers = []
        root.setLevel(logging.WARNING)
        try:
            yield application
        finally:
            application.handlers = saved_handlers
            application.setLevel(saved_level)
            application.propagate = saved_propagate
            root.handlers = saved_root_handlers
            root.setLevel(saved_root_level)

    @staticmethod
    def _managed_handlers():
        """The handlers the bootstrap owns, identified by the tag it sets on them.

        The single handler is installed on the ROOT logger rather than on the application
        logger, so one formatter serves the application tree and the standard library alike
        and an application record is formatted once and emitted once. Asserting on the tag
        rather than on ``application.handlers`` is what makes the assertion about the property
        that matters - exactly one handler serves the tree - rather than about where it sits.
        """
        return [
            handler
            for handler in logging.getLogger().handlers
            if getattr(handler, "_excel_clone_managed", False)
        ]

    def test_it_admits_info_records_and_attaches_one_handler(self):
        from backend.app.core.logging_config import (
            configure_logging,
            has_effective_handler,
        )

        with self.bare_logging_tree() as application:
            configured = configure_logging()
            assert configured is application
            assert configured.level == logging.INFO
            assert configured.isEnabledFor(logging.INFO)
            managed = self._managed_handlers()
            assert len(managed) == 1, logging.getLogger().handlers
            assert isinstance(managed[0], logging.StreamHandler)
            # The record now reaches that handler, which is the whole point of the bootstrap.
            assert has_effective_handler(application) is True

    def test_it_is_idempotent(self):
        from backend.app.core.logging_config import configure_logging

        with self.bare_logging_tree():
            configure_logging()
            configure_logging()
            assert len(self._managed_handlers()) == 1

    def test_it_leaves_an_operator_configuration_alone(self):
        """A deployment passing --log-config keeps every handler it configured.

        The bootstrap adds its own beside them rather than instead of them, and adds none to
        the application logger. That it adds one at all is deliberate and is the reconciled
        contract: the escaping formatter is a security control, and an operator's handler does
        not escape control characters, so declining to install ours whenever one already
        existed would silently withdraw the log-forging defence (CWE-117) on exactly the
        deployments that configure logging most carefully. The cost is that such a deployment
        sees the record twice; the guarantee bought is that one of the two cannot be forged.
        """
        from backend.app.core.logging_config import configure_logging

        with self.bare_logging_tree() as application:
            operator_handler = logging.NullHandler()
            logging.getLogger().addHandler(operator_handler)
            configure_logging()
            assert application.handlers == []
            assert operator_handler in logging.getLogger().handlers
            assert len(self._managed_handlers()) == 1
            # The level still moves, because a dropped record has two causes and an inherited
            # WARNING is Python's default rather than the operator's choice.
            assert application.isEnabledFor(logging.INFO)

    def test_it_does_not_lower_a_level_somebody_chose(self):
        from backend.app.core.logging_config import configure_logging

        with self.bare_logging_tree() as application:
            application.setLevel(logging.ERROR)
            configure_logging()
            assert application.level == logging.ERROR

    def test_a_record_emitted_below_warning_is_dropped_without_the_bootstrap(self):
        """The measured pre-fix state, pinned so the bootstrap cannot become a no-op.

        With the tree as uvicorn leaves it, the application logger inherits the root logger's
        default level of WARNING and reaches no handler at all, which is why the start-up
        confirmations disappeared while the warnings survived through ``logging.lastResort``.
        """
        from backend.app.core.logging_config import has_effective_handler

        with self.bare_logging_tree() as application:
            assert application.getEffectiveLevel() == logging.WARNING
            assert not application.isEnabledFor(logging.INFO)
            assert has_effective_handler(application) is False

    def test_the_throttling_confirmation_reaches_the_application_logger(
        self, captured_logs, set_settings
    ):
        """The record this exists to rescue, asserted end to end.

        Registration is driven for real and the record is read off the application logger, so
        this fails both if the record stops being emitted and if the tree stops admitting it.
        """
        from backend.app.core.logging_config import configure_logging
        from backend.app.core.rate_limit import register_rate_limiting

        with self.bare_logging_tree():
            configure_logging()
            handler = captured_logs("backend.app.core.rate_limit")
            set_settings(
                rate_limit_enabled="true",
                rate_limit_default="600/minute",
                rate_limit_write="300/minute",
            )
            assert logging.getLogger("backend.app.core.rate_limit").isEnabledFor(
                logging.INFO
            )
            register_rate_limiting(FastAPI())
            messages = [record.getMessage() for record in handler.records]
        confirmations = [
            message for message in messages if "Request throttling enabled" in message
        ]
        assert confirmations, messages
        # The store is the operative fact: memory:// means the quota is per worker.
        assert "memory://" in confirmations[0], confirmations


# ===========================================================================
# V10 / V11 and the static delivery points - shape assertions on committed artifacts
# ===========================================================================
class TestFirestoreRules:
    """V10: the committed rules govern the browser's direct path to Firestore."""

    @pytest.fixture
    def rules(self):
        return (REPOSITORY_ROOT / "firestore.rules").read_text(encoding="utf-8")

    @staticmethod
    def _allow_conditions(rules):
        """Return {operation: condition} for every ``allow`` statement in the rules.

        Comment lines are dropped first, so prose that happens to name an operation cannot
        be mistaken for a rule, and each statement is joined into one line because the
        conditions span several.
        """
        import re

        code = "\n".join(
            line for line in rules.splitlines() if not line.strip().startswith("//")
        )
        conditions = {}
        for match in re.finditer(
            r"allow\s+([a-z,\s]+?):\s*if\s(.*?);", code, re.DOTALL
        ):
            operations = [part.strip() for part in match.group(1).split(",")]
            condition = " ".join(match.group(2).split())
            for operation in operations:
                conditions[operation] = condition
        return conditions

    def test_the_rules_file_is_deployed_by_the_manifest(self):
        import json

        manifest = json.loads(
            (REPOSITORY_ROOT / "firebase.json").read_text(encoding="utf-8")
        )
        assert manifest["firestore"]["rules"] == "firestore.rules"

    def test_the_rules_require_an_authenticated_caller(self, rules):
        assert "rules_version = '2'" in rules
        assert "request.auth" in rules
        assert "request.auth.uid" in rules

    def test_the_rules_admit_no_signed_in_caller_by_itself(self, rules):
        """A signed-in-is-enough rule is the exposure the rules exist to close."""
        assert "allow read, write: if request.auth != null;" not in rules

    def test_destroying_a_workbook_is_owner_only(self, rules):
        """M2: a collaborator was admitted to delete, against the file's own contract.

        The authority table at the head of the rules states the collaborator's authority as
        read and a content-confined update. Admitting it here gave every shared user the
        power to destroy work it does not own, irreversibly. Asserted on the committed rules
        text so the expansion cannot return without this failing, because the emulator check
        that would otherwise catch it needs a running emulator.
        """
        delete_rules = [
            line.strip()
            for line in rules.splitlines()
            if line.strip().startswith("allow delete")
        ]
        assert delete_rules == ["allow delete: if isOwner();"], delete_rules

    def test_the_delete_rule_agrees_with_the_emulator_expectation(self, rules):
        """The two artifacts encode one authority, so they are pinned to each other.

        ``backend/tests/firestore_rules_emulator_check.py`` expects a collaborator delete to
        be DENIED. That expectation and the rule are the same decision written twice, and a
        divergence between them is what M2 was.
        """
        emulator_check = (
            REPOSITORY_ROOT / "backend" / "tests" / "firestore_rules_emulator_check.py"
        ).read_text(encoding="utf-8")
        assert '"collaborator delete",' in emulator_check
        collaborator_delete = emulator_check.split('"collaborator delete",', 1)[1]
        assert collaborator_delete.lstrip().startswith("DENIED"), (
            "the emulator check no longer expects a collaborator delete to be denied"
        )
        assert "isCollaborator()" not in rules.split("allow delete")[1].split("\n")[0]
    def test_every_operation_carries_a_condition(self, rules):
        """Exactly the four operations, each gated. An ungated ``allow`` would be a hole."""
        assert set(self._allow_conditions(rules)) == {
            "read",
            "create",
            "update",
            "delete",
        }

    def test_delete_is_owner_only(self, rules):
        """C1: the delete rule admitted ``isCollaborator()``, so any listed collaborator
        could destroy another user's workbook irreversibly.

        This is the gate for that regression, and it runs in CI with the rest of this suite.
        ``backend/tests/firestore_rules_emulator_check.py`` asserts the same semantics against
        the real rules engine, which is the authority; this assertion is what makes a
        re-admission fail without an emulator being available.
        """
        condition = self._allow_conditions(rules)["delete"]
        assert condition == "isOwner()", condition
        assert "isCollaborator" not in condition

    def test_reads_and_content_updates_still_reach_collaborators(self, rules):
        """The delete fix must not narrow the authority a collaborator is meant to have."""
        conditions = self._allow_conditions(rules)
        assert "isCollaborator()" in conditions["read"]
        assert "isCollaboratorUpdate()" in conditions["update"]

    def test_the_documented_authority_model_matches_the_rules(self, rules):
        """The comment block states the authority by caller, and it is what an operator
        reads. It said collaborators may not delete while the rule allowed it."""
        assert "collaborator read, and update confined to the content fields" in rules
        assert "only the owner may destroy a workbook" in rules

    def test_the_emulator_check_asserts_the_same_delete_semantics(self):
        """The acceptance artifact already expected owner-only delete, so the rules were
        contradicting their own verification. Pinned here so the two cannot drift again."""
        check = (
            REPOSITORY_ROOT / "backend" / "tests" / "firestore_rules_emulator_check.py"
        ).read_text(encoding="utf-8")
        collaborator_delete = check.index('"collaborator delete"')
        owner_delete = check.index('"owner delete"')
        assert "DENIED" in check[collaborator_delete:collaborator_delete + 120]
        assert "ALLOWED" in check[owner_delete:owner_delete + 120]


class TestStaticDeliveryPolicies:
    """C1 and V7: the document policy must not be able to block the configured API."""

    @staticmethod
    def _directives(policy):
        parsed = {}
        for part in policy.split(";"):
            tokens = part.split()
            if tokens:
                parsed[tokens[0]] = tokens[1:]
        return parsed

    @pytest.fixture
    def document_policy(self):
        import re

        html = (
            REPOSITORY_ROOT / "frontend" / "public" / "index.html"
        ).read_text(encoding="utf-8")
        match = re.search(
            r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html
        )
        assert match, "no Content-Security-Policy meta element found"
        return self._directives(match.group(1))

    def test_the_document_policy_does_not_restrict_connections(self, document_policy):
        assert "connect-src" not in document_policy
        assert "default-src" not in document_policy

    def test_the_document_policy_still_restricts_loading(self, document_policy):
        for directive in (
            "base-uri",
            "object-src",
            "form-action",
            "script-src",
            "style-src",
            "img-src",
            "font-src",
        ):
            assert directive in document_policy, directive
        assert document_policy["script-src"] == ["'self'"]
        assert document_policy["object-src"] == ["'none'"]

    def test_the_document_policy_carries_nothing_a_rollout_cannot_relax(
        self, document_policy
    ):
        """H3: a meta element has no report-only form, so this policy is always enforced and
        ``csp_report_only`` does not reach it.

        ``upgrade-insecure-requests`` therefore belongs in the response header alone: carried
        here it keeps rewriting insecure subresource requests during a report-only rollout, so
        the violations enforcement would produce never appear in the reports the rollout exists
        to collect.
        """
        assert "upgrade-insecure-requests" not in document_policy

    def test_the_served_policies_still_upgrade_insecure_requests(self):
        """The directive is not lost - it moves to where the rollout mode governs it."""
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        terraform = (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")
        assert "upgrade-insecure-requests" in CONTENT_SECURITY_POLICY
        assert "upgrade-insecure-requests" in nginx
        assert "upgrade-insecure-requests" in terraform

    @pytest.mark.parametrize(
        "artifact",
        [
            ("infrastructure", "docker", "Dockerfile.frontend"),
            ("scripts", "deploy.sh"),
        ],
    )
    def test_every_build_path_disables_the_inlined_runtime(self, artifact):
        """H3: ``script-src 'self'`` carries no nonce and no hash, and Create React App
        inlines its webpack runtime into the document by default - so a browser enforcing the
        policy refuses that runtime and the application does not start at all. Both paths that
        compile the application must disable the inlining."""
        source = (REPOSITORY_ROOT.joinpath(*artifact)).read_text(encoding="utf-8")
        assert "INLINE_RUNTIME_CHUNK=false" in source, artifact

    @pytest.mark.parametrize(
        "artifact",
        [
            ("infrastructure", "docker", "Dockerfile.frontend"),
            ("scripts", "deploy.sh"),
        ],
    )
    def test_no_build_path_compiles_the_application_more_than_once(self, artifact):
        """A substring check cannot see a SECOND build. A duplicated block spliced a further
        ``npm run build`` into scripts/deploy.sh, WITHOUT the variable, after the guarded one -
        and because react-scripts empties build/ first, that second build discarded the
        CSP-safe artifact and the published document carried the inlined runtime. Every
        assertion in this file passed while the deployed application could not start.

        The count is what catches it: every compile of this application must be a guarded
        compile, so the number of guarded builds must equal the number of builds.
        """
        source = (REPOSITORY_ROOT.joinpath(*artifact)).read_text(encoding="utf-8")
        invocations = [
            line.strip()
            for line in source.splitlines()
            if "npm run build" in line and not line.lstrip().startswith("#")
        ]
        assert len(invocations) == 1, "%s compiles the application %d times: %s" % (
            artifact,
            len(invocations),
            invocations,
        )
        # The one invocation must itself be guarded. In the Dockerfile the variable is an ENV
        # instruction above the RUN, so the guard is accepted either on the line or in the file;
        # what may not happen is a build this file does not also guard.
        assert "INLINE_RUNTIME_CHUNK=false" in invocations[0] or (
            "ENV INLINE_RUNTIME_CHUNK=false" in source
        ), invocations[0]

    def test_the_document_policy_admits_no_inline_script(self, document_policy):
        """The other half of the same contract: with the runtime emitted as a file, the policy
        must not be relaxed to admit inline script."""
        assert "'unsafe-inline'" not in document_policy["script-src"]
        assert "'unsafe-eval'" not in document_policy["script-src"]

    #: The one directive whose meta source list may differ from the served one, and the one
    #: extra source it may carry. A meta element cannot take a nonce or a report-only mode, and
    #: the development server sends no response header, so this is the only instrument that
    #: reaches the path where Create React App injects the application's own stylesheet as a
    #: ``<style>`` element. Both delivered paths accompany the document with a response header
    #: that still says ``'self'``, and a browser enforces every policy it receives - see D99.
    PERMITTED_DOCUMENT_DIVERGENCE = ("style-src-elem", "'unsafe-inline'")

    def test_the_document_policy_never_narrows_the_served_policy(self, document_policy):
        """Every directive the document names must also be served, and must admit at least
        what the served copy admits - otherwise the intersection of the two is narrower than
        the policy of record and the document silently becomes the effective one."""
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        served = self._directives(CONTENT_SECURITY_POLICY)
        for directive, sources in document_policy.items():
            assert directive in served, directive
            assert set(served[directive]) <= set(sources), directive

    def test_the_document_policy_diverges_in_exactly_one_place(self, document_policy):
        """The divergence D99 permits is bounded here, so a second relaxation cannot arrive
        quietly under the first one's justification."""
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        served = self._directives(CONTENT_SECURITY_POLICY)
        directive, extra_source = self.PERMITTED_DOCUMENT_DIVERGENCE
        divergent = {
            name: (served[name], sources)
            for name, sources in document_policy.items()
            if set(sources) != set(served[name])
        }
        assert list(divergent) == [directive], divergent
        assert set(document_policy[directive]) - set(served[directive]) == {
            extra_source
        }, divergent

    def test_the_served_policies_still_refuse_an_inline_style_element(self):
        """The other half of D99: production is unchanged. All THREE header producers keep
        ``style-src-elem 'self'``, so the response header that accompanies the document in
        every delivered path still blocks an injected ``<style>``."""
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        terraform = (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")
        assert (
            self._directives(CONTENT_SECURITY_POLICY)["style-src-elem"] == ["'self'"]
        )
        for name, producer in (("nginx.conf", nginx), ("main.tf", terraform)):
            assert "style-src-elem 'self'" in producer, name
            assert "style-src-elem 'self' 'unsafe-inline'" not in producer, name

    def test_the_document_carries_no_script_element_of_its_own(self):
        """D101: the template named ``%PUBLIC_URL%/bundle.js``, which no build emits. Every
        load fetched it, received this document through the history fallback and parsed HTML
        as JavaScript - a ``SyntaxError`` on every page view, in the one console a reviewer
        reads to judge whether the policy is working. react-scripts injects its own hashed
        script tags, so the element was never needed."""
        html = (
            REPOSITORY_ROOT / "frontend" / "public" / "index.html"
        ).read_text(encoding="utf-8")
        assert "<script" not in html
        assert "bundle.js" not in html

    def test_the_container_refuses_to_start_with_no_api_origin(self):
        """D102: the two static delivery paths must fail the same way on the same mistake.

        Terraform rejects an empty ``api_origin`` at plan time. The container had no
        equivalent, so an image run without the override started happily and served a policy
        admitting no API origin - and a browser enforcing it blocks every API call the
        application makes. The guard is asserted here rather than merely present because the
        thing that makes it work is its NUMERIC PREFIX: the image's entrypoint runs
        ``/docker-entrypoint.d/*.sh`` in ``sort -V`` order under ``set -e``, so a prefix above
        the envsubst step's would render the broken policy before refusing it.
        """
        dockerfile = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "Dockerfile.frontend"
        ).read_text(encoding="utf-8")
        variables = (TERRAFORM / "variables.tf").read_text(encoding="utf-8")

        guard = re.search(
            r"/docker-entrypoint\.d/(\d+)-([A-Za-z0-9-]+)\.sh", dockerfile
        )
        assert guard, "no /docker-entrypoint.d guard is installed"
        assert int(guard.group(1)) < 20, (
            "the guard runs at %s, after the image's own 20-envsubst-on-templates.sh, so the "
            "policy would be rendered before the value was checked" % guard.group(1)
        )
        assert "chmod +x /docker-entrypoint.d/%s-%s.sh" % guard.groups() in dockerfile, (
            "the image entrypoint skips a script that is not executable, so an unmarked guard "
            "is a guard that never runs"
        )
        # Empty is refused, and so is a value missing the leading space it is concatenated with.
        assert '-z "${CSP_CONNECT_SRC_API:-}"' in dockerfile
        assert '\'  " "*) ;;\'' in dockerfile
        assert dockerfile.count("exit 1") == 2, (
            "both refusals must exit non-zero: the entrypoint runs these under set -e, which "
            "is the whole mechanism that turns the check into a refusal to start"
        )
        # Parity with the edge: the same mistake is already refused on the other static path.
        # api_origin carries no default, so it must be supplied, and its validation matches a
        # full scheme://host origin - which an empty string cannot satisfy.
        block = variables.split('variable "api_origin" {')[1].split("\nvariable ")[0]
        assert "default" not in block, "api_origin acquired a default, so it can go unset"
        assert 'can(regex("^https?://' in block

    def test_the_document_discloses_no_repository_path(self):
        """D100: this file is downloaded by every visitor, so a comment naming the Terraform
        and Nginx sources handed out internal layout for free. The threat marker stays; the
        contract text moved to the decision log.

        D109 widened this: the original check asserted five repository-path fragments and
        nothing else, so two comments that broke D100's rule in a different way survived it -
        one citing three decision identifiers, one naming the ``domain_name`` Terraform
        variable. A decision identifier tells a reader which internal record to ask for and
        how it is organised; a variable name tells them how the deployment is parameterised.
        Neither is a threat statement, and both are published to every visitor.
        """
        html = (
            REPOSITORY_ROOT / "frontend" / "public" / "index.html"
        ).read_text(encoding="utf-8")
        for path in ("infrastructure/", "backend/", "frontend/src", ".tf", ".conf"):
            assert path not in html, path
        # No decision, review or follow-up identifier: D9, R48, F42 and the like.
        identifiers = re.findall(r"\b(?:D|R|F|DEV-)\d+\b", html)
        assert identifiers == [], identifiers
        # No configuration input named by the artifacts that render the served policy.
        for variable in (
            "domain_name",
            "api_origin",
            "signer_service_account",
            "project_id",
            "CSP_CONNECT_SRC_API",
        ):
            assert variable not in html, variable
        assert "SECURITY:" in html

    def test_the_document_still_sets_a_referrer_policy(self):
        html = (
            REPOSITORY_ROOT / "frontend" / "public" / "index.html"
        ).read_text(encoding="utf-8")
        assert 'name="referrer" content="strict-origin-when-cross-origin"' in html

    def test_the_served_policies_render_the_configured_api_origin(self):
        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        terraform = (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")
        assert "${CSP_CONNECT_SRC_API}" in nginx
        assert "var.api_origin" in terraform

    def test_the_container_policy_is_the_api_policy_plus_the_api_origin(self):
        """M9: the three producers were described as rendering one equal policy.

        They do not. The container and edge policies take an API origin the API middleware
        has no input for. What must hold - and is asserted here - is that with no API origin
        configured the container renders the API policy byte for byte, so the difference
        between the producers is exactly that one appended source and nothing else.
        """
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        rendered_with_no_api_origin = nginx.replace("${CSP_CONNECT_SRC_API}", "")
        assert CONTENT_SECURITY_POLICY in rendered_with_no_api_origin

    def test_the_api_middleware_takes_no_api_origin_input(self):
        """M9: the API middleware was documented as rendering from an API-origin input."""
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        source = (BACKEND_APP / "core" / "security_headers.py").read_text(
            encoding="utf-8"
        )
        assert "CSP_CONNECT_SRC_API" not in source
        # The policy is a constant: nothing in it varies with a deployment's API origin.
        assert "${" not in CONTENT_SECURITY_POLICY
        assert "connect-src 'self'" in CONTENT_SECURITY_POLICY

    @pytest.mark.parametrize(
        "endpoint",
        [
            "identitytoolkit.googleapis.com",
            "securetoken.googleapis.com",
            "firestore.googleapis.com",
            "firebaseinstallations.googleapis.com",
        ],
    )
    def test_the_served_policies_still_admit_firebase(self, endpoint):
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        terraform = (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")
        assert endpoint in CONTENT_SECURITY_POLICY
        assert endpoint in nginx
        assert endpoint in terraform

    def test_the_nginx_configuration_keeps_the_single_page_fallback(self):
        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        assert "try_files $uri $uri/ /index.html" in nginx

    def test_the_container_does_not_advertise_its_server_version(self):
        """The response headers this file pins are all additive, so none of them removes what
        nginx volunteers by default: a ``Server: nginx/<version>`` on every response, including
        every error page. That hands an unauthenticated caller the exact patch level to look up
        against an advisory feed, which is reconnaissance the deployment gains nothing from.
        """
        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        directives = [
            line.strip()
            for line in nginx.splitlines()
            if line.strip().startswith("server_tokens")
        ]
        assert directives == ["server_tokens off;"], directives

    def test_no_redirect_names_a_scheme_the_edge_does_not_use(self):
        """nginx defaults ``absolute_redirect`` to on, and its directory-normalisation redirect
        answered ``/static`` with ``Location: http://<host>/static/`` -- measured on a running
        instance, not inferred. This server terminates no TLS; it sits behind the load balancer
        that does and sees plain HTTP internally. So the scheme it bakes into that Location is
        ``http``, which downgrades a client that arrived over ``https`` and discloses the internal
        host derived from the Host header -- contradicting both the HTTPS edge and the
        ``Strict-Transport-Security`` header this same file emits. A path-only Location leaves the
        client resolving against the scheme and host it actually used.
        """
        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        directives = [
            line.strip()
            for line in nginx.splitlines()
            if line.strip().startswith("absolute_redirect")
        ]
        assert directives == ["absolute_redirect off;"], directives

    def test_the_container_base_image_cannot_move_underneath_a_rebuild(self):
        """A floating ``nginx:alpine`` tag makes the image that carries these headers
        irreproducible: two builds of the same commit can ship different nginx binaries, and a
        tag republished upstream changes the deployed edge with no change here to review. The
        digest is the only reference that cannot move, so the tag may stay for readability but
        a digest must accompany it.
        """
        dockerfile = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "Dockerfile.frontend"
        ).read_text(encoding="utf-8")
        serving_stages = [
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip().startswith("FROM ") and "nginx" in line
        ]
        assert serving_stages, "no nginx serving stage found"
        for stage in serving_stages:
            assert "@sha256:" in stage, stage
            # A 64-character hex digest, so a truncated or placeholder value is refused too.
            digest = stage.split("@sha256:", 1)[1].split()[0]
            assert len(digest) == 64, stage
            assert all(character in "0123456789abcdef" for character in digest), stage


class TestPublishedArtifactScope:
    """What the deployment publishes, and what the edge does with a path that names no object.

    The header set and the build guard were both correct while the publish step shipped the
    original TypeScript beside every bundle and the edge answered a missing asset with the entry
    document under status 200. Both are properties of the publishing pipeline rather than of any
    header, and both are asserted here on the committed configuration.
    """

    @pytest.fixture
    def deploy(self):
        return (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    @pytest.fixture
    def dockerfile(self):
        return (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "Dockerfile.frontend"
        ).read_text(encoding="utf-8")

    @pytest.fixture
    def terraform(self):
        return (TERRAFORM / "main.tf").read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "artifact", [("infrastructure", "docker", "Dockerfile.frontend"), ("scripts", "deploy.sh")]
    )
    def test_no_build_path_emits_a_source_map(self, artifact):
        """Create React App defaults ``GENERATE_SOURCEMAP`` to true and nothing in the repository
        set it, so every build wrote ``*.js.map`` beside its bundle - and the publish step is a
        whole-directory rsync into a bucket that grants read to ``allUsers``. A source map
        republishes the original TypeScript, its comments and every identifier the compiler
        renamed. Both build paths must disable it, or the one that does not becomes the leak.
        """
        source = (REPOSITORY_ROOT.joinpath(*artifact)).read_text(encoding="utf-8")
        assert "GENERATE_SOURCEMAP=false" in source, artifact

    def test_the_published_build_is_the_guarded_build(self, deploy):
        """A substring check cannot see WHICH build publishes. The guards have to be on the one
        invocation whose output the rsync uploads, so both are asserted on that line."""
        builds = [
            line.strip()
            for line in deploy.splitlines()
            if "npm run build" in line and not line.lstrip().startswith("#")
        ]
        assert len(builds) == 1, builds
        assert "INLINE_RUNTIME_CHUNK=false" in builds[0], builds[0]
        assert "GENERATE_SOURCEMAP=false" in builds[0], builds[0]
        publishes = [
            line.strip()
            for line in deploy.splitlines()
            if "rsync" in line and not line.lstrip().startswith("#")
        ]
        assert len(publishes) == 1, publishes
        assert "frontend/build" in publishes[0], publishes[0]

    def test_the_edge_refuses_to_rewrite_an_asset_path(self, terraform):
        """The url map rewrote EVERY 404 to /index.html with status 200, so a missing bundle,
        stylesheet or icon was answered with the document: a status-code health check called the
        origin healthy while it served nothing usable, a CDN cached an HTML body under a
        script's cache key, and nosniff became the only control stopping the browser executing
        that document as JavaScript. Asset paths must keep the bucket's own status, which is the
        same scoping infrastructure/docker/nginx.conf applies with try_files ... =404.
        """
        matcher = re.search(r"path_matcher\s*\{(.*?)\n  \}", terraform, flags=re.DOTALL)
        assert matcher, "no path matcher found on the url map"
        body = matcher.group(1)
        rule = re.search(r"path_rule\s*\{(.*?)\n    \}", body, flags=re.DOTALL)
        assert rule, "no asset path rule found"
        assert '"/static/*"' in rule.group(1), rule.group(1)
        policy = re.search(
            r"custom_error_response_policy\s*\{(.*?)\n      \}", rule.group(1), flags=re.DOTALL
        )
        assert policy, "the asset rule declares no error-response policy"
        # Declining the inherited rewrite takes a rule that MATCHES 404, because the policy is
        # resolved per code at the lowest level matching it -- an empty policy matches nothing,
        # so the inherited rewrite would still win and the scoping would be silently inert.
        assert "error_response_rule" in policy.group(1), policy.group(1)
        assert '"404"' in policy.group(1), policy.group(1)
        # ... and it must name no path, or it would rewrite the very thing it is declining.
        assert re.search(r"^\s*path\s*=", policy.group(1), flags=re.MULTILINE) is None, (
            "the asset rule rewrites to a path, which reinstates the behaviour it must decline: "
            + policy.group(1)
        )
        override = re.search(r"override_response_code\s*=\s*(\d+)", policy.group(1))
        assert override and override.group(1) == "404", policy.group(1)

    def test_a_client_routed_path_still_reaches_the_entry_document(self, terraform):
        """The other half of the same contract: scoping the rewrite must not remove it. A deep
        link is a fresh request to the load balancer and Cloud Storage holds no object there."""
        rewrites = re.findall(
            r"error_response_rule\s*\{[^}]*?match_response_codes\s*=\s*\[\"404\"\][^}]*?"
            r"path\s*=\s*\"/index\.html\"[^}]*?override_response_code\s*=\s*200",
            terraform,
            flags=re.DOTALL,
        )
        # One at url-map level and one at path-matcher level: a matcher declaring none of its
        # own does not necessarily inherit it.
        assert len(rewrites) == 2, len(rewrites)

    def test_the_published_objects_carry_a_cache_lifetime(self, deploy):
        """Cloud Storage serves Cache-Control from the OBJECT, not from the load balancer, so
        without this every object was served with no Cache-Control at all - including the entry
        document, which names the content-hashed bundles to load and carries the meta
        Content-Security-Policy, so a cached copy pins a browser to a superseded bundle set and
        a superseded policy."""
        lines = [line.strip() for line in deploy.splitlines() if "setmeta" in line]
        assert lines, "the deployment publishes no cache metadata"
        joined = "\n".join(lines)
        assert "max-age=31536000, immutable" in joined, joined
        assert "Cache-Control:no-cache" in joined, joined

    def test_the_cache_metadata_is_published_after_the_upload(self, deploy):
        """rsync uploads new objects without this metadata, so setting it first would leave
        every freshly uploaded object bare."""
        body = [
            (index, line.strip())
            for index, line in enumerate(deploy.splitlines())
            if not line.lstrip().startswith("#")
        ]
        rsync = next(index for index, line in body if "rsync" in line)
        setmeta = min(index for index, line in body if "setmeta" in line)
        assert rsync < setmeta, (rsync, setmeta)

    def test_the_two_delivery_paths_agree_on_the_cache_lifetimes(self, deploy):
        """The container renders these from a map; the bucket carries them as metadata. A
        divergence would mean the same object is cached differently depending on which edge
        served it."""
        nginx = (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"
        ).read_text(encoding="utf-8")
        for lifetime in ("max-age=31536000, immutable", "no-cache"):
            assert lifetime in nginx, lifetime
            assert lifetime in deploy, lifetime

    def test_the_direct_object_path_exposure_is_published_in_full(self, terraform):
        """The bucket grants allUsers so the load balancer can read it, which leaves a second,
        unheadered way to reach every object. It is accepted rather than closed - closing it is
        the private-bucket-behind-Cloud-CDN redesign carried as F12 - so the acceptance has to
        state what is actually exposed. Framing is the part that is easy to leave out: a meta
        element ignores frame-ancestors entirely, so the document cannot substitute for the
        missing header and the direct path is framable.
        """
        published = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for record in (terraform, published):
            assert "storage.googleapis.com" in record
            assert "frame-ancestors" in record
            assert "nosniff" in record
        assert "F12" in published


class TestDocumentShellReferences:
    """Every path the compiled document requests must be a path a build actually publishes.

    Both static delivery paths answer an unmatched path with this document under status 200 -
    nginx through ``try_files`` and the load balancer through its custom error response policy -
    so a reference to something no build emits does not fail loudly. It succeeds with the wrong
    body, and ``X-Content-Type-Options: nosniff`` is then the only thing between the browser and
    parsing an HTML document as whatever the reference asked for.
    """

    PUBLIC = REPOSITORY_ROOT / "frontend" / "public"

    @pytest.fixture
    def document(self):
        """The document's MARKUP, with comments removed.

        The comments explain what was removed and quote it, so an assertion made against the
        raw text would match the very thing it exists to forbid. What ships to a browser is
        the markup, and that is what these assertions are about.
        """
        raw = (self.PUBLIC / "index.html").read_text(encoding="utf-8")
        return re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)

    def test_the_document_declares_no_script_element_of_its_own(self, document):
        """Create React App injects the elements for the bundles it emits and does not remove
        one an author wrote, so a hardcoded tag is served ALONGSIDE the real bundle and asks for
        a path no build produces."""
        scripts = re.findall(r"<script\b[^>]*>", document, flags=re.IGNORECASE)
        assert scripts == [], scripts

    def test_the_document_declares_no_canonical_url(self, document):
        """``og:url`` must be the ABSOLUTE canonical URL, which is a per-deployment value this
        file cannot know - the domain lives in the ``domain_name`` Terraform variable and Create
        React App substitutes nothing here. It carried a placeholder naming a domain this
        project does not own, so a crawler that trusted it attributed the page elsewhere. Absent,
        a crawler falls back to the URL it requested, which is right for every deployment."""
        assert 'property="og:url"' not in document

    def test_the_document_names_no_placeholder_domain(self, document):
        """The check that catches a placeholder reintroduced under any property name."""
        for placeholder in ("your-excel-app-url", "example.com", "your-domain"):
            assert placeholder not in document, placeholder

    @pytest.mark.parametrize(
        "asset", ["favicon.ico", "logo192.png", "manifest.json", "og-image.jpg"]
    )
    def test_every_referenced_public_asset_exists(self, asset, document):
        assert asset in document, "%s is no longer referenced" % asset
        published = self.PUBLIC / asset
        assert published.is_file(), "%s is referenced but not published" % asset
        assert published.stat().st_size > 0, asset

    def test_every_public_url_reference_resolves_to_a_published_file(self, document):
        """The complete set, so a reference added later is caught rather than only these four.

        Create React App replaces ``%PUBLIC_URL%`` with the deployment's public path, so every
        such reference is a request for a file at the root of the published directory.
        """
        referenced = set(re.findall(r"%PUBLIC_URL%/([^\"'\s>]+)", document))
        assert referenced, "no %PUBLIC_URL% reference found"
        missing = sorted(name for name in referenced if not (self.PUBLIC / name).is_file())
        assert missing == [], missing

    def test_the_web_manifest_is_valid_json_the_browser_can_read(self):
        """An absent manifest was answered with the SPA document, which the browser reported as
        ``Manifest: Line: 1, column: 1, Syntax error``."""
        import json

        manifest = json.loads((self.PUBLIC / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["name"]
        assert manifest["icons"], "a manifest with no icons defeats its own purpose"
        for icon in manifest["icons"]:
            assert (self.PUBLIC / icon["src"]).is_file(), icon["src"]

    @pytest.mark.parametrize(
        "asset,magic",
        [
            ("favicon.ico", b"\x00\x00\x01\x00"),
            ("logo192.png", b"\x89PNG\r\n\x1a\n"),
            ("og-image.jpg", b"\xff\xd8\xff"),
        ],
    )
    def test_every_published_image_is_the_format_its_extension_claims(self, asset, magic):
        """``nosniff`` is emitted on both delivery paths, so a file whose bytes disagree with
        its extension is refused by the browser rather than sniffed into working."""
        assert (self.PUBLIC / asset).read_bytes().startswith(magic), asset


class TestStaticDeliveryRouting:
    """How the container decides WHAT to answer a request with, not just which headers to add.

    The header set was comprehensive while the routing beneath it answered every unmatched path
    with the entry document under status 200. That made ``X-Content-Type-Options: nosniff`` the
    only control standing between a browser and executing an HTML document as JavaScript, turned
    a status-code health check into a liar, and let a CDN cache an HTML body under a script's
    cache key. These assertions pin the routing so a header cannot be load-bearing again.
    """

    NGINX = REPOSITORY_ROOT / "infrastructure" / "docker" / "nginx.conf"

    @pytest.fixture
    def nginx(self):
        return self.NGINX.read_text(encoding="utf-8")

    @pytest.fixture
    def configured(self):
        """The file with every comment removed.

        This configuration explains itself at length, and the prose names the directives and
        media types it is explaining. A regex run over the raw text matches that prose: the
        ``charset_types`` assertion below first captured a comment listing nginx's built-in types
        and passed on it, which would have let the directive be wrong while the test was green.
        Prose naming a directive is not that directive.
        """
        raw = self.NGINX.read_text(encoding="utf-8")
        return "\n".join(line.split("#", 1)[0] for line in raw.splitlines())

    @staticmethod
    def _directives(text):
        """Every directive in the file paired with the block path it sits in.

        A hand-rolled walk rather than a parser dependency: nginx configuration is brace-nested
        and the only structure these assertions need is which block a directive belongs to.
        Comments are stripped first, because this file explains itself at length and prose
        naming a directive is not that directive.
        """
        walked = []
        stack = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.endswith("{"):
                stack.append(" ".join(line[:-1].split()))
                continue
            if line == "}":
                if stack:
                    stack.pop()
                continue
            walked.append((tuple(stack), line))
        return walked

    def test_an_asset_request_cannot_fall_back_to_the_entry_document(self, nginx):
        """The rule that stops a missing bundle, stylesheet, icon or /robots.txt answering 200
        with HTML. Without it a health check reports the origin healthy while it serves nothing
        usable, and Lighthouse's robots-txt audit fails on a 28-line HTML shell."""
        asset_blocks = [
            block
            for block, _ in self._directives(nginx)
            if block and block[-1].startswith("location ~*") and "js" in block[-1]
        ]
        assert asset_blocks, "no asset location block found"
        asset_block = asset_blocks[0]
        terminators = [
            directive
            for block, directive in self._directives(nginx)
            if block == asset_block and directive.startswith("try_files")
        ]
        assert terminators == ["try_files $uri =404;"], terminators

    @pytest.mark.parametrize(
        "extension", ["js", "css", "json", "ico", "png", "svg", "woff2", "txt", "wasm"]
    )
    def test_the_asset_rule_covers_the_extensions_a_build_publishes(self, extension, configured):
        pattern = re.search(r"location ~\* \\\.\(\?:([^)]+)\)\$", configured)
        assert pattern, "no asset extension group found"
        covered = pattern.group(1).split("|")
        assert any(
            extension == alternative or extension.startswith(alternative.rstrip("?2"))
            for alternative in covered
        ), "%s is not covered by %s" % (extension, covered)

    def test_dotfiles_are_refused(self, nginx):
        """`.env` and `.git/config` in the document root were returned verbatim under 200.
        Whole-directory publishing means anything that reaches the build output is served."""
        denied = [
            block[-1]
            for block, directive in self._directives(nginx)
            if directive == "deny all;" and block
        ]
        assert any("/\\." in rule for rule in denied), denied

    def test_source_maps_are_refused(self, nginx):
        """A map republishes the original TypeScript to anyone who asks."""
        denied = [
            block[-1]
            for block, directive in self._directives(nginx)
            if directive == "deny all;" and block
        ]
        assert any(".map$" in rule for rule in denied), denied

    def test_the_refusals_are_matched_before_the_asset_rule(self, nginx):
        """nginx takes the FIRST matching regex location, so ordering is the control.

        Listed after the asset rule, `main.js.map` would 404 rather than be refused - which
        leaks whether the file exists, and would serve it the moment one was published.
        """
        blocks = [block[-1] for block, _ in self._directives(nginx) if block]
        ordered = [rule for rule in blocks if rule.startswith("location")]
        dotfiles = next(index for index, rule in enumerate(ordered) if "/\\." in rule)
        maps = next(index for index, rule in enumerate(ordered) if ".map$" in rule)
        assets = next(index for index, rule in enumerate(ordered) if "wasm" in rule)
        fallback = next(index for index, rule in enumerate(ordered) if rule == "location /")
        assert dotfiles < maps < assets < fallback, ordered

    def test_a_refusal_has_no_error_page_of_its_own(self, nginx):
        """The fallback location maps 403 to the entry document so a directory resolves to the
        application. Were that mapping to reach a deny rule, a dotfile would be answered with
        the document under status 200 instead of being refused."""
        for block, directive in self._directives(nginx):
            if directive.startswith("error_page"):
                assert block[-1] == "location /", (block, directive)

    def test_a_directory_resolves_to_the_entry_document(self, nginx):
        """`try_files`' `$uri/` argument matches a real directory, `index` then finds nothing in
        it and `autoindex` is off, so nginx answered `/static/` with 403 and the fallback never
        ran. A client route colliding with a directory name showed an nginx error page."""
        directives = self._directives(nginx)
        mapped = [
            (block, directive)
            for block, directive in directives
            if directive.startswith("error_page 403")
        ]
        assert mapped == [(("server", "location /"), "error_page 403 = @spa;")], mapped
        assert any(block and block[-1] == "location @spa" for block, _ in directives)
        named = [
            directive
            for block, directive in directives
            if block and block[-1] == "location @spa"
        ]
        assert named == ["try_files /index.html =404;"], named

    def test_every_response_header_stays_at_server_level(self, nginx):
        """The invariant that keeps the six security headers on every response.

        nginx inherits ``add_header`` from an enclosing level ONLY when the current level
        declares none of its own. A single ``add_header`` inside any location would therefore
        drop all six from every response that location serves - silently, with no warning from
        ``nginx -t``. This is why the cache value arrives through a map variable.
        """
        misplaced = [
            (block, directive)
            for block, directive in self._directives(nginx)
            if directive.startswith("add_header") and block[-1:] != ("server",)
        ]
        assert misplaced == [], misplaced

    def test_the_cache_policy_is_keyed_on_the_status_as_well_as_the_path(self, nginx, configured):
        """Keyed on the path alone, a 404 for a hashed asset not yet published - the ordinary
        state during a rollout, since the document and its bundles do not land in the same
        instant - was itself immutable for a year. A browser that asked one moment too early
        cached that 404 and stayed broken until the cache expired."""
        source = re.search(r"map\s+(\S+)\s+\$excel_app_cache_control", configured)
        assert source, "no cache-control map found"
        assert "$status" in source.group(1), source.group(1)
        immutable = [
            directive
            for block, directive in self._directives(nginx)
            if block and block[0].startswith("map") and "immutable" in directive
        ]
        assert immutable, "no immutable rule found"
        for rule in immutable:
            assert "200" in rule and "304" in rule, rule
            assert "/static/" in rule, rule

    def test_the_entry_document_is_never_cached_without_revalidation(self, nginx):
        """It names which hashed bundles to load and carries the meta Content-Security-Policy,
        so a cached copy pins a browser to a superseded bundle set and a superseded policy."""
        defaults = [
            directive
            for block, directive in self._directives(nginx)
            if block and block[0].startswith("map") and directive.startswith("default")
        ]
        assert defaults == ['default                "no-cache";'], defaults

    def test_the_container_declares_a_text_encoding(self, nginx):
        """nginx sends a bare ``Content-Type: text/html`` by default. Decoding worked only
        because the document declares its own charset, and nothing carries that declaration for
        a JSON, JavaScript, CSS or plain-text response."""
        assert "charset utf-8;" in nginx

    @pytest.mark.parametrize(
        "media_type",
        [
            # nginx's built-in charset_types list, which the directive REPLACES rather than
            # extends - so every default has to be restated or its charset silently disappears.
            "text/xml",
            "text/plain",
            "text/vnd.wap.wml",
            "application/javascript",
            "application/rss+xml",
            # The types the built-in list omits, which is why the directive is needed at all.
            # Measured: with charset alone the web manifest went out as bare application/json.
            "text/css",
            "application/json",
            "application/manifest+json",
            "image/svg+xml",
        ],
    )
    def test_the_encoding_reaches_every_text_media_type(self, media_type, configured):
        """``charset`` applies only to ``charset_types``, and that list defaults to six types
        that exclude JSON and CSS - so the declaration silently missed the very responses the
        directive exists for."""
        declared = re.search(r"charset_types\s+([^;]+);", configured)
        assert declared, "no charset_types directive found"
        assert media_type in declared.group(1).split(), (media_type, declared.group(1))

    def test_the_encoding_list_does_not_repeat_what_nginx_always_includes(self, configured):
        """``text/html`` is always in the list. Naming it again makes ``nginx -t`` warn
        "duplicate MIME type", and a warning in the one file whose purpose is reviewability
        trains a reviewer to skim past warnings."""
        declared = re.search(r"charset_types\s+([^;]+);", configured)
        assert declared, "no charset_types directive found"
        assert "text/html" not in declared.group(1).split(), declared.group(1)


class TestPublishedSecurityDocumentation:
    """``SECURITY.md`` publishes the response-header values, so they are pinned to the code.

    The document is the canonical published record of what the application emits, which means
    a header changed in ``backend/app/core/security_headers.py`` without the document being
    updated in the same commit is a documentation defect that ships. These tests make it a
    test failure instead.
    """

    @pytest.fixture
    def policy_document(self):
        return (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")

    def test_every_static_header_value_is_published(self, policy_document):
        from backend.app.core.security_headers import STATIC_SECURITY_HEADERS

        for name, value in STATIC_SECURITY_HEADERS.items():
            assert "`%s`" % name in policy_document, name
            assert "`%s`" % value in policy_document, (name, value)

    def test_both_policy_header_names_are_published(self, policy_document):
        from backend.app.core.security_headers import (
            CONTENT_SECURITY_POLICY_HEADER,
            CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER,
        )

        assert "`%s`" % CONTENT_SECURITY_POLICY_HEADER in policy_document
        assert "`%s`" % CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER in policy_document

    def test_the_published_policy_is_the_policy_that_is_emitted(self, policy_document):
        """The document reproduces the policy across lines, so both sides are normalised."""
        import re

        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        blocks = re.findall(r"```\n(.*?)\n```", policy_document, re.S)
        normalised = [" ".join(block.split()) for block in blocks]
        assert " ".join(CONTENT_SECURITY_POLICY.split()) in normalised, normalised

    def test_the_published_directive_count_matches(self, policy_document):
        """Prose counts rot silently, so the two the document states are pinned too."""
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        directives = len(CONTENT_SECURITY_POLICY.split(";"))
        expected = "%s directives" % _NUMBER_WORDS[directives]
        assert expected in policy_document.lower(), expected

    def test_the_published_header_count_matches(self, policy_document):
        from backend.app.core.security_headers import STATIC_SECURITY_HEADERS

        # The five fixed headers plus whichever Content-Security-Policy name is emitted.
        emitted = len(STATIC_SECURITY_HEADERS) + 1
        expected = "%s header names" % _NUMBER_WORDS[emitted]
        assert expected in policy_document.lower(), expected
class TestApiClientContract:
    """V3 client half, M1 and M6, asserted on the source of a module this suite cannot run.

    The API client is TypeScript. It has no runner here - ``react-scripts`` is imported by the
    application yet undeclared in ``frontend/package.json``, which is out of this change set's
    scope - so its security-relevant properties are asserted on its source, exactly as the
    Nginx, document, Terraform and deployment artifacts are.
    """

    @pytest.fixture
    def api_client_source(self):
        return (
            REPOSITORY_ROOT / "frontend" / "src" / "services" / "api.ts"
        ).read_text(encoding="utf-8")

    def test_the_bearer_token_is_attached_by_a_request_interceptor(self, api_client_source):
        assert "interceptors.request.use" in api_client_source
        assert "getIdToken()" in api_client_source
        assert "`Bearer ${token}`" in api_client_source

    def test_a_request_with_no_token_is_refused_rather_than_sent(self, api_client_source):
        assert "if (!token)" in api_client_source
        assert "NOT_AUTHENTICATED" in api_client_source

    def test_the_interceptor_is_confined_to_the_api_instance(self, api_client_source):
        """Installed on the global axios default it would attach the credential to every
        axios call anywhere in the bundle."""
        assert "axios.create({ baseURL: API_BASE_URL })" in api_client_source
        assert "axios.interceptors" not in api_client_source

    def test_the_token_is_removed_from_a_failed_request(self, api_client_source):
        assert "redactAuthorizationHeader" in api_client_source

    def test_every_interpolated_path_segment_is_encoded(self, api_client_source):
        """M1: an identifier is caller-supplied, and interpolated raw a value carrying ``/``
        or ``..`` changes which resource the request addresses."""
        import re

        assert "function pathSegment" in api_client_source
        assert "encodeURIComponent" in api_client_source
        paths = re.findall(r"`(/[^`]*)`", api_client_source)
        assert paths, "no request path template was found"
        raw = [
            expression
            for path in paths
            for expression in re.findall(r"\$\{([^}]+)\}", path)
            if not expression.startswith("pathSegment(")
        ]
        assert raw == [], raw

    def test_the_write_budget_is_set_from_the_coalescing_window(self, api_client_source):
        """M6: the queue and ``rate_limit_write`` are one decision. At the superseded 500 ms
        window a single edited worksheet could spend the whole shared write budget, so the
        window and the budget were raised together and neither may move alone."""
        from backend.app.core.config import Settings

        assert "CELL_WRITE_COALESCE_MS = 1000" in api_client_source
        budget = int(Settings.__fields__["rate_limit_write"].default.split("/")[0])
        assert budget >= 5 * (60_000 // 1000)

    def test_the_exported_update_contract_is_preserved(self, api_client_source):
        """The route takes a list and answers with an acknowledgement rather than cells, so
        the exported signature is unchanged and each caller is settled with the cell it
        supplied - there is no cell in the response to return."""
        assert (
            "export const updateCell = (workbookId: string, worksheetId: string, "
            "cell: CellSchema): Promise<CellSchema>" in api_client_source
        )
        assert "waiters.forEach((waiter) => waiter.resolve(waiter.cell));" in api_client_source
        assert "waiters.forEach((waiter) => waiter.reject(error));" in api_client_source

    def test_the_client_credential_path_is_covered_by_its_own_tests(self):
        """The interceptor attaches the only credential the API accepts, so a regression in
        it locks every user out. ``ci.yml`` runs this module as a blocking gate."""
        module = REPOSITORY_ROOT / "frontend" / "src" / "services" / "api.test.ts"
        assert module.exists()
        source = module.read_text(encoding="utf-8")
        assert "Bearer" in source
        assert "coalesce" in source

    @pytest.fixture
    def page_sources(self):
        pages = REPOSITORY_ROOT / "frontend" / "src" / "pages"
        return {
            name: (pages / name).read_text(encoding="utf-8")
            for name in ("Dashboard.tsx", "Workbook.tsx")
        }

    @pytest.mark.parametrize("page", ["Dashboard.tsx", "Workbook.tsx"])
    def test_a_refused_credential_clears_the_signed_in_identity(self, page_sources, page):
        """D103: the interface may not keep presenting a session the server has refused.

        ``api.ts`` sets ``reauthenticate`` from the status alone, so a page keys off that
        rather than deciding for itself which codes mean an expired credential.
        """
        source = page_sources[page]
        assert "apiFailure(err)?.reauthenticate === true" in source, page
        assert "dispatch(clearUser())" in source, page

    @pytest.mark.parametrize("page", ["Dashboard.tsx", "Workbook.tsx"])
    def test_every_failure_surface_is_announced(self, page_sources, page):
        """Closes residual 29: a screen-reader user was not told the session had ended,
        because the containers holding the message carried no live-region role."""
        source = page_sources[page]
        containers = re.findall(r'className="([a-z-]*error[a-z-]*)"', source)
        assert containers, page
        for container in containers:
            marker = source.split('className="%s"' % container)[1][:40]
            assert 'role="alert"' in marker, (page, container)

    def test_a_failed_cell_write_does_not_unmount_the_workbook(self, page_sources):
        """The finding this closes: the error state was rendered by an EARLY RETURN, so one
        refused cell write replaced the grid, the ribbon, the formula bar and the sidebar with
        a bare message - no retry, no dismissal, and no indication of which cell failed.

        Two states now exist because they cost different things to show. A *load* failure still
        returns early, which is correct: there is no workbook to render. A *write* failure is a
        sibling of the content, so the grid stays where the person editing left it.
        """
        source = page_sources["Workbook.tsx"]
        assert "const [saveError, setSaveError] = useState<string | null>(null);" in source
        # The write failure sets the non-blocking state, and the blocking one is not reachable
        # from the write path at all.
        write_path = source.split("const handleCellUpdate")[1].split("if (loading)")[0]
        assert "setSaveError(" in write_path
        assert "setError(" not in write_path
        # It is rendered inside the returned tree, alongside the grid, not in place of it.
        rendered = source.split("<div className=\"workbook-container\">")[1]
        assert "saveError === null ? null : (" in rendered
        assert "<Grid " in rendered
        # And it names the cell, because the edit was not saved and the caller has to know which.
        assert "`${cellId}: ${apiFailureMessage(" in source

    def test_the_collaboration_client_addresses_one_document(self):
        """The Firestore rules match /workbooks/{workbookId}, so a subscription built with
        ``collection()`` addresses a path no rule governs and ``onSnapshot`` cannot
        evaluate against the document the owner check reads."""
        source = (
            REPOSITORY_ROOT / "frontend" / "src" / "services" / "collaboration.ts"
        ).read_text(encoding="utf-8")
        assert "doc(db, 'workbooks', workbookId)" in source
        assert "collection(db, 'workbooks', workbookId)" not in source


class TestDeploymentSurface:
    """V8 and V11: the committed edge is HTTPS-only and grants no public invoker."""

    @pytest.fixture
    def deploy_script(self):
        return (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    def test_no_public_invoker_is_granted(self, deploy_script):
        assert "--allow-unauthenticated" not in code_only(deploy_script)

    def test_no_plaintext_listener_is_created(self, deploy_script):
        script = code_only(deploy_script)
        assert "target-http-proxies create" not in script
        assert "--ports=80 " not in script

    def test_the_script_names_one_service_account(self, deploy_script):
        """A second runtime address to configure would describe a topology Terraform does
        not build, and the preflight would compare the pod spec against the wrong identity."""
        assert "RUNTIME_SERVICE_ACCOUNT" not in deploy_script
        assert "SIGNER_SERVICE_ACCOUNT" in deploy_script

    def test_the_preflight_requires_the_runtime_grants(self, deploy_script):
        """Each of these is load-bearing on the request path, so preflight asserts it."""
        for role in (
            "roles/iam.workloadIdentityUser",
            "roles/firebaseauth.viewer",
            "roles/cloudsql.client",
            "roles/datastore.user",
        ):
            assert role in deploy_script, role
        assert ".svc.id.goog[${KUBERNETES_NAMESPACE}/${KUBERNETES_SERVICE_ACCOUNT}]" in (
            deploy_script
        )

    def test_the_port_80_redirect_is_required_not_staged(self, deploy_script):
        """Absence of the redirect is a finding, not the first half of a cutover."""
        assert "https_cutover_enabled" not in deploy_script
        assert "HTTP_REDIRECT_ONLY" not in deploy_script
        assert "The port-80 forwarding rule excel-app-http-forwarding-rule does not exist." in (
            deploy_script
        )

    def test_no_step_is_a_placeholder_before_the_success_banner(self, deploy_script):
        """Two steps printed a heading, did nothing, and let the run report success."""
        placeholders = [
            line
            for line in deploy_script.splitlines()
            if line.strip().startswith("# HUMAN ASSISTANCE NEEDED")
        ]
        assert placeholders == []
        assert "DB_MIGRATION_COMMAND" in deploy_script
        assert "POST_DEPLOY_TEST_COMMAND" in deploy_script

    def test_the_backend_image_is_built_from_the_real_dockerfile(self, deploy_script):
        """``cd backend && docker build .`` names a directory that holds no Dockerfile."""
        assert 'docker build -f "$BACKEND_DOCKERFILE"' in deploy_script
        assert (
            REPOSITORY_ROOT / "infrastructure" / "docker" / "Dockerfile.backend"
        ).is_file()

    def test_the_known_blockers_are_stated_up_front(self, deploy_script):
        """The script stops on defects in files this change set may not edit; a reader is
        told which, and where, before running it rather than one abort at a time."""
        assert "KNOWN BLOCKERS" in deploy_script

    def test_the_static_bucket_is_not_claimed_unreachable(self, deploy_script):
        """The bucket grants allUsers read, so objects stay fetchable without the edge."""
        assert "served ONLY at" not in deploy_script
        assert "supported entry point" in deploy_script


class TestOperatorFacingClaims:
    """The published documents must not describe a control the code does not have.

    Three claims in particular were wrong in a way an operator could act on: that a Bearer
    token stayed mandatory once enforcement was disabled, that token revocation was not
    checked, and that the write budget was a value it no longer is. Each is pinned here
    against the code, so a document and its implementation cannot drift apart silently.
    """

    @pytest.fixture
    def readme(self):
        return (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    @pytest.fixture
    def policy(self):
        return (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")

    @pytest.fixture
    def onboarding(self):
        return (
            REPOSITORY_ROOT / "documentation" / "Developer Onboarding.md"
        ).read_text(encoding="utf-8")

    @pytest.fixture
    def decisions(self):
        return (
            REPOSITORY_ROOT / "documentation" / "Security Decision Log.md"
        ).read_text(encoding="utf-8")

    @pytest.fixture
    def traceability(self):
        return (
            REPOSITORY_ROOT / "documentation" / "Security Traceability Matrix.md"
        ).read_text(encoding="utf-8")

    def test_the_authentication_guarantee_is_qualified_everywhere(
        self, readme, policy, onboarding
    ):
        """An unqualified "all routes require authentication" is false while the switch is
        false, and it is exactly the claim that makes the switch look safe to flip."""
        for name, document in (
            ("README.md", readme),
            ("SECURITY.md", policy),
            ("Developer Onboarding.md", onboarding),
        ):
            assert "auth_enforcement_enabled" in document, name

    def test_the_credential_requirement_is_stated_not_overstated(
        self, readme, policy, onboarding
    ):
        """The switch weakens verification; it does not open a credential-less path. Both
        the earlier claims were actionable errors in opposite directions - one said a token
        stayed fully checked, the next said none was needed at all.

        The onboarding guide is asserted alongside the other two because omitting it is how the
        false claim survived: the guide went on publishing credential-less admission on a
        placeholder ``User()``, and ``auto_error=False``, long after both were corrected
        elsewhere. Every document that describes the switch is checked, not a subset.
        """
        source = (
            BACKEND_APP / "core" / "security.py"
        ).read_text(encoding="utf-8")
        assert "_anonymous_caller" not in source
        assert "auto_error" not in code_only(source)
        assert "placeholder User()" not in source
        for name, document in (
            ("README.md", readme),
            ("SECURITY.md", policy),
            ("Developer Onboarding.md", onboarding),
        ):
            assert "no `Authorization` header" in document, name
            # No document may name a construct the module does not carry. Phrased as the
            # construct itself rather than as prose about a placeholder, so a document may
            # still RECORD that an earlier version made the claim - SECURITY.md does, and
            # that correction record is the reason the error is not repeated.
            assert "auto_error=False" not in document, name
            assert "on a transient placeholder" not in document, name
        assert "The caller has no identity of any kind." not in policy

    def test_revocation_is_not_documented_as_absent(self, policy):
        """``check_revoked=True`` is passed, so a document saying otherwise understates the
        control and hides the IAM dependency it carries."""
        source = (
            BACKEND_APP / "core" / "security.py"
        ).read_text(encoding="utf-8")
        assert "check_revoked=True" in source
        assert "Token revocation is not checked" not in policy
        assert "roles/firebaseauth.viewer" in policy

    def test_the_published_switch_defaults_match_the_code(self, policy):
        from backend.app.core.config import Settings

        assert (
            "`600/minute` / `{0}`".format(
                Settings.__fields__["rate_limit_write"].default
            )
            in policy
        )

    def test_the_residual_register_carries_the_two_omitted_risks(self, policy):
        """A register that claims to list open risks and omits two is worse than no
        register: a reader takes its silence for absence.

        The email risk narrowed rather than closed. Restoring the ``email_verified`` gate shut
        the registration half of it - nobody can register a Firebase account carrying an
        existing local user's address and be admitted as them - while keying identity on an
        address the user can change is what remains open. So the register must still carry the
        half that is open, must record that the other half is shut, and must not go on
        advertising an unverified-address hole that no longer exists: a register that overstates
        is distrusted exactly as fast as one that omits.
        """
        assert "mutable email address" in policy
        assert (
            "`email_verified` claim must be exactly the boolean `true`" in policy
        )
        assert "mutable, unverified email" not in policy
        assert "Node 14" in policy

    def test_every_superseded_decision_says_so(self, decisions):
        """A log whose obsolete entries read as current is not a source of truth. Each
        entry is kept rather than deleted, because the reasoning is what explains why the
        later decision went the other way.

        ``R48`` is asserted alongside the others because its marker was where the marker's
        placement started to matter: it sat at the very end of a 2,400-character cell whose
        opening sentence read as a live instruction to build a two-account topology, and
        that reading is what the published control table then carried. The marker must
        therefore LEAD the decision cell, as ``R8``'s does.
        """
        for entry in ("| R8 |", "| R21 |", "| R48 |", "| D27 |", "| D31 |"):
            start = decisions.index(entry)
            row = decisions[start : decisions.index("\n", start)]
            assert "SUPERSEDED" in row, entry
        for entry in ("| R8 |", "| R48 |"):
            start = decisions.index(entry)
            row = decisions[start : decisions.index("\n", start)]
            decision_cell = row.split("|")[3].strip()
            assert decision_cell.startswith("**SUPERSEDED"), entry

    def test_the_traceability_arithmetic_is_self_consistent(self, traceability):
        """The reverse table's row count must equal the coverage it claims."""
        rows = re.findall(r"^\| (\d+) \| `[^`]+` \|", traceability, re.MULTILINE)
        assert [int(number) for number in rows] == list(range(1, len(rows) + 1))
        assert "Reverse coverage: {0} of {0}.".format(len(rows)) in traceability
        assert "Changed artifacts | **{0}**".format(len(rows)) in traceability


class TestConfigurationTemplate:
    """``.env.example`` is the operator's copy of the configuration surface.

    A key it documents that the application does not read wastes an operator's time; a
    field it omits is one they never set, and for a security switch that means a control
    left at whatever the code happens to default to without anyone deciding so. Both
    directions are therefore asserted, along with the documented default of every switch.
    """

    @pytest.fixture
    def template(self):
        return (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

    @staticmethod
    def _assignments(template):
        pairs = {}
        for line in template.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                name, value = stripped.split("=", 1)
                pairs[name.strip()] = value.strip()
        return pairs

    #: Read directly from the process environment by the Google Cloud client libraries.
    ADC_VARIABLES = {"GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"}

    def _backend_keys(self, template):
        assignments = self._assignments(template)
        return {
            name
            for name in assignments
            if not name.startswith("REACT_APP_") and name not in self.ADC_VARIABLES
        }

    def test_the_template_and_the_settings_surface_are_the_same_set(self, template):
        """Neither direction may drift: no undocumented field, no documented non-field."""
        from backend.app.core.config import Settings

        assert self._backend_keys(template) == set(Settings.__fields__)

    def test_no_withdrawn_key_is_still_documented(self, template):
        """Three keys were withdrawn to restore the authorized surface."""
        documented = self._backend_keys(template)
        for withdrawn in (
            "signer_service_account",
            "rate_limit_storage_uri",
            "rate_limit_trusted_proxies",
        ):
            assert withdrawn not in documented, withdrawn

    def test_every_documented_switch_states_its_real_default(self, template):
        """A template that shows a value the code does not default to teaches the wrong
        posture: an operator who copies it believes they have kept the default."""
        from backend.app.core.config import Settings

        documented = self._assignments(template)
        expected = {
            "signed_url_expiry_minutes": "15",
            "db_sslmode": "require",
            "auth_token_verifier": "firebase",
            "auth_enforcement_enabled": "true",
            "rate_limit_enabled": "true",
            "rate_limit_default": "600/minute",
            "rate_limit_write": "300/minute",
            "csp_report_only": "false",
        }
        for name, shown in expected.items():
            assert documented[name] == shown, name
            actual = Settings.__fields__[name].default
            assert str(actual).lower() == shown.lower(), (name, actual, shown)

    def test_the_write_budget_matches_the_client_coalescing_window(self, template):
        """One decision recorded in two files; neither may move without the other."""
        client = (
            REPOSITORY_ROOT / "frontend" / "src" / "services" / "api.ts"
        ).read_text(encoding="utf-8")
        assert "CELL_WRITE_COALESCE_MS = 1000" in client
        assert "rate_limit_write=300/minute" in template
        assert "1000 ms" in template

    def test_the_dead_cutover_flag_is_gone_from_the_operator_path(self, template):
        """The port-80 listener is unconditional, so no staged instruction remains."""
        assert "https_cutover_enabled" not in template

    def test_the_legacy_verifier_is_not_offered_as_a_lockout_remedy(self, template):
        """Its caller contract is a token the browser cannot mint, so selecting it to
        recover from a lockout makes the lockout total."""
        assert "IS NOT A ROLLBACK" in template

    def test_the_deployable_database_mode_is_distinguished(self, template):
        """Three modes are accepted at construction; the shipped proxy topology completes
        exactly one of them."""
        assert "DEPLOYABLE" in template
        assert "ACCEPTED here" in template


class TestContinuousIntegrationGates:
    """The security job's checks are gates, and its pins match the sources they build.

    A tolerated step reports rather than blocks, so a regression it detects reaches the
    branch anyway. Only the two steps that genuinely cannot run against this repository -
    ``npm audit`` needs a lockfile and declared packages - are tolerated; the rest fail the
    workflow.
    """

    @pytest.fixture
    def workflow(self):
        return (
            REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

    @pytest.fixture
    def deploy_script(self):
        return (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    def test_the_router_pin_matches_the_source_it_builds(self, workflow):
        """``app.tsx`` uses ``Switch`` and ``component=``, which react-router-dom 6 removed.

        Pinned at 6.30.1 the type check reports four errors in that file; pinned at 5.3.4 it
        reports none. Migrating the source is outside this change set, so CI installs the
        version the source is written for.
        """
        application = (
            REPOSITORY_ROOT / "frontend" / "src" / "app.tsx"
        ).read_text(encoding="utf-8")
        assert "Switch" in application
        assert "component={" in application
        assert "react-router-dom@5.3.4" in workflow
        assert "@types/react-router-dom@5.3.3" in workflow
        assert "react-router-dom@6" not in workflow

    def test_the_interceptor_check_is_a_gate(self, workflow):
        """The interceptor attaches the only credential the API accepts, so a regression
        locks every user out rather than degrading anything. No step in this job may be
        incapable of failing, the frontend audit included: it asserts which state the
        manifest is in instead of tolerating any outcome."""
        import yaml

        steps = yaml.safe_load(workflow)["jobs"]["security-checks"]["steps"]
        tolerated = [
            step.get("name", "") for step in steps if step.get("continue-on-error")
        ]
        assert tolerated == []
        interceptor = [
            step
            for step in steps
            if "interceptor" in str(step.get("name", "")).lower()
        ]
        assert len(interceptor) == 1
        assert not interceptor[0].get("continue-on-error")

    def test_every_undeclared_package_is_installed_in_one_command(self, workflow):
        """``--no-save`` prunes what a previous ``--no-save`` added, so a second invocation
        would remove the first one's packages and the runner would be missing them."""
        installs = [
            line
            for line in workflow.splitlines()
            if "install --no-save" in line
        ]
        assert len(installs) == 1

    def test_the_terraform_edge_terminates_tls(self):
        terraform = (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")
        assert "google_compute_managed_ssl_certificate" in terraform
        assert "google_compute_target_https_proxy" in terraform
        assert "ENCRYPTED_ONLY" in terraform
        assert "public_access_prevention" in terraform
        assert "uniform_bucket_level_access" in terraform

    # -- Cross-plane identifier contracts ----------------------------------------------
    # M8: scripts/deploy.sh asserts deployed state by naming Terraform resources in its
    # failure messages and by comparing against values Terraform sets. Those two files are
    # edited independently, and nothing bound them together: renaming or deleting a resource
    # left the script instructing an operator to apply something that no longer exists, and
    # removing a Terraform argument left the script asserting a value nothing produced. Both
    # happened. Each test below pins one such pair.
    def test_the_api_may_not_be_published_on_the_static_edge(self, deploy_script):
        """CR-4: this edge routes no API path, so its own origin is not a valid API base."""
        terraform = REPOSITORY_ROOT / "infrastructure" / "terraform"
        main = (terraform / "main.tf").read_text(encoding="utf-8")
        variables = (terraform / "variables.tf").read_text(encoding="utf-8")

        # The URL map refuses the edge domain as the API origin at plan time.
        assert 'var.api_origin != "https://${var.domain_name}"' in main
        # No default admits the unsupported same-origin mode any more.
        assert 'default     = ""' not in variables.split('variable "api_origin"')[1].split(
            "variable "
        )[0]
        # The origin is always a connect-src source rather than conditionally present.
        assert 'var.api_origin == "" ? [] : [var.api_origin]' not in main
        # The deployment refuses a build whose API origin is the edge domain.
        assert 'BUILD_API_ORIGIN" = "https://${DOMAIN_NAME}"' in deploy_script

    def test_the_deployment_compares_whole_policy_source_tokens(self, deploy_script):
        """MJ-2: substring matching approved an origin the policy does not admit."""
        assert "csp_admits_origin" in deploy_script
        assert "grep -Fxq" in deploy_script
        assert '*"$BUILD_API_ORIGIN"*' not in deploy_script

    def test_the_container_states_that_it_serves_no_api(self):
        docker = REPOSITORY_ROOT / "infrastructure" / "docker"
        nginx = (docker / "nginx.conf").read_text(encoding="utf-8")
        dockerfile = (docker / "Dockerfile.frontend").read_text(encoding="utf-8")
        directives = [
            line.strip()
            for line in nginx.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not [line for line in directives if line.startswith("proxy_pass")]
        assert "STATIC FILES ONLY" in nginx
        assert "STATIC FILES ONLY" in dockerfile


# ===========================================================================
# The published security documentation states the delivered values
# ===========================================================================
class TestSecurityDocumentation:
    """MJ-8: the documentation named values the code does not emit.

    ``SECURITY.md`` is what an operator configures from, so a value recorded there that the
    code does not emit is a defect in its own right rather than a cosmetic one. These tests
    read the emitted values and require the document to carry them, so a control changed in
    one place fails here instead of drifting quietly.
    """

    @pytest.fixture
    def published(self):
        """SECURITY.md with every run of whitespace collapsed, so its line wrapping does
        not decide whether a value is found."""
        import re

        text = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        return re.sub(r"\s+", " ", text)

    def test_every_emitted_header_value_is_published(self, published):
        from backend.app.core.security_headers import (
            CONTENT_SECURITY_POLICY_HEADER,
            CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER,
            STATIC_SECURITY_HEADERS,
        )

        for name, value in STATIC_SECURITY_HEADERS.items():
            assert name in published, name
            assert value in published, (name, value)
        assert CONTENT_SECURITY_POLICY_HEADER in published
        assert CONTENT_SECURITY_POLICY_REPORT_ONLY_HEADER in published

    def test_every_emitted_policy_directive_is_published(self, published):
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        directives = [part.strip() for part in CONTENT_SECURITY_POLICY.split(";")]
        assert len(directives) == 13
        for directive in directives:
            assert directive in published, directive

    def test_the_published_throttling_budgets_are_the_configured_ones(self, published):
        """The document's switch table records defaults, so the declared defaults are what
        it is compared against - not a live instance, which reflects the local
        environment."""
        fields = importlib.import_module("backend.app.core.config").Settings.__fields__
        for name in ("rate_limit_default", "rate_limit_write"):
            default = fields[name].default
            assert default, name
            assert default in published, (name, default)


    @pytest.fixture
    def deploy_script(self):
        return (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    @pytest.fixture
    def terraform_main(self):
        return (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")

    @pytest.fixture
    def terraform_variables(self):
        return (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "variables.tf"
        ).read_text(encoding="utf-8")

    def test_every_terraform_resource_the_script_names_is_declared(
        self, deploy_script, terraform_main
    ):
        """C1: the script named four resources the configuration had stopped declaring."""
        import re

        declared = set(
            re.findall(r'^resource "([a-z_]+)" "([a-z_0-9]+)"', terraform_main, re.MULTILINE)
        )
        declared_addresses = {"{0}.{1}".format(kind, name) for kind, name in declared}
        # Attribute paths such as google_identity_platform_config.authorized_domains are not
        # resource addresses, so only the kinds this configuration actually declares are
        # considered.
        declared_kinds = {kind for kind, _ in declared}
        named = {
            address
            for address in re.findall(r"\bgoogle_[a-z_]+\.[a-z_0-9]+", deploy_script)
            if address.split(".", 1)[0] in declared_kinds
        }
        assert named, "the script names no Terraform resource at all"
        assert named <= declared_addresses, sorted(named - declared_addresses)

    def test_one_account_is_the_runtime_and_the_signer(
        self, terraform_main, deploy_script, published
    ):
        """C1: the API runtime and the URL signer are one account on every plane.

        backend/app/services/file_storage.py signs as the address its own ambient
        credentials report, so a second account could never be the signer: the runtime
        would ask the IAM signBlob endpoint to sign as itself and be refused. The runtime
        input is therefore not declared at all, and the deployment script reads the one
        address.

        The published document is one of those planes, and it was the plane that drifted.
        Its control table went on describing R48's two-account arrangement - a separate
        signer holding only read access, the signing role delegated onto it, and a plan
        that refuses two equal addresses - for a configuration that has none of those
        things, which understates residual 6: the signBlob capability belongs to the
        identity that serves traffic. Every one of those claims is forbidden here, and the
        collapse and the self-grant are required, so the row cannot revert to prose the
        Terraform contradicts.
        """
        assert 'resource "google_service_account" "api_runtime"' not in terraform_main
        assert 'resource "google_service_account" "url_signer"' in terraform_main
        assert "var.runtime_service_account" not in terraform_main
        assert "SIGNER_SERVICE_ACCOUNT" in deploy_script
        assert "RUNTIME_SERVICE_ACCOUNT" not in deploy_script

        for retracted in (
            "signing split from running",
            "a **separate** account exists only to be",
            "holds nothing but read access to the uploads bucket",
            "a delegation, never a self-grant",
            "fails the plan if the two addresses are equal",
        ):
            assert retracted not in published, retracted
        assert "**That one account is both the runtime and the URL signer**" in published
        assert "a **self-grant**, which is the correct and only working form" in published
        # The bucket role as granted, so "read access" cannot creep back in: objectAdmin
        # covers the write and the delete the upload path performs.
        assert "roles/storage.objectAdmin" in terraform_main
        assert "`roles/storage.objectAdmin` on the single uploads bucket" in published
        assert (
            "fails the plan if the configured address names any project other than this one"
            in published
        )

    def test_the_signing_grant_is_held_on_the_account_that_signs(self, terraform_main):
        """C1: signBlob authority must name the account the runtime signs as, which is
        itself, so the grant is a self-binding and is load-bearing rather than inert."""
        block = terraform_main.split(
            'resource "google_service_account_iam_member" "url_signer_token_creator"', 1
        )
        assert len(block) == 2, "the signing grant is not declared"
        body = block[1].split("\n}", 1)[0]
        assert "google_service_account.url_signer.name" in body, body
        assert "google_service_account.url_signer.member" in body, body
        assert "roles/iam.serviceAccountTokenCreator" in body, body

    def test_the_workload_identity_pool_and_binding_are_declared(self, terraform_main):
        """C1: the node pool requested GKE_METADATA with no pool on the cluster."""
        assert "workload_identity_config" in terraform_main
        assert "${var.project_id}.svc.id.goog" in terraform_main
        assert (
            'resource "google_service_account_iam_member" "api_runtime_workload_identity"'
            in terraform_main
        )
        assert "roles/iam.workloadIdentityUser" in terraform_main

    def test_the_revocation_check_has_its_iam_grant(self, terraform_main):
        """C2: verification uses check_revoked=True, which needs firebaseauth.users.get.

        Without the grant the lookup fails, so every VALID credential is refused - the
        whole application unreachable while the tokens themselves are fine.
        """
        security = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        assert "check_revoked=True" in security
        assert "roles/firebaseauth.viewer" in terraform_main
        assert "api_runtime_firebaseauth_viewer" in terraform_main

    def test_the_invoker_binding_is_authoritative(self, terraform_main):
        """C5: an additive member grant left a surviving allUsers binding in place."""
        assert (
            'resource "google_cloudfunctions_function_iam_binding" "invoker"' in terraform_main
        )
        assert (
            'resource "google_cloudfunctions_function_iam_member"' not in terraform_main
        )

    def test_the_function_trigger_refuses_plaintext(self, terraform_main, deploy_script):
        """M1: the provider default SECURE_OPTIONAL serves both schemes."""
        assert 'https_trigger_security_level = "SECURE_ALWAYS"' in terraform_main
        assert "SECURE_ALWAYS" in deploy_script

    def test_the_dead_cutover_contract_is_gone_from_every_plane(
        self, deploy_script, terraform_main, terraform_variables
    ):
        """M4: a variable declared as a security control that nothing ever read.

        An operator could satisfy it and change nothing. It is removed rather than wired,
        so the port-80 redirect is unconditional - and the only mention that may remain is
        the tombstone recording why the input is absent.
        """
        assert "https_cutover_enabled" not in deploy_script
        assert "https_cutover_enabled" not in terraform_main
        assert 'variable "https_cutover_enabled"' not in terraform_variables

    def test_the_plaintext_listener_is_verified_to_be_redirect_only(self, deploy_script):
        """M4: absence of the rule was accepted, and its target was never checked.

        A port-80 rule pointing at the application's own url map would serve every response
        in cleartext on the same address.
        """
        assert "excel-app-https-redirect-url-map" in deploy_script
        assert "excel-app-http-forwarding-rule does not exist" in deploy_script

    def test_the_proxy_marker_check_requires_both_markers(self, deploy_script):
        """M5: written with &&, either marker alone satisfied it. The two halves are now
        separate conditions, so neither can be satisfied by the other."""
        assert 'grep -q "cloud-sql-proxy" &&' not in deploy_script
        assert 'grep -q "cloud-sql-proxy" ||' not in deploy_script
        assert 'grep -q "cloud-sql-proxy"; then' in deploy_script
        assert 'grep -qF "$SQL_CONNECTION_NAME"; then' in deploy_script

    def test_the_transport_contract_is_one_contract_on_every_plane(
        self, deploy_script, terraform_main
    ):
        """C3: the script demanded the one mode the proxy cannot answer.

        The proxy presents a plain TCP loopback listener, so a client asking for TLS on it
        cannot connect. The script now requires the plaintext mode AND proves the endpoint
        is local, which is the same pair Settings enforces.
        """
        from backend.app.core.config import (
            ENCRYPTING_DATABASE_SSL_MODES,
            UNENCRYPTED_DATABASE_SSL_MODE,
        )

        assert UNENCRYPTED_DATABASE_SSL_MODE == "disable"
        assert "require" in ENCRYPTING_DATABASE_SSL_MODES
        assert "db_sslmode=disable" in deploy_script
        assert "Use db_sslmode=require" not in deploy_script
        assert "loopback" in deploy_script
        assert 'ssl_mode = "ENCRYPTED_ONLY"' in terraform_main

    def test_the_api_origin_is_proven_to_terminate_tls(self, deploy_script):
        """M11: the committed edge serves only the static bucket.

        The API's HTTPS ingress is outside Terraform, so a live handshake is the only
        available evidence; every earlier check read the configured origin's TEXT.
        """
        assert "openssl s_client" in deploy_script
        assert "-verify_hostname" in deploy_script
        assert "-verify_return_error" in deploy_script

    def test_the_function_source_contract_is_checked_live(self, deploy_script):
        """M7: Terraform can reject buckets it knows the names of and nothing else."""
        assert "sourceArchiveUrl" in deploy_script
        assert "allAuthenticatedUsers" in deploy_script
        assert "entryPoint" in deploy_script
        assert "httpsTrigger.securityLevel" in deploy_script

    def test_the_function_source_posture_is_read_not_merely_declared(
        self, terraform_main
    ):
        """A ``data`` block nothing references is inert: Terraform reads it and no plan or apply
        can fail because of what it found. The source-archive bucket's posture was declared this
        way and never consumed, so the published claim that the posture is "read from the live
        resource and asserted" described a check that could not run.

        Declaring it is therefore not the property worth pinning - CONSUMING it is. Each read
        must reach a ``condition``, which is the only construct that turns the value into a
        refusal.
        """
        assert 'data "google_storage_bucket" "function_source"' in terraform_main
        conditions = [
            line.strip()
            for line in terraform_main.splitlines()
            if line.strip().startswith("condition")
            and "data.google_storage_bucket.function_source" in line
        ]
        asserted = " ".join(conditions)
        assert (
            "public_access_prevention" in asserted
        ), "the archive bucket's public-access posture is not asserted: %s" % (conditions,)
        assert (
            "uniform_bucket_level_access" in asserted
        ), "the archive bucket's ACL posture is not asserted: %s" % (conditions,)

    def test_the_invoker_assertion_precedes_every_mutation(self, deploy_script):
        """C5: revocation ran after the frontend, the image and the manifests were live.

        Pinned by position rather than by presence: the check is only a gate if it happens
        before the things it is meant to gate.
        """
        mutation_marker = "MUTATIONS - nothing above this line changes any state"
        assert mutation_marker in deploy_script
        mutation_start = deploy_script.index(mutation_marker)
        assertion = deploy_script.index("roles/cloudfunctions.invoker")
        assert assertion < mutation_start, (
            "the invoker policy is asserted after the mutation boundary, so a publicly "
            "invocable function would be discovered with the release already published"
        )
        # And no invoker mutation survives anywhere.
        assert "remove-iam-policy-binding" in deploy_script  # only inside a failure message
        assert deploy_script.index("remove-iam-policy-binding") < mutation_start


class TestRuntimeIdentityTopology:
    """One service account, reached by the pods through Workload Identity.

    The runtime holds no key: it authenticates as ``google_service_account.url_signer``
    because the Kubernetes service account is bound to that account, and it signs object
    URLs as itself. Every grant the application needs is made to that one account, so a
    second "runtime" identity would be an account with no purpose and no grants. These
    assertions pin the pieces that are individually inert - a binding without the cluster
    pool, a pool without the binding, or a runtime without the Firebase Authentication read
    each leave the deployment unable to serve a single request.
    """

    @pytest.fixture
    def main(self):
        return (TERRAFORM / "main.tf").read_text(encoding="utf-8")

    @pytest.fixture
    def variables(self):
        return (TERRAFORM / "variables.tf").read_text(encoding="utf-8")

    def test_one_account_is_created_and_it_signs_as_itself(self, main):
        """One identity serves the API: it is the runtime, and it is the signer.

        ``function_runtime`` is the Cloud Function's own execution identity and holds no
        binding anywhere, which is what keeps the function off the App Engine default
        account and its project Editor role. It is not a second API identity.
        """
        accounts = re.findall(r'resource "google_service_account" "([a-z_]+)"', main)
        assert accounts == ["url_signer", "function_runtime"], accounts
        assert "google_service_account.function_runtime.member" not in main
        assert (
            'resource "google_service_account_iam_member" "url_signer_token_creator"'
            in main
        )
        assert "roles/iam.serviceAccountTokenCreator" in main

    def test_the_pods_may_authenticate_as_that_account(self, main):
        """The Google-side half of Workload Identity, keyed on the two Kubernetes inputs."""
        assert "roles/iam.workloadIdentityUser" in main
        assert (
            'serviceAccount:${var.project_id}.svc.id.goog'
            '[${var.kubernetes_namespace}/${var.kubernetes_service_account}]' in main
        )

    def test_the_cluster_issues_workload_identity_credentials(self, main):
        """Without the cluster pool the binding names a subject that cannot exist."""
        assert "workload_identity_config" in main
        assert 'workload_pool = "${var.project_id}.svc.id.goog"' in main
        assert "GKE_METADATA" in main

    def test_the_runtime_may_read_firebase_authentication_users(self, main):
        """``check_revoked=True`` looks the account up, and the lookup needs this role."""
        assert "roles/firebaseauth.viewer" in main
        assert "check_revoked" in (
            REPOSITORY_ROOT / "backend" / "app" / "core" / "security.py"
        ).read_text(encoding="utf-8")

    def test_no_second_runtime_account_is_an_input(self, variables):
        """A ``runtime_service_account`` input would describe a topology that is not built."""
        assert 'variable "runtime_service_account"' not in variables
        assert "var.runtime_service_account" not in (
            TERRAFORM / "main.tf"
        ).read_text(encoding="utf-8")

    def test_the_redirect_is_not_behind_a_flag(self, main, variables):
        """The port-80 listener redirects unconditionally, so no cutover input exists."""
        assert 'variable "https_cutover_enabled"' not in variables
        assert "https_cutover_enabled" not in main
        assert 'resource "google_compute_global_forwarding_rule" "excel_app_http"' in main
        assert "count" not in re.search(
            r'resource "google_compute_global_forwarding_rule" "excel_app_http" \{(.*?)\n\}',
            main,
            re.DOTALL,
        ).group(1)

    def test_every_declared_input_is_consumed_or_says_it_is_not(self, main, variables):
        """An input that is neither read nor labelled misrepresents the input surface.

        An operator who sets a value and sees no effect cannot tell that apart from a
        value that failed to apply, so a variable this configuration does not read must
        say so in its own description.
        """
        blocks = dict(
            re.findall(r'variable "([a-z_]+)" \{(.*?)\n\}\n', variables, re.DOTALL)
        )
        assert blocks, "no variable blocks parsed"
        consumed = set(re.findall(r"var\.([a-z_]+)", main))

        for name, body in blocks.items():
            if name in consumed:
                continue
            assert "NOT REFERENCED" in body, (
                f"{name} is not read by main.tf and does not say so"
            )
            assert "default" in body, (
                f"{name} is not read by main.tf yet is a required input"
            )


# ===========================================================================
# C2 / C3 / M7 / N2 - the identity and database contract the runtime depends on
# ===========================================================================
class TestDeploymentSequencing:
    """H6: checks that passed on partial evidence, and an order that published before it
    protected.

    Each of these is a race or a gap rather than a missing control: the control was written,
    and the thing that was supposed to enforce it accepted a state that does not satisfy it.
    """

    @pytest.fixture
    def deploy_script(self):
        return (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    def test_the_cloud_sql_check_requires_both_halves(self, deploy_script):
        """The condition was ``if ! A && ! B``, which is ``NOT (A OR B)``: it aborted only when
        BOTH were absent, so either half alone passed. A proxy container for some other
        instance passed, and this instance's connection name with no proxy to dial it passed."""
        # The defect was the `&&` continuation joining the two negations into one condition.
        # Its absence is the assertion; the two standalone `if`s are what replaced it.
        assert 'grep -q "cloud-sql-proxy" &&' not in deploy_script
        prefix = 'if ! printf \'%s\\n\' "$MANIFESTS" | grep -'
        assert prefix + 'q "cloud-sql-proxy"; then' in deploy_script
        assert prefix + 'qF "$SQL_CONNECTION_NAME"; then' in deploy_script

    def test_each_half_reports_its_own_distinct_failure(self, deploy_script):
        """Two separate assertions are only an improvement if they diagnose separately."""
        assert "The manifests run no cloud-sql-proxy container." in deploy_script
        assert "The manifests name no cloud-sql-proxy target matching" in deploy_script

    def test_the_port_eighty_listener_is_mandatory(self, deploy_script):
        """Its absence was accepted and merely noted, on the reasoning that Terraform created
        it in a separately-gated second stage. Terraform now creates it unconditionally, so
        accepting absence accepted a real defect: nothing answers on http:// at all, which a
        client sees as a connection failure rather than an upgrade to TLS."""
        assert (
            "The port-80 forwarding rule excel-app-http-forwarding-rule does not exist."
            in deploy_script
        )
        assert "does not exist yet" not in deploy_script
        assert "Apply Terraform before running this script." in deploy_script

    def test_the_port_eighty_listener_is_proven_to_redirect(self, deploy_script):
        """The table verified that ``excel-app-http-proxy`` carries the redirect URL map, and
        separately that a rule listened on 80 - never that the rule pointed at that proxy. A
        port-80 rule aimed at the HTTPS proxy passed every check while serving the application
        in cleartext."""
        assert (
            "forwarding-rules|excel-app-http-forwarding-rule|--global"
            "|target|/targetHttpProxies/excel-app-http-proxy"
        ) in deploy_script
        assert (
            "forwarding-rules|excel-app-http-forwarding-rule|--global|portRange|80"
        ) in deploy_script

    def test_the_proxy_targets_are_matched_on_a_path_segment(self, deploy_script):
        """A bare name is ambiguous between the two proxies; the path segment is not."""
        assert "|target|/targetHttpsProxies/excel-app-https-proxy" in deploy_script
        assert "|target|excel-app-https-proxy" not in deploy_script

    def test_the_script_declares_exactly_one_mutation_boundary(self, deploy_script):
        """Everything that reasons about "before the mutations" indexes the FIRST occurrence of
        this banner, and the operator reads the "Preflight passed" line as the point after which
        state changes. A second copy of either makes both statements false while every
        occurrence-based assertion still passes: a duplicated block introduced a second banner
        that was itself false - two mutations had already run above it - and a second
        "Preflight passed", so the script announced a read-only preflight twice and re-ran
        preflight assertions after publishing.

        Pinned as counts rather than presence, which is the difference that catches it.
        """
        assert deploy_script.count("# MUTATIONS - nothing above") == 1
        assert deploy_script.count("Preflight passed. Beginning deployment.") == 1

    def test_the_script_fails_on_an_unset_variable_and_a_broken_pipe(self, deploy_script):
        """With only ``set -e``, a mistyped variable expanded to the empty string - so a check
        comparing an observed value against an empty expectation passed - and a pipeline was
        judged by its LAST command, so a failing read feeding a filter that succeeded on no
        input passed too. Both are silent, and both report a control that was never verified."""
        assert "set -euo pipefail" in deploy_script
        # Every tolerated non-match is confined to the one command that legitimately exits 1,
        # so a blanket `|| true` over a whole pipeline cannot creep back in.
        assert "{ grep -i '^location:' || true; }" in deploy_script

    def test_the_script_can_run_unattended(self, deploy_script):
        """``gcloud auth login`` opened a browser prompt unconditionally and blocked, so the
        preflight could not run in CI or from an operator's service account - and a preflight
        nobody can run verifies nothing."""
        assert "gcloud auth list --filter=status:ACTIVE" in deploy_script
        # Counted as INVOCATIONS, not as occurrences of the text: the command is discussed in
        # two comments as well. An unconditional invocation sits at the start of its line; the
        # guarded one is indented inside the `if`.
        invocations = [
            line
            for line in deploy_script.splitlines()
            if line.strip() == "gcloud auth login" and not line.startswith("#")
        ]
        assert len(invocations) == 1, invocations
        assert invocations[0].startswith(" "), (
            "gcloud auth login is invoked unconditionally: %r" % invocations[0]
        )

    def test_the_shell_command_inputs_state_their_trust_boundary(self, deploy_script):
        """Both values are executed with ``eval``, so whatever supplies them runs commands with
        this script's credentials. The boundary is documented where the value is assigned, not
        only where it is used."""
        assert "TRUST BOUNDARY" in deploy_script
        assert deploy_script.count("eval \"$DB_MIGRATION_COMMAND\"") == 1
        assert deploy_script.count("eval \"$POST_DEPLOY_TEST_COMMAND\"") == 1

    @staticmethod
    def _mutation_order(deploy_script):
        body = deploy_script[deploy_script.index("# MUTATIONS - nothing above"):]
        return {
            "rules": body.index("firebase deploy --only firestore:rules"),
            "frontend": body.index("gsutil -m rsync"),
            "image": body.index("docker push"),
            "backend": body.index("kubectl apply"),
        }

    def test_the_authorization_rules_are_deployed_before_anything_is_published(
        self, deploy_script
    ):
        """The rules were deployed LAST, after the frontend, the image and the manifests. The
        browser reaches Firestore directly, so on a first deployment a freshly published client
        was live against no rules at all - unrestricted access to every workbook document."""
        order = self._mutation_order(deploy_script)
        assert order["rules"] < order["frontend"]
        assert order["rules"] < order["image"]
        assert order["rules"] < order["backend"]

    def test_deploying_rules_first_cannot_break_the_running_backend(self, deploy_script):
        """Why the reorder is safe, asserted rather than assumed: the reason is recorded at the
        point of the decision, because a future reorder would otherwise look harmless."""
        start = deploy_script.index("# MUTATIONS - nothing above")
        end = deploy_script.index("# Update Google Cloud Firestore security rules")
        banner = deploy_script[start:end]
        assert "server client library" in banner
        assert "bypass" in banner

    def test_the_deployment_verifies_the_headers_it_claims(self, deploy_script):
        """The preflight confirmed the backend bucket was CONFIGURED with the headers. Nothing
        confirmed a client received them, and the script reported success either way.

        The live response is now read and each header compared, and this asserts the comparison
        is of VALUES. An earlier version of this test pinned the name-only form
        ``grep -qi "^${expected_header}:"``, which is satisfied by a response carrying all six
        names with values that protect nothing - ``X-Frame-Options: ALLOWALL``,
        ``max-age=1``, ``Referrer-Policy: unsafe-url`` - and the script then printed the headers
        as confirmed. Pinning that form made the weaker check the contract, so it is replaced
        rather than kept alongside.
        """
        for header in _expected_header_names():
            assert header in deploy_script
        assert "response_header_value()" in deploy_script
        assert "canonical_header_value() {" in deploy_script
        assert 'if [ "$served_value" != "$(lower_case "$(canonical_header_value ' in deploy_script

    def test_the_canonical_header_values_the_script_checks_are_the_terraform_ones(
        self, deploy_script
    ):
        """The script's canonical values must be the ones Terraform configures, or the
        comparison it now makes would refuse a correctly deployed edge."""
        main_tf = (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")
        for name in (
            "Strict-Transport-Security",
            "X-Frame-Options",
            "X-Content-Type-Options",
            "Referrer-Policy",
            "Permissions-Policy",
        ):
            entry = re.search(
                r'"%s: ([^"]+)"' % re.escape(name), main_tf
            )
            assert entry, name
            assert "printf '%%s' '%s'" % entry.group(1) in deploy_script, name

    def test_the_deployment_verifies_the_policy_directives_that_carry_protection(
        self, deploy_script
    ):
        """A policy stripped of ``frame-ancestors 'none'`` or widened with ``'unsafe-inline'``
        keeps its header name, so the name is not what has to be checked."""
        for directive in (
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "script-src 'self'",
        ):
            assert deploy_script.count('    "%s"' % directive) == 2, directive
        assert "csp_has_directive() {" in deploy_script

    def test_the_deployment_verifies_the_plaintext_redirect(self, deploy_script):
        assert "instead of a permanent redirect" in deploy_script
        assert "which is not an https URL" in deploy_script

    def test_the_deployment_verifies_an_unauthenticated_request_is_refused(
        self, deploy_script
    ):
        """The assertion the whole authentication change exists to make true, and the one an
        operator is least able to make by eye."""
        assert 'if [ "$API_PROBE_STATUS" != "401" ]; then' in deploy_script
        assert "not 401" in deploy_script

    def test_the_readiness_retry_cannot_be_mistaken_for_a_missing_control(
        self, deploy_script
    ):
        """An unavailable backend answers 502 or 503 through the load balancer. Reporting that
        as 'authentication is not enforced' would be wrong, so the rollout is given a bounded
        chance to become ready and only then asserted."""
        assert "000|502|503|504)" in deploy_script
        assert 'API_PROBE_ATTEMPT" -le 10' in deploy_script

    def test_a_failed_verification_prints_a_rollback_procedure(self, deploy_script):
        """The release is already live at this point, so the operator needs the procedure in
        front of them rather than a reference to it."""
        assert "POST-DEPLOYMENT VERIFICATION FAILED" in deploy_script
        assert "ROLLBACK" in deploy_script
        assert "kubectl rollout restart deployment" in deploy_script
        assert "git revert --no-edit HEAD" in deploy_script

    def test_the_rollback_states_the_two_reverts_that_are_unsafe_alone(self, deploy_script):
        """Reverting file_storage.py without the Terraform bucket change restores a
        make_public() call that now fails with HTTP 400, and rolling back the Firestore rules
        to fix a client-side symptom reopens every document."""
        assert "Do NOT roll back the Firestore rules" in deploy_script
        assert "Do NOT revert file_storage.py on its own" in deploy_script
        assert "HTTP 400" in deploy_script

    def test_there_is_no_image_rollback_and_the_script_says_so(self, deploy_script):
        """The image is tagged :latest only and this run overwrote the previous one, so an
        operator told to 'roll back the image' would be looking for a tag that never existed."""
        assert "There is no image rollback" in deploy_script
        assert ":latest" in deploy_script

    def test_the_verification_tooling_is_checked_before_any_mutation(self, deploy_script):
        """A missing tool discovered after publication would silently skip the verification,
        which is the only evidence the controls are in effect."""
        preflight = deploy_script[: deploy_script.index("# MUTATIONS - nothing above")]
        assert "command -v curl" in preflight

    def test_no_post_deployment_test_placeholder_remains(self, deploy_script):
        """The verification replaced a HUMAN ASSISTANCE NEEDED stub, so the stub must be gone
        rather than sitting alongside real checks."""
        assert "Please specify the test runner" not in deploy_script


class TestCloudIdentityTopology:
    """C2: the runtime had no declared identity, and the pods no way to assume one."""

    @pytest.fixture
    def terraform(self):
        return (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")

    @pytest.fixture
    def variables(self):
        return (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "variables.tf"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _block(source, header):
        start = source.index(header)
        return source[start:source.index("\n}\n", start)]

    def test_the_cluster_declares_its_workload_identity_pool(self, terraform):
        """Only the node pool's GKE_METADATA mode was present. Without the pool on the cluster
        there is no principal for a binding to name, so pods hold no Google identity at all and
        the first Settings() construction fails on Secret Manager."""
        cluster = self._block(terraform, 'resource "google_container_cluster" "primary"')
        assert "workload_identity_config" in cluster
        assert 'workload_pool = "${var.project_id}.svc.id.goog"' in cluster

    def test_the_node_pool_still_serves_the_pod_identity(self, terraform):
        """The other half: GKE_METADATA is what makes the metadata server answer with the
        pod's own identity rather than the node service account's."""
        assert "GKE_METADATA" in terraform

    def test_the_kubernetes_service_account_is_bound_to_the_runtime(self, terraform):
        binding = self._block(
            terraform,
            'resource "google_service_account_iam_member" "api_runtime_workload_identity"',
        )
        assert "roles/iam.workloadIdentityUser" in binding
        assert "google_service_account.url_signer.name" in binding
        assert (
            "serviceAccount:${var.project_id}.svc.id.goog"
            "[${var.kubernetes_namespace}/${var.kubernetes_service_account}]" in binding
        )

    def test_the_runtime_and_the_signer_are_one_account(self, terraform):
        """file_storage.py signs as the address its own ambient credentials report, so a
        separate signer account could never be the account a signature is minted for."""
        assert 'resource "google_service_account" "api_runtime"' not in terraform
        assert 'resource "google_service_account" "url_signer"' in terraform
        assert "google_service_account.api_runtime" not in terraform

    def test_the_signing_role_is_held_on_the_account_it_signs_as(self, terraform):
        """C2: the account that signs is the RESOURCE and the caller is the MEMBER, and here
        they are the same account - so the binding is what authorizes signing at all rather
        than a redundancy. The permission no IAM Condition can narrow therefore lands on the
        identity that also reads the secrets, which is why that identity holds no
        project-level grant beyond the four roles the application provably needs."""
        grant = self._block(
            terraform,
            'resource "google_service_account_iam_member" "url_signer_token_creator"',
        )
        assert "service_account_id = google_service_account.url_signer.name" in grant
        assert "roles/iam.serviceAccountTokenCreator" in grant
        assert "member             = google_service_account.url_signer.member" in grant

    def test_one_bucket_grant_covers_the_write_and_the_signature(self, terraform):
        """objectAdmin is what the uploads path needs - write, read back and delete an
        unsignable generation - and because the signature is minted for this same account it
        is also the read authority a signed GET resolves against, so there is one grant.
        It is scoped to the one bucket and is never a project-level role."""
        grant = self._block(
            terraform,
            'resource "google_storage_bucket_iam_member" "user_uploads_signer_object_admin"',
        )
        assert "roles/storage.objectAdmin" in grant
        assert "google_service_account.url_signer.member" in grant
        assert "bucket = google_storage_bucket.user_uploads.name" in grant
        assert 'resource "google_storage_bucket_iam_member" "user_uploads_runtime_object_admin"' not in (
            terraform
        )

    def test_the_runtime_may_read_a_firebase_user_record(self, terraform):
        """C2: verify_id_token is called with check_revoked=True, whose lookup needs
        firebaseauth.users.get. Without the role every verification fails the lookup rather
        than the signature, so nothing can authenticate."""
        assert "roles/firebaseauth.viewer" in terraform

    def test_the_revocation_check_that_needs_that_role_is_still_made(self):
        source = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        assert "check_revoked=True" in source

    @pytest.mark.parametrize(
        "name",
        ["signer_service_account", "kubernetes_namespace", "kubernetes_service_account"],
    )
    def test_every_identity_variable_is_actually_used(self, terraform, name):
        """C2: each was declared, validated and described as governing resources - and
        referenced nowhere, so the topology they described did not exist. There is no
        runtime_service_account input, because the runtime is the account named here."""
        assert "var.%s" % name in terraform
        assert "runtime_service_account" not in terraform

    def test_the_dead_cutover_variable_is_gone(self, variables, terraform):
        """It gated a redirect listener that is now created unconditionally, so it described a
        two-stage apply the configuration no longer performs."""
        assert "https_cutover_enabled" not in code_only(variables)
        assert "https_cutover_enabled" not in code_only(terraform)

    def test_the_redirect_listener_exists_unconditionally(self, terraform):
        """Which is why the variable is gone: a deployment that omitted the redirect left port
        80 with no listener at all, so the plaintext URL failed to connect instead of
        upgrading."""
        rule = self._block(
            terraform,
            'resource "google_compute_global_forwarding_rule" "excel_app_http"',
        )
        assert "count" not in rule
        assert 'port_range            = "80"' in rule
        assert "google_compute_target_http_proxy.excel_app_redirect" in rule


class TestSupplyChainGates:
    """H7 and H10: the CI job that exists to detect supply-chain risk was taking it on.

    Three separate problems, and none of them was a missing check - each was a check that
    could not report. Two steps carried ``continue-on-error: true`` so no outcome failed the
    build, one of them installed seven guessed package versions in the process, every action
    was referenced by a mutable tag, and fourteen advisories were suppressed by a bare list
    of identifiers that named neither a package nor a reason.
    """

    @pytest.fixture
    def workflow(self):
        return (
            REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

    @pytest.fixture
    def security_job(self, workflow):
        import yaml

        return yaml.safe_load(workflow)["jobs"]["security-checks"]

    @pytest.fixture
    def security_job_source(self, workflow):
        """Raw text of the security-checks job only, comments included.

        Scoped rather than whole-file on purpose. The pre-existing ``build`` job still
        references ``actions/checkout@v2``, and the Agent Action Plan places that job
        outside this change set (0.6.1, "Existing jobs untouched"), so a whole-file scan
        would assert against a job this work is not permitted to modify.
        """
        return workflow[workflow.index("  security-checks:"):]

    @staticmethod
    def _step(job, needle):
        for step in job["steps"]:
            if needle in str(step.get("name", "")):
                return step
        raise AssertionError("no step whose name contains %r" % needle)

    # --- H7: no step may be incapable of failing -------------------------------------
    def test_no_security_step_tolerates_its_own_failure(self, security_job):
        """``continue-on-error: true`` made a step decorative: a real high-severity
        advisory and the known blocker produced the same green result, and nothing would
        notice when the blocker was fixed and the step began doing nothing useful."""
        tolerated = [
            step.get("name")
            for step in security_job["steps"]
            if step.get("continue-on-error")
        ]
        assert tolerated == []

    def test_every_action_is_pinned_to_a_commit_sha(self, security_job):
        """A tag is a mutable pointer its own maintainer can move. This job holds the
        repository contents and the audit verdict, so it is exactly the step an attacker
        who could move a tag would want to reach."""
        used = [step["uses"] for step in security_job["steps"] if step.get("uses")]
        assert used, "the job must still check out the repository"
        for reference in used:
            _, _, ref = reference.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", ref), reference

    def test_each_pin_names_the_version_it_corresponds_to(self, security_job_source):
        """A bare SHA is unreviewable. The version beside it is what makes the pin
        checkable with ``git ls-remote --tags``."""
        annotated = [
            line for line in security_job_source.splitlines() if "uses: actions/" in line
        ]
        assert annotated
        for line in annotated:
            assert re.search(r"#\s*v\d+\.\d+\.\d+", line), line

    def test_the_audit_tool_itself_is_pinned(self, security_job):
        """Unpinned, the meaning of the gate could change between two runs of the same
        commit: a newer release can add a data source or change an exit code, and the
        exception list was validated against one exact version."""
        step = self._step(security_job, "Install the dependency audit tool")
        assert re.search(r"pip install pip-audit==\d+\.\d+(\.\d+)?", step["run"])

    def test_the_undeclared_packages_are_installed_at_exact_versions(self, workflow):
        """``npm install --no-save`` on versions present in no manifest is the
        supply-chain risk this job exists to detect, so every one of them is pinned to an
        exact version rather than to a range, and they go in ONE command because
        ``--no-save`` prunes what a previous ``--no-save`` added. The install is required
        rather than removable: it is what makes the interceptor gate runnable while
        ``frontend/package.json`` - which this change set may not edit - declares neither
        ``react-scripts`` nor the five other packages the sources import."""
        script = code_only(workflow)
        installs = [line for line in script.splitlines() if "--no-save" in line]
        assert len(installs) == 1, installs
        block = script[script.index("--no-save") :].split("\n\n")[0]
        for pin in (
            "react-scripts@5.0.1",
            "@reduxjs/toolkit@1.9.7",
            "react-router-dom@5.3.4",
            "firebase@10.14.1",
            "mathjs@11.12.0",
            "date-fns@2.30.0",
        ):
            assert pin in block, pin
        assert not re.search(r"@[\^~]", block), block

    def test_the_interceptor_is_still_covered_somewhere(self):
        """Removing the step is only correct because the assertion moved rather than
        disappeared."""
        assert "class TestApiClientContract" in (
            Path(__file__).read_text(encoding="utf-8")
        )

    # --- H7: the frontend audit self-promotes ----------------------------------------
    def test_the_frontend_audit_distinguishes_the_states_it_can_be_in(self, security_job):
        """Not ``continue-on-error`` by another name: it asserts WHICH state it is in, so a
        partially-fixed manifest fails rather than passing while unable to audit."""
        run = self._step(security_job, "Audit frontend dependencies")["run"]
        assert 'if [ "$MISSING" = "" ] && [ "$LOCKFILE" = "True" ]; then' in run
        assert "npm --prefix frontend audit --audit-level=high" in run
        assert '"$MISSING" = "firebase,react-scripts" ]' in run
        assert "exit 1" in run

    def test_the_frontend_audit_reads_committed_state_rather_than_the_filesystem(
        self, security_job
    ):
        """What blocks the audit is that no lock file is committed, and the documented local
        setup writes an untracked one, so a filesystem probe made a local run of this job
        disagree with CI - where the checkout carries tracked files only.

        The three probe results are asserted rather than only the git call: ``Untracked`` has
        to be its own value, or the failure branch cannot say which state it is refusing.
        """
        run = self._step(security_job, "Audit frontend dependencies")["run"]
        assert (
            "git ls-files --error-unmatch frontend/package-lock.json" in run
        ), "the probe does not read committed state"
        assert "LOCKFILE=True" in run
        assert "LOCKFILE=Untracked" in run
        assert "LOCKFILE=False" in run
        # An untracked lock file takes the documented-blocker branch, not the failure branch.
        assert '[ "$LOCKFILE" != "True" ]' in run
        assert '[ "$LOCKFILE" = "False" ]' not in run

    def test_the_frontend_audit_names_both_halves_of_the_fix(self, security_job):
        """``npm audit`` needs the declarations and the lock file together, so a message
        naming one half sends a developer to do work that leaves the step still refusing."""
        run = self._step(security_job, "Audit frontend dependencies")["run"]
        remedy = run[run.index("in neither the documented blocked state") :]
        assert "declare firebase and react-scripts" in remedy
        assert "commit frontend/package-lock.json" in remedy.lower()
        assert "not committed" in remedy

    def test_the_frontend_audit_becomes_a_gate_without_editing_the_workflow(
        self, security_job
    ):
        """The promotion has to be automatic. A step that needs a human to remember to
        remove an exception stays exempt for as long as nobody remembers."""
        run = self._step(security_job, "Audit frontend dependencies")["run"]
        promote = run.index("The documented blocker is cleared")
        audit = run.index("npm --prefix frontend audit")
        assert promote < audit

    # --- H10: every exception is justified individually -------------------------------
    def test_the_advisory_exceptions_are_the_committed_fourteen(self, security_job):
        run = self._step(security_job, "committed advisory baseline")["run"]
        identifiers = re.findall(r"--ignore-vuln\s+(\S+)", run)
        assert len(identifiers) == 14
        assert len(set(identifiers)) == 14

    def test_every_exception_carries_its_own_justification(self, security_job):
        """A single shared comment over a block of identifiers could not be checked:
        nothing tied an identifier to a package or a reason, so an entry whose fix had
        become reachable was indistinguishable from the rest and got renewed forever."""
        run = self._step(security_job, "committed advisory baseline")["run"]
        lines = run.splitlines()
        for index, line in enumerate(lines):
            if "--ignore-vuln" not in line:
                continue
            # Walk back over the contiguous comment block introducing this exception.
            justification = []
            cursor = index - 1
            while cursor >= 0 and lines[cursor].strip().startswith("#"):
                justification.insert(0, lines[cursor].strip().lstrip("#").strip())
                cursor -= 1
            text = " ".join(justification)
            identifier = re.search(r"--ignore-vuln\s+(\S+)", line).group(1)
            assert text, "%s has no justification above it" % identifier
            # A justification has to name the package and say something about the fix.
            assert re.match(r"^[a-z0-9_.-]+ ", text), "%s: %r" % (identifier, text)
            assert "->" in text, "%s does not state a fix version: %r" % (
                identifier,
                text,
            )

    def test_every_exception_states_why_the_fix_cannot_be_taken(self, security_job):
        """Either the fix requires an interpreter this project does not run, or no fix
        exists. Anything else is not an exception, it is an outstanding upgrade."""
        run = self._step(security_job, "committed advisory baseline")["run"]
        blocks = re.split(r"--ignore-vuln\s+\S+", run)[:-1]
        assert len(blocks) == 14
        for block in blocks:
            assert (
                "requires Python >=3.10" in block or "NO PUBLISHED FIX" in block
            ), block[-200:]

    def test_the_manifest_declares_nothing_no_module_imports(self):
        """A pin with no importer ships its transitive surface into the runtime image for no
        control, and the annotation beside it said the code used it - so the manifest asserted
        the opposite of what a passing test in this file already proved. Both halves are pinned
        here: the package is absent from the manifest AND absent from the code.
        """
        manifest = (
            REPOSITORY_ROOT / "backend" / "requirements.txt"
        ).read_text(encoding="utf-8")
        declared = re.findall(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)", manifest)
        assert "slowapi" not in declared, "slowapi is pinned but nothing imports it"
        source = (BACKEND_APP / "core" / "rate_limit.py").read_text(encoding="utf-8")
        assert "slowapi" not in code_only(source)
        # Every entry is an exact pin, so the resolved set stays knowable.
        pins = [
            line
            for line in manifest.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert all("==" in pin for pin in pins), [p for p in pins if "==" not in p]

        # The header's own three counts, measured by the rule the header states. They were
        # 23 direct and 89 total against a file carrying 22 and 88: withdrawing slowapi from
        # the direct section left the figures behind. A manifest that miscounts itself is
        # the first thing a reader checks and the first reason they stop trusting the rest of
        # its annotations, including the security floor and the audit baseline.
        lines = manifest.splitlines()
        direct_at = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("# DIRECT REQUIREMENTS")
        )
        transitive_at = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("# TRANSITIVE GRAPH")
        )
        counted = {
            "direct": sum(
                1 for line in lines[direct_at:transitive_at] if line[:1].isalnum()
            ),
            "transitive": sum(
                1 for line in lines[transitive_at:] if line[:1].isalnum()
            ),
        }
        counted["total"] = counted["direct"] + counted["transitive"]
        assert counted["total"] == len(pins), (counted, len(pins))
        assert "%d direct pins and %d transitive" % (
            counted["direct"],
            counted["transitive"],
        ) in manifest, counted
        assert "pins, %d in all," % counted["total"] in manifest, counted

    def test_the_infrastructure_configuration_is_gated_on_loading(self, security_job):
        """Every cloud-side control is delivered only by ``terraform apply``, and the shape
        assertions in this file read the .tf files as TEXT - so a configuration full of correct
        arguments that Terraform refuses to LOAD passes all of them while delivering none of the
        controls. That is exactly what an unescaped interpolation in a variable description did.

        The step is also required to be scoped, because validating the directory as it stands
        fails on a pre-existing defect in a file outside this change set, and a gate that is red
        on every run is a gate the team learns to ignore.
        """
        step = self._step(security_job, "Validate the Terraform configuration")
        run = step["run"]
        assert "continue-on-error" not in step
        assert "terraform fmt -check" in run
        assert "validate" in run
        # Scoped to the two files this change set owns...
        assert "cp main.tf variables.tf" in run
        # ...while still asserting the pre-existing failure has not changed, so repairing it is
        # noticed rather than silently tolerated forever.
        assert "outputs" in run

    def test_the_one_permanent_exception_is_marked_as_such(self, security_job):
        """ecdsa is the only entry the runtime upgrade will not clear, which makes it the
        only standing decision here rather than a deferral - and the only one whose review
        question is 'has a fix appeared' rather than 'has the runtime moved'."""
        run = self._step(security_job, "committed advisory baseline")["run"]
        index = run.index("--ignore-vuln PYSEC-2026-1325")
        block = run[run.rindex("# ecdsa", 0, index):index]
        assert "NO PUBLISHED FIX" in block
        assert "python-jose" in block
        assert "firebase-admin" in block

    @pytest.fixture
    def audit_step_source(self, workflow):
        """The raw text of the audit step, INCLUDING the YAML comments above it.

        ``security_job`` cannot serve here: yaml.safe_load discards comments, so the
        preamble explaining the list is invisible in the parsed step.
        """
        start = workflow.index("# Delta mode.")
        end = workflow.index("- name: Audit frontend dependencies")
        return workflow[start:end]

    def test_the_exception_list_names_its_own_exit_criterion(self, audit_step_source):
        """Thirteen of fourteen share one cause. Recording that makes the list a
        consequence of the runtime decision rather than fourteen separate ones."""
        assert "exit criterion" in audit_step_source

    def test_the_python_version_claims_are_reproducible(self, audit_step_source):
        """The claims were verified by resolving each fix version, not read from a
        changelog, so the command that verifies them is recorded."""
        assert "uv pip install" in audit_step_source
        assert "--dry-run" in audit_step_source

    # --- H10: the runtime the exceptions depend on ------------------------------------
    def test_the_audit_runs_on_the_runtime_the_manifest_was_resolved_against(
        self, security_job
    ):
        """A newer interpreter resolves a different transitive set, so the audit would
        describe a dependency tree the runtime does not install - and would silently
        clear exceptions that are still live in production."""
        step = self._step(security_job, "Set up Python 3.9")
        assert step["with"]["python-version"] == "3.9"


class TestRepositoryHygieneControls:
    """No .gitignore existed, so nothing stood between ``git add .`` and a committed
    secret. A credential that reaches history stays reachable after it is deleted from the
    working tree; the remedy is rotation, not another commit."""

    @pytest.fixture
    def ignore_rules(self):
        path = REPOSITORY_ROOT / ".gitignore"
        assert path.is_file(), "the repository must carry a root .gitignore"
        return path.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "pattern",
        [".env", ".env.*", "*.pem", "*.key", "*.tfstate", "*.tfvars"],
    )
    def test_secret_bearing_paths_are_ignored(self, ignore_rules, pattern):
        assert pattern in ignore_rules.splitlines()

    def test_the_documented_template_is_still_committable(self, ignore_rules):
        """.env.example is the file .env.example's own documentation refers to, and
        README and the onboarding guide send a new developer to copy it. Ignoring it
        alongside .env would remove the only record of what must be configured."""
        assert "!.env.example" in ignore_rules.splitlines()
        assert (REPOSITORY_ROOT / ".env.example").is_file()

    def test_the_frontend_lockfile_is_not_ignored(self, ignore_rules):
        """Its absence is a recorded residual and CI needs it, so it has to be
        committable the moment it is generated correctly. An over-broad ignore rule would
        make the residual permanent."""
        assert "frontend/package-lock.json" not in [
            line.strip() for line in ignore_rules.splitlines()
            if not line.strip().startswith("#")
        ]
        assert "package-lock.json" not in [
            line.strip() for line in ignore_rules.splitlines()
            if not line.strip().startswith("#")
        ]

    def test_no_secret_is_tracked_today(self):
        """The rules only prevent the next mistake. This asserts the current state is
        clean, so the control is not being added over an existing leak."""
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPOSITORY_ROOT),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert tracked, "git ls-files returned nothing"
        offenders = [
            path
            for path in tracked
            if path.rsplit("/", 1)[-1] == ".env"
            or path.endswith((".pem", ".key", ".p12", ".pfx", ".tfstate"))
        ]
        assert offenders == []


class TestDocumentedFactsMatchTheCode:
    """M5: the documentation stated counts and behaviours the code contradicted.

    Prose drifts silently. Every claim asserted here is one a reader would act on — a field
    count they would reconcile against `.env.example`, a control they would assume is in
    place, a test count they would compare a run against — so each one is pinned to the thing
    it describes rather than to another document.
    """

    @pytest.fixture
    def settings_field_names(self):
        tree = _parse(BACKEND_APP / "core" / "config.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Settings":
                return [
                    item.target.id
                    for item in node.body
                    if isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name)
                ]
        raise AssertionError("no Settings class in config.py")

    @pytest.fixture
    def env_example(self):
        return (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

    @pytest.fixture
    def documents(self):
        return {
            "onboarding": REPOSITORY_ROOT / "documentation" / "Developer Onboarding.md",
            "matrix": REPOSITORY_ROOT
            / "documentation"
            / "Security Traceability Matrix.md",
            "decisions": REPOSITORY_ROOT / "documentation" / "Security Decision Log.md",
            "security": REPOSITORY_ROOT / "SECURITY.md",
            "readme": REPOSITORY_ROOT / "README.md",
        }

    def test_every_settings_field_is_documented(self, settings_field_names, env_example):
        """`.env.example` calls itself the authoritative reference, so an undocumented field
        is a configuration value nobody knows to set."""
        documented = set(re.findall(r"(?m)^([A-Za-z_][A-Za-z0-9_]*)=", env_example))
        missing = [name for name in settings_field_names if name not in documented]
        assert missing == []

    def test_the_documented_field_count_is_the_real_one(
        self, settings_field_names, documents
    ):
        """The count was 21 while the class carried 22, which is exactly the kind of claim a
        reader reconciles by hand and then distrusts the whole document over."""
        onboarding = documents["onboarding"].read_text(encoding="utf-8")
        assert "%d `Settings` fields" % len(settings_field_names) in onboarding

    def test_no_document_still_claims_there_is_no_gitignore(self, documents):
        """It was true, it is no longer, and the instruction it justified — add `.env` to
        `.git/info/exclude` yourself — now reads as though the control is absent."""
        assert (REPOSITORY_ROOT / ".gitignore").is_file()
        for name, path in documents.items():
            body = path.read_text(encoding="utf-8")
            for claim in (
                "has no `.gitignore`",
                "there is no root `.gitignore`",
                "NO root `.gitignore`",
            ):
                # The Decision Log deliberately quotes its own superseded text, marked as
                # such, because the reasoning it records is what explains the reversal.
                if name == "decisions":
                    continue
                assert claim not in body, "%s: %r" % (name, claim)

    def test_no_document_claims_revocation_is_unchecked(self, documents):
        """`verify_id_token` is called with `check_revoked=True`, so a document saying
        otherwise understates the control and invites someone to 'add' it."""
        source = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        assert "check_revoked=True" in source
        for name, path in documents.items():
            assert (
                "revocation is not checked" not in path.read_text(encoding="utf-8").lower()
            ), name

    def test_the_firestore_verification_caveat_is_published(self, documents):
        """QA F-E: every Firestore denial on the browser's streaming transport comes back
        HTTP 200 with the refusal inside the payload, so a check keying on status reports a
        confident PASS against wide-open rules. The committed harness is safe because it drives
        the REST surface, where a denial really is 403 - and that distinction is the thing worth
        writing down, since 'assert on the status' is the obvious way to write such a check.
        """
        policy = documents["security"].read_text(encoding="utf-8")
        onboarding = documents["onboarding"].read_text(encoding="utf-8")
        for body in (policy, onboarding):
            assert "PERMISSION_DENIED" in body
            assert "cause.code" in body
        assert "never by status alone" in policy
        assert "Never assert on the HTTP status of a browser request" in onboarding

    def test_the_emulator_diagnostic_is_recorded_as_a_non_defect(self, documents):
        """QA F-D: the hypothesis that this line indicated a rules bug was formed and then
        disproved. Recording the disproof is what stops it being investigated a third time."""
        policy = documents["security"].read_text(encoding="utf-8")
        assert "evaluation error at L132:22" in policy
        assert "disproved" in policy
        onboarding = documents["onboarding"].read_text(encoding="utf-8")
        assert "is not a failure" in onboarding

    def test_the_documentation_ui_trade_off_is_disclosed(self, documents):
        """The enforcing policy blanks `/docs`, which reads as a broken API to an operator who
        does not know the policy is doing it on purpose."""
        policy = documents["security"].read_text(encoding="utf-8")
        assert "/docs" in policy
        assert "SwaggerUIBundle" in policy
        assert "/openapi.json" in policy

    def test_the_access_log_query_string_hazard_is_disclosed(self, documents):
        """Latent, not active: the client sends the credential in a header and a query-parameter
        credential is refused. It becomes real when an integration passes a secret in a URL."""
        policy = documents["security"].read_text(encoding="utf-8")
        assert "query string" in policy
        assert "redacts it" in policy

    def test_revocation_checking_is_recorded_as_a_deviation(self, documents):
        """QA F-B: the *decision* to check revocation was logged three times over, but never
        as a departure from a plan that specifies default verification and calls revocation
        checking a tunable - so a reader reconciling code against plan found an unexplained
        difference, which the explainability rule treats as a defect in itself.

        Also pins the non-configurability, because the finding's first suggested remedy was a
        setting and the reason it was declined - the authorised configuration surface - is the
        kind of constraint that stops being obvious once the code is a year old.
        """
        decisions = documents["decisions"].read_text(encoding="utf-8")
        assert "| D106 |" in decisions
        row = decisions[decisions.index("| D106 |") :]
        row = row[: row.index("\n")]
        assert "check_revoked" in row
        assert "deviation" in row.lower()
        from backend.app.core.config import Settings

        assert "firebase_check_revoked" not in Settings.__fields__
        policy = documents["security"].read_text(encoding="utf-8")
        assert "not configurable" in policy
        assert "D106" in policy

    def test_the_documented_throttling_scope_matches_the_limiter(self, documents):
        """slowapi's `default_limits` were counted per endpoint function and exempted a
        path matching no route, so describing the ceiling as global was the documentation
        agreeing with the intent and not with the code. The ceiling is now metered against
        one fixed bucket in pure ASGI, before routing, which is what makes the documented
        scope true."""
        from backend.app.core import rate_limit

        source = (BACKEND_APP / "core" / "rate_limit.py").read_text(encoding="utf-8")
        assert "slowapi" not in code_only(source)
        assert rate_limit._ALL_REQUESTS_SCOPE
        assert "default_limits" not in source
        security = documents["security"].read_text(encoding="utf-8")
        assert "across every route" in security

    @staticmethod
    def _parametrize_rows(argvalues, owner):
        """Return how many cases one ``parametrize`` argvalues expression contributes.

        Only the forms this module actually uses are handled, and anything else raises rather
        than being guessed at, so a new form cannot silently make the count approximate. A name
        is resolved against the enclosing class first and the module second, which is the order
        Python itself used when the decorator was evaluated. Nothing is ``eval``'d.
        """
        if isinstance(argvalues, (ast.List, ast.Tuple)):
            return len(argvalues.elts)
        if isinstance(argvalues, ast.Name):
            if owner is not None and hasattr(owner, argvalues.id):
                return len(getattr(owner, argvalues.id))
            return len(globals()[argvalues.id])
        if isinstance(argvalues, ast.BinOp) and isinstance(argvalues.op, ast.Add):
            return TestDocumentedFactsMatchTheCode._parametrize_rows(
                argvalues.left, owner
            ) + TestDocumentedFactsMatchTheCode._parametrize_rows(argvalues.right, owner)
        if isinstance(argvalues, ast.Call) and getattr(argvalues.func, "id", None) == "range":
            return len(range(*[argument.value for argument in argvalues.args]))
        raise AssertionError(
            "unhandled parametrize argvalues form: %s" % ast.dump(argvalues)[:120]
        )

    @classmethod
    def _case_multiplier(cls, decorators, owner):
        """Product of every ``parametrize`` on one function or class."""
        multiplier = 1
        for decorator in decorators:
            if (
                isinstance(decorator, ast.Call)
                and getattr(decorator.func, "attr", None) == "parametrize"
            ):
                multiplier *= cls._parametrize_rows(decorator.args[1], owner)
        return multiplier

    def test_the_documented_test_count_matches_this_module(self, documents):
        """Measured from this module's own source, and asserted as an EQUALITY.

        The earlier form compared the published figure against the number of test FUNCTIONS with
        ``>=``, which parametrised cases always exceed - so the figure could drift upward
        without limit and still pass, and the docstring's claim that it was measured rather than
        typed by hand was not true of the number actually published. It also read only two of
        the five documents that quote the figure, so three could disagree unnoticed.

        Here every ``parametrize`` is expanded from the syntax tree, giving the run count pytest
        itself produces, and every document quoting a three-digit case count is required to
        quote exactly that.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        expanded = 0
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                owner = globals()[node.name]
                class_multiplier = self._case_multiplier(node.decorator_list, owner)
                for member in node.body:
                    if isinstance(
                        member, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and member.name.startswith("test_"):
                        expanded += class_multiplier * self._case_multiplier(
                            member.decorator_list, owner
                        )
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                expanded += self._case_multiplier(node.decorator_list, None)

        # Every phrasing the five documents use to quote the figure.
        pattern = r"\*\*(\d{3})(?:\*\*)?\s+(?:of them|cases|passed|tests)"
        cited = {}
        for key, path in documents.items():
            for value in re.findall(pattern, path.read_text(encoding="utf-8")):
                cited.setdefault(int(value), []).append(key)
        assert cited, "no document quotes a test-case count"
        assert set(cited) == {expanded}, (
            "documents quote %s but this module expands to %d cases"
            % (sorted(cited), expanded)
        )

    def test_every_documented_test_reference_resolves(self, documents):
        """A document naming a test that does not exist tells a reader a verification is in
        place when none is.

        The Decision Log cited `TestKnownResiduals::test_the_user_mapper_cannot_be_configured`
        as the proof of a residual - a test that never existed, for a residual that had been
        closed. Nothing failed, because no check tied a documented node ID to the suite. Every
        `Class::test_name` reference in the five documents is resolved here against this
        module's own syntax tree, so a citation to a test that is absent, renamed or moved to
        another class fails.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        defined = {
            node.name: {
                member.name
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        references = {}
        for key, path in documents.items():
            body = path.read_text(encoding="utf-8")
            for class_name, test_name in re.findall(
                r"(Test[A-Z]\w*)::(test_\w+)", body
            ):
                references.setdefault((class_name, test_name), set()).add(key)
        assert references, "no document cites a test node id"
        unresolved = sorted(
            "%s::%s (cited in %s)" % (class_name, test_name, ", ".join(sorted(cited)))
            for (class_name, test_name), cited in references.items()
            if test_name not in defined.get(class_name, set())
        )
        assert unresolved == [], unresolved

    def test_no_document_claims_a_workflow_value_the_workflow_does_not_declare(
        self, documents
    ):
        """A document describing a CI mechanism that is not in the workflow sends a reviewer
        looking for something that was never there.

        The Decision Log recorded, inside a clause labelled as a correction, that the audit
        step declared an `env` value holding the advisory baseline. The step declares no `env`
        at all: the identifiers are inline `--ignore-vuln` flags. Every `env` entry the
        documents attribute to the workflow is resolved against it here.
        """
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        claimed = {}
        for key, path in documents.items():
            for name in re.findall(
                r"declares? `env: (\w+)`", path.read_text(encoding="utf-8")
            ):
                claimed.setdefault(name, set()).add(key)
        missing = sorted(
            "%s (claimed in %s)" % (name, ", ".join(sorted(cited)))
            for name, cited in claimed.items()
            if name not in workflow
        )
        assert missing == [], missing

    def test_the_documented_recovery_install_matches_the_workflow(self, documents):
        """A documented install command that installs a version the source cannot compile
        against sends a reader further from a working tree, not nearer one.

        The matrix published `react-router-dom@6.30.1` while the workflow pinned `5.3.4`, and
        omitted `@types/react-router-dom` entirely. `frontend/src/app.tsx` imports `Switch` and
        passes `component=` to `Route`, both removed in v6, so following the document produced
        four type errors that following the workflow does not (R40). The workflow is the
        authority, so every pin the workflow installs is required to appear in every document
        that publishes the command, at the same version.
        """
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        pinned = set(re.findall(r"((?:@[\w./-]+/)?[\w.-]+@\d[\w.-]*)", workflow))
        pinned = {
            specifier
            for specifier in pinned
            if specifier.split("@")[0] or specifier.startswith("@")
        }
        assert "react-router-dom@5.3.4" in pinned, sorted(pinned)
        for key, path in documents.items():
            body = path.read_text(encoding="utf-8")
            if "--testPathPattern api.test" not in body:
                continue
            for specifier in sorted(pinned):
                package = specifier.rsplit("@", 1)[0]
                if package + "@" not in body:
                    continue
                assert specifier in body, (
                    "%s publishes a different version of %s than the workflow installs"
                    % (key, package)
                )

    def test_no_document_claims_the_infrastructure_cannot_be_initialised(self, documents):
        """`terraform init` succeeds here, and a document saying otherwise stops a reader
        running the one command that proves the configuration loads.

        What does fail is `validate` over the whole directory, on the pre-existing `outputs.tf`,
        and that distinction is the whole point: a text assertion cannot tell a configuration
        that loads from one that does not, so the init is the verification that matters and the
        `outputs.tf` failure is a separate, tracked defect.
        """
        for key, path in documents.items():
            body = path.read_text(encoding="utf-8")
            for claim in (
                "`terraform init` cannot run",
                "terraform init cannot run",
                "cannot run `terraform init`",
            ):
                assert claim not in body, "%s claims %r" % (key, claim)

    def test_no_document_claims_the_deployment_script_revokes_the_invoker_binding(
        self, documents
    ):
        """D56 replaced the revoke-then-re-read pair with an assertion in the read-only
        preflight, and three documents went on describing the removed behaviour.

        This matters beyond tidiness: an operator who believes the script removes a public
        `allUsers` binding will not go and remove one, and the script will not either — it
        aborts and tells them to. The claim is checked against the script rather than against
        prose, so the two cannot drift apart again.
        """
        deploy = (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        # The behaviour the documents must describe, asserted against the script first - so if
        # the script ever goes back to mutating the policy, this test fails here and is retired
        # rather than silently forcing the documents to keep denying something that is true.
        assert "gcloud functions get-iam-policy" in deploy
        assert "A public principal holds roles/cloudfunctions.invoker" in deploy
        assert "The invoker policy of '${FUNCTION_NAME}' could not be read." in deploy
        for key, path in documents.items():
            body = path.read_text(encoding="utf-8")
            for claim in (
                "revokes any `allUsers` invoker binding",
                "revokes any `allUsers` binding",
                "confirms the revocation",
                "revokes `allUsers` *and* `allAuthenticatedUsers`",
            ):
                assert claim not in body, "%s claims %r" % (key, claim)

    def test_the_reverse_matrix_matches_the_working_tree(self, documents):
        """Rule 1 requires 100% bidirectional coverage. A row naming a path that no longer
        exists is the failure mode this catches — `api.test.ts` was such a row.

        This is one direction only, which is why the test below exists.
        """
        matrix = documents["matrix"].read_text(encoding="utf-8")
        for path in re.findall(r"^\| \d+ \| `([^`]+)` \|", matrix, re.M):
            if path.endswith("/"):
                continue
            assert (REPOSITORY_ROOT / path).exists(), path

    def test_every_changed_path_is_represented_in_the_reverse_matrix(self, documents):
        """The other direction, which nothing checked — and a real omission survived it.

        The table claimed 47 of 47 while the diff carried 51 paths: `favicon.ico`,
        `logo192.png`, `manifest.json` and `og-image.jpg` were changed and unrepresented. Both
        existing guards passed, because one only checks the table's internal arithmetic and the
        other only checks that each listed path exists — neither ever reads the diff. Rule 1's
        coverage claim is a statement about two set differences, so both are asserted here.

        Untracked files are included as well as changed ones: `git diff` does not report a new
        file until it is staged, so a newly added and unmentioned artifact would otherwise be
        invisible to this check right up to the commit.

        Skipped rather than failed when the baseline commit is unreachable, which is what a
        shallow clone gives. That is a real gap, so it is closed where it matters instead of
        being tolerated: the `security-checks` job in `.github/workflows/ci.yml` checks out with
        `fetch-depth: 0` so this runs there as a gate.
        """
        import subprocess

        def _git(*arguments):
            return subprocess.run(
                ("git",) + arguments,
                cwd=str(REPOSITORY_ROOT),
                capture_output=True,
                text=True,
            )

        baseline = _TRACEABILITY_BASELINE_COMMIT
        if _git("rev-parse", "--verify", "--quiet", baseline + "^{commit}").returncode != 0:
            pytest.skip(
                "the traceability baseline commit %s is unreachable, which is what a shallow "
                "clone gives; the security-checks job fetches full history so this runs there"
                % baseline
            )

        changed = _git("diff", baseline, "--name-only")
        assert changed.returncode == 0, changed.stderr
        untracked = _git("ls-files", "--others", "--exclude-standard")
        assert untracked.returncode == 0, untracked.stderr

        matrix = documents["matrix"].read_text(encoding="utf-8")
        represented = set(re.findall(r"^\| \d+ \| `([^`]+)` \|", matrix, re.M))
        observed = {
            line.strip()
            for line in changed.stdout.splitlines() + untracked.stdout.splitlines()
            if line.strip()
        }
        # Temporary validation artifacts are never committed and are named so they can be
        # recognised; they are not part of the change set the matrix accounts for.
        observed = {
            path
            for path in observed
            if not path.rsplit("/", 1)[-1].startswith("blitzy_adhoc_test_")
        }

        unrepresented = sorted(observed - represented)
        assert unrepresented == [], (
            "changed or untracked paths absent from the reverse matrix: %s" % unrepresented
        )
        # And the count the document publishes is the count of paths it actually accounts for.
        assert "Reverse coverage: {0} of {0}.".format(len(represented)) in matrix

    def test_the_client_test_module_is_advertised_because_it_ships(self, documents):
        """It is the committed verification for the identity bridge's client half and CI
        runs it as a blocking gate, so Rule 1's reverse coverage has to account for it."""
        assert (
            REPOSITORY_ROOT / "frontend" / "src" / "services" / "api.test.ts"
        ).is_file()
        matrix = documents["matrix"].read_text(encoding="utf-8")
        rows = re.findall(r"^\| \d+ \| `([^`]+)` \|", matrix, re.M)
        assert "frontend/src/services/api.test.ts" in rows

    def test_the_emulator_check_documents_reference_still_resolves(self, documents):
        """`scripts/deploy.sh` sends an operator to SECURITY.md by name for this procedure,
        so the procedure has to be there and has to name the script that performs it."""
        deploy = (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        assert "emulator procedure in SECURITY.md" in deploy
        security = documents["security"].read_text(encoding="utf-8")
        assert "firebase emulators:exec" in security
        assert "firestore_rules_emulator_check.py" in security
        script = REPOSITORY_ROOT / "backend" / "tests" / "firestore_rules_emulator_check.py"
        assert script.is_file()

    def test_the_documented_advisory_count_matches_the_ci_gate(self, documents):
        """Two documents quote this number and the workflow is the thing that enforces it."""
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        suppressed = len(re.findall(r"--ignore-vuln\s+\S+", workflow))
        for key in ("security", "onboarding"):
            body = documents[key].read_text(encoding="utf-8")
            assert "%d advisories" % suppressed in body, key

    def test_the_declined_findings_cite_a_plan_clause(self, documents):
        """A declined finding without a citation is indistinguishable from one that was
        skipped, which is the difference Rule 1 exists to make visible."""
        matrix = documents["matrix"].read_text(encoding="utf-8")
        section = matrix[matrix.index("The seven declined findings"):]
        section = section[: section.index("Declined is not the same")]
        rows = [
            line
            for line in section.splitlines()
            if line.startswith("| ")
            and not line.startswith("| Finding |")
            and not set(line) <= set("|- ")
        ]
        assert len(rows) == 7, [row[:60] for row in rows]
        for row in rows:
            assert re.search(r"§0\.\d|\bD\d+\b", row), row

    def test_the_runtime_findings_table_cites_a_clause_decision_or_residual(self, documents):
        """The later verification order drove the controls rather than reading them, and raised
        its own set of observations. They are tracked in their own table because they were found
        a different way, and that table needs the same property as the one above: a row without a
        citation is indistinguishable from a finding nobody answered.

        Two of its rows are deliberately *not* declines - they record framework behaviour that was
        examined and found harmless - so the citation may be a decision rather than a clause.
        """
        matrix = documents["matrix"].read_text(encoding="utf-8")
        heading = "### Runtime verification findings, final order"
        assert heading in matrix, "the runtime findings table is not in the matrix"
        section = matrix[matrix.index(heading) + len(heading):]
        for boundary in ("\n## ", "\n### "):
            if boundary in section:
                section = section[: section.index(boundary)]
        rows = [
            line
            for line in section.splitlines()
            if line.startswith("| ")
            and not line.startswith("| Finding |")
            and not set(line) <= set("|- ")
        ]
        assert rows, "the runtime findings table has no rows"
        for row in rows:
            assert re.search(
                r"§0\.\d|\bD\d+\b|\bR\d+\b|residual \d+|\bF\d+\b|Test\w+::", row
            ), row[:120]
        # The coverage assertion above must agree with the table it points at, or the count is
        # a claim about nothing.
        assert "| Runtime verification findings, final order | %d / %d tracked |" % (
            len(rows),
            len(rows),
        ) in matrix, len(rows)


class TestRuntimeIdentityAndDatabaseContract:
    """C2, C3, M7 and N2: what the runtime needs to authenticate, and to what.

    Every assertion here is on the configuration rather than on a deployed resource, which a
    deployment alone can confirm. What they do catch is the failure mode this checkpoint actually
    had: the three planes describing *different* topologies, so each read consistently on its own
    and nothing joined them up. Terraform, ``scripts/deploy.sh`` and the settings surface are
    therefore asserted against each other.
    """

    @pytest.fixture
    def terraform(self):
        return (TERRAFORM / "main.tf").read_text(encoding="utf-8")

    @pytest.fixture
    def variables(self):
        return (TERRAFORM / "variables.tf").read_text(encoding="utf-8")

    @pytest.fixture
    def deploy(self):
        return (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    # --- C2: Workload Identity is complete, not half-configured --------------
    def test_the_cluster_declares_a_workload_pool(self, terraform):
        """Without it the node pool's GKE_METADATA mode has nothing to resolve against."""
        assert "workload_identity_config" in terraform
        assert 'workload_pool = "${var.project_id}.svc.id.goog"' in terraform

    def test_the_node_pool_serves_the_pod_identity_rather_than_the_node(self, terraform):
        assert "workload_metadata_config" in terraform
        assert 'mode = "GKE_METADATA"' in terraform

    def test_the_kubernetes_principal_may_act_as_the_runtime_account(self, terraform):
        """Enabling the pool grants nothing; this binding is what authorizes the exchange."""
        assert 'resource "google_service_account_iam_member" "api_runtime_workload_identity"' in (
            terraform
        )
        assert 'role               = "roles/iam.workloadIdentityUser"' in terraform
        assert (
            'member             = "serviceAccount:${var.project_id}.svc.id.goog'
            "[${var.kubernetes_namespace}/${var.kubernetes_service_account}]\"" in terraform
        )

    def test_exactly_one_runtime_identity_is_declared(self, terraform, variables):
        """C2: the planes described two API accounts while the configuration created one.

        Every runtime grant lands on ``url_signer``. The only other account declared is the
        Cloud Function's execution identity, which carries no grant at all.
        """
        accounts = re.findall(r'resource "google_service_account" "(\w+)"', terraform)
        assert accounts == ["url_signer", "function_runtime"], accounts
        assert "google_service_account.api_runtime" not in terraform
        assert "runtime_service_account" not in terraform
        assert 'variable "runtime_service_account"' not in variables
        assert (
            "service_account_email = google_service_account.function_runtime.email"
            in terraform
        )

    def test_the_signing_grant_is_the_form_a_self_signing_runtime_needs(self, terraform):
        """The runtime signs as itself, so it holds the token-creator role on its own account."""
        block = re.search(
            r'resource "google_service_account_iam_member" "url_signer_token_creator" \{(.*?)\n\}',
            terraform,
            re.S,
        )
        assert block, "url_signer_token_creator is not declared"
        body = block.group(1)
        assert 'role               = "roles/iam.serviceAccountTokenCreator"' in body
        assert "service_account_id = google_service_account.url_signer.name" in body
        assert "member             = google_service_account.url_signer.member" in body

    # --- C3: the revocation check has the permission it needs ---------------
    def test_the_runtime_may_read_identity_platform_user_records(self, terraform):
        """C3: ``check_revoked=True`` reads the user record, so the role is not optional."""
        assert 'resource "google_project_iam_member" "api_runtime_firebaseauth_viewer"' in (
            terraform
        )
        assert 'role    = "roles/firebaseauth.viewer"' in terraform

        security = (BACKEND_APP / "core" / "security.py").read_text(encoding="utf-8")
        assert "check_revoked=True" in security, (
            "the binding above exists for this call; if the call is gone the binding "
            "should be reconsidered rather than left unexplained"
        )

    def test_every_runtime_grant_names_the_one_account(self, terraform):
        for resource in (
            "api_runtime_cloudsql_client",
            "api_runtime_secret_accessor",
            "api_runtime_datastore_user",
            "api_runtime_firebaseauth_viewer",
            "user_uploads_signer_object_admin",
        ):
            block = re.search(
                r'resource "\w+" "%s" \{(.*?)\n\}' % resource, terraform, re.S
            )
            assert block, resource
            assert "google_service_account.url_signer.member" in block.group(1), resource

    # --- M7: one canonical database name across every plane -----------------
    def test_the_database_name_has_the_canonical_default(self, variables):
        block = re.search(r'variable "db_name" \{(.*?)\n\}', variables, re.S)
        assert block, "db_name is not declared"
        assert 'default     = "main-database"' in block.group(1)

    def test_the_environment_template_names_the_same_database(self):
        template = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
        url = re.search(r"^DATABASE_URL=(.+)$", template, re.M)
        assert url, "the template declares no DATABASE_URL"
        assert url.group(1).rstrip().endswith("/main-database"), url.group(1)

    def test_the_template_url_satisfies_the_settings_validator(self):
        """The template must be a value the application would actually accept."""
        from backend.app.core.config import validate_database_url

        template = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
        url = re.search(r"^DATABASE_URL=(.+)$", template, re.M).group(1).strip()
        assert validate_database_url(url) == url

    # --- N2: no input exists that nothing consumes --------------------------
    def test_no_dead_database_credential_inputs_are_declared(self, variables):
        """N2: they were required inputs that no resource read."""
        assert 'variable "db_user"' not in variables
        assert 'variable "db_password"' not in variables

    def test_every_declared_variable_is_read_or_documented_as_unread(
        self, terraform, variables
    ):
        """An input that is declared, described as configuration and never read misleads.

        An operator who sets one sees no effect and cannot tell that apart from a setting that
        failed to apply. So each variable must either be referenced or say plainly that it is not.
        """
        declared = set(re.findall(r'variable "(\w+)"', variables))
        used = set(re.findall(r"var\.(\w+)", terraform))
        assert declared, "no variables were parsed"

        undeclared = used - declared
        assert not undeclared, undeclared

        for name in sorted(declared - used):
            block = re.search(r'variable "%s" \{(.*?)\n\}' % name, variables, re.S)
            assert block, name
            assert "NOT REFERENCED" in block.group(1), (
                "%s is declared but never read, and does not say so" % name
            )

    # --- C2: the deployment script checks the topology that exists -----------
    def test_the_script_asks_for_the_one_account_terraform_declares(self, deploy):
        """C2: it required a runtime_service_account variable that Terraform does not declare."""
        assert "runtime_service_account" not in deploy
        assert "signer_service_account Terraform variable" in deploy

    def test_the_script_verifies_the_cluster_workload_pool(self, deploy):
        assert "workloadIdentityConfig.workloadPool" in deploy
        assert 'EXPECTED_WORKLOAD_POOL="${PROJECT_ID}.svc.id.goog"' in deploy

    def test_the_script_verifies_the_live_workload_identity_binding(self, deploy):
        """An annotation in the pod spec is a pointer; this binding is the grant."""
        assert "roles/iam.workloadIdentityUser" in deploy
        assert (
            'EXPECTED_WORKLOAD_IDENTITY_MEMBER="serviceAccount:${PROJECT_ID}.svc.id.goog'
            '[${KUBERNETES_NAMESPACE}/${KUBERNETES_SERVICE_ACCOUNT}]"' in deploy
        )

    def test_every_terraform_resource_the_script_names_exists(self, deploy, terraform):
        """C2: the script cited a resource no configuration declared, so its advice was
        unusable."""
        cited = set(
            re.findall(
                r"google_(?:service_account|project_iam_member|service_account_iam_member|"
                r"sql_database|sql_database_instance|storage_bucket|storage_bucket_iam_member|"
                r"compute_[a-z_]+|cloudfunctions_function|cloudfunctions_function_iam_member|"
                r"container_cluster|container_node_pool|secret_manager_secret_iam_member|"
                r"firestore_database|identity_platform_config)\.(\w+)",
                deploy,
            )
        )
        declared = set(re.findall(r'resource "\w+" "(\w+)"', terraform))
        assert cited, "the script cites no Terraform resources at all"
        assert cited <= declared, cited - declared

    # --- M5: the proxy preflight requires both conditions --------------------
    def test_the_proxy_preflight_requires_both_conditions(self, deploy):
        """M5: ``! A && ! B`` passed whenever either string alone was present."""
        assert (
            '! printf \'%s\\n\' "$MANIFESTS" | grep -q "cloud-sql-proxy" &&' not in deploy
        ), "the two proxy conditions are still combined with &&"
        assert 'grep -q "cloud-sql-proxy"' in deploy
        assert "PROXY_ARGUMENT_LINES" in deploy

    def test_the_connection_name_must_appear_in_a_container_argument(self, deploy):
        """A connection name in a comment, label or annotation connects nothing."""
        assert 'grep -v \'^[[:space:]]*#\'' in deploy
        assert 'grep -qF "$SQL_CONNECTION_NAME"' in deploy

    # --- M7: the secret is checked against the provisioned database ----------
    def test_the_script_validates_the_database_url_secret(self, deploy):
        assert 'DB_NAME="${DB_NAME:-main-database}"' in deploy
        assert "gcloud secrets versions access latest --secret=DATABASE_URL" in deploy
        for check in ("URL_SCHEME", "URL_HOST", "URL_DATABASE", "URL_LOGIN"):
            assert check in deploy, check
        assert "postgresql | postgresql+psycopg2" in deploy
        assert "gcloud sql databases list" in deploy

    def test_the_secret_value_is_never_echoed_or_written(self, deploy):
        """It carries the database password, so it may not reach stdout, a file or a message."""
        assert "$DATABASE_URL_SECRET" not in deploy.replace(
            'DATABASE_URL_SECRET="$READ_GCLOUD_VALUE"', ""
        ).replace("unset DATABASE_URL_SECRET", "")
        assert "unset DATABASE_URL_SECRET" in deploy
        for line in deploy.splitlines():
            if "DATABASE_URL_SECRET" in line:
                assert not line.strip().startswith("echo "), line

    def test_the_script_names_no_terraform_variable_that_was_removed(self, deploy):
        """N2: it told the operator to copy DB_USER from a variable that no longer exists."""
        assert "the db_user Terraform variable" not in deploy
        assert "db_password" not in deploy
        assert "there is no db_user variable to copy it from" in deploy

    def test_the_script_no_longer_directs_a_two_stage_edge_cutover(self, deploy):
        """The port-80 redirect is declared unconditionally, so that instruction cannot be
        followed."""
        assert "https_cutover_enabled" not in deploy


# ===========================================================================
# C1 / M2 - the ORM seam the identity lookup and the worksheets route depend on
# ===========================================================================
class TestOrmSeam:
    """C1 and M2: mapper configuration and row serialization both used to raise.

    ``Workbook.owner`` declared ``back_populates='workbooks'`` while ``User`` carried no such
    property, so configuring the mappers raised ``InvalidRequestError`` and *no* query against
    ``User`` could run - which meant the authentication dependency refused every request it was
    given, whatever the token said. ``WorksheetSchema`` carried no ``orm_mode``, so the
    ``from_orm`` call in the worksheets route raised ``ConfigError`` before it could answer.

    Both are asserted here as working, so re-breaking either fails a test instead of silently
    returning the seam to a state where authentication cannot succeed.
    """

    @pytest.fixture
    def worksheets_route_client(self, in_memory_database, monkeypatch):
        """Mount the REAL ``backend/app/api/worksheets.py`` router over real created tables.

        The module cannot be imported as delivered, because it resolves ``get_db``,
        ``WorksheetSchema`` and ``WorksheetService`` from three packages that carry no
        ``__init__.py`` and, in the service's case, define no such class. Those are the absent
        product surfaces the change set is not permitted to build, and this fixture does not
        build them into the application: it binds the two names that DO exist onto the
        namespace-package objects, supplies the query the absent service would perform, and
        then imports the committed module unmodified. What is exercised is therefore the real
        handler - its decorator, its page window, its authentication dependency and its
        ``from_orm`` comprehension - rather than a re-implementation of it.

        What this does NOT claim: that ``backend.app.main`` starts. It cannot, and that is a
        recorded residual. This is the same technique ``conftest.py`` already applies to the
        Secret Manager client and the identity lookup's ``get_db`` binding.

        Yields:
            tuple: the ``TestClient``, and a callable seeding a workbook, its owner and a list
                of ``(name, [(row, column, value, formula, style), ...])`` worksheets.
        """
        from datetime import datetime as _datetime

        from firebase_admin import auth as firebase_auth

        from backend.app.core import security
        from backend.app.db.models import Cell, User, Workbook, Worksheet

        session_factory = in_memory_database

        def _get_test_db():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        # The identity lookup calls the ``get_db`` name the security module imported, so that
        # binding is what has to be pointed at the test database.
        monkeypatch.setattr(security, "get_db", _get_test_db)

        # Verification is stubbed at ``_firebase_app`` as well as at ``verify_id_token``,
        # because reaching the Admin SDK would initialise a real application.
        monkeypatch.setattr(security, "_firebase_app", lambda project_id: "verifying-app")

        def _verify(token, app=None, check_revoked=False):
            assert check_revoked is True, "revocation must be checked"
            return {"email": "owner@example.com", "email_verified": True}

        monkeypatch.setattr(firebase_auth, "verify_id_token", _verify)

        class _WorksheetService:
            """The query the absent ``WorksheetService`` would perform, and nothing else."""

            def __init__(self, db):
                self._db = db

            def get_worksheets(self, workbook_id, skip=0, limit=100):
                return (
                    self._db.query(Worksheet)
                    .filter(Worksheet.workbook_id == int(workbook_id))
                    .order_by(Worksheet.order)
                    .offset(skip)
                    .limit(limit)
                    .all()
                )

        db_package = importlib.import_module("backend.app.db")
        schema_package = importlib.import_module("backend.app.schema")
        services_package = importlib.import_module("backend.app.services")
        monkeypatch.setattr(db_package, "get_db", _get_test_db, raising=False)
        monkeypatch.setattr(
            schema_package,
            "WorksheetSchema",
            importlib.import_module(
                "backend.app.schema.workbook_schema"
            ).WorksheetSchema,
            raising=False,
        )
        monkeypatch.setattr(
            services_package, "WorksheetService", _WorksheetService, raising=False
        )
        monkeypatch.delitem(sys.modules, "backend.app.api.worksheets", raising=False)
        route_module = importlib.import_module("backend.app.api.worksheets")

        app = FastAPI()
        app.include_router(route_module.router)
        client = TestClient(app, raise_server_exceptions=False)

        def _seed(worksheets):
            session = session_factory()
            try:
                session.add(
                    User(
                        id=1,
                        email="owner@example.com",
                        name="Owner",
                        created_at=_datetime(2024, 1, 1),
                    )
                )
                session.add(
                    Workbook(
                        id=1,
                        name="Book1",
                        owner_id=1,
                        created_at=_datetime(2024, 1, 1),
                        modified_at=_datetime(2024, 1, 1),
                        settings=None,
                    )
                )
                for order, (name, cells) in enumerate(worksheets):
                    worksheet = Worksheet(
                        id=order + 1, workbook_id=1, name=name, order=order
                    )
                    session.add(worksheet)
                    for index, (row, column, value, formula, style) in enumerate(cells):
                        session.add(
                            Cell(
                                worksheet_id=worksheet.id,
                                row=row,
                                column=column,
                                value=value,
                                formula=formula,
                                style=style,
                            )
                        )
                session.commit()
            finally:
                session.close()

        try:
            yield client, _seed
        finally:
            sys.modules.pop("backend.app.api.worksheets", None)

    def test_the_mappers_configure(self):
        from sqlalchemy.orm import configure_mappers

        import backend.app.db.models  # noqa: F401 - importing registers every mapper

        configure_mappers()

    def test_the_owner_relationship_pair_is_complete(self):
        from backend.app.db.models import User, Workbook

        assert User.workbooks.property.mapper.class_ is Workbook
        assert Workbook.owner.property.mapper.class_ is User
        assert User.workbooks.property.back_populates == "owner"
        assert Workbook.owner.property.back_populates == "workbooks"

    def test_no_column_was_added_to_the_user_table(self):
        """The reverse relationship is a mapper-level property, so it needs no migration."""
        from backend.app.db.models import User

        assert [column.name for column in User.__table__.columns] == [
            "id",
            "email",
            "name",
            "created_at",
        ]

    def test_the_identity_query_runs_against_real_tables(self, in_memory_database):
        """The exact query ``_resolve_current_user`` issues, against created tables."""
        from backend.app.db.models import User

        session = in_memory_database()
        try:
            session.add(
                User(
                    id=1,
                    email="user@example.com",
                    name="Test User",
                    created_at=datetime(2024, 1, 1),
                )
            )
            session.commit()
            found = session.query(User).filter(User.email == "user@example.com").first()
            assert found is not None
            assert found.id == 1
            assert session.query(User).filter(User.email == "absent@example.com").first() is None
        finally:
            session.close()

    def test_the_worksheet_schema_is_built_from_a_row_object(self):
        from backend.app.schema.workbook_schema import WorksheetSchema

        class _Row:
            id = "1"
            name = "Sheet1"
            cells = {"A1": {"value": "7", "formula": None, "style": {}}}
            named_ranges = None

        model = WorksheetSchema.from_orm(_Row())
        assert model.name == "Sheet1"
        assert model.cells["A1"].value == "7"

    def test_the_worksheet_schema_field_contract_is_unchanged(self):
        """``orm_mode`` and the ``cells`` projection are a ``Config`` flag and a ``pre``
        validator: neither adds, renames nor retypes a field."""
        from typing import Dict

        from backend.app.schema.workbook_schema import CellSchema, WorksheetSchema

        assert list(WorksheetSchema.__fields__) == ["name", "cells", "named_ranges"]
        assert WorksheetSchema.__fields__["name"].outer_type_ is str
        assert WorksheetSchema.Config.orm_mode is True
        cells = WorksheetSchema.__fields__["cells"]
        assert cells.outer_type_ == Dict[str, CellSchema]
        assert cells.required is True

    # -- The cells projection ------------------------------------------------------------
    #
    # ``Worksheet.cells`` is a one-to-many relationship, so a row object presents a LIST of
    # ``Cell`` rows, while ``WorksheetSchema.cells`` declares a map keyed by cell reference.
    # ``GET /workbooks/{id}/worksheets`` answered ``500`` for every worksheet holding at least
    # one cell because of that. An empty relationship coerced - Pydantic reads ``[]`` as ``{}``
    # - so the failure surfaced on exactly the case every real workbook is.

    def test_an_empty_worksheet_row_serializes_to_an_empty_map(self):
        from backend.app.db.models import Worksheet
        from backend.app.schema.workbook_schema import WorksheetSchema

        row = Worksheet(id=1, workbook_id=1, name="Sheet1", order=0)
        assert WorksheetSchema.from_orm(row).cells == {}

    def test_a_populated_worksheet_row_projects_onto_cell_references(self):
        """The case that used to raise. The key is the only place a response carries a
        position, because ``CellSchema`` declares no coordinate field."""
        from backend.app.db.models import Cell, Worksheet
        from backend.app.schema.workbook_schema import WorksheetSchema

        row = Worksheet(id=1, workbook_id=1, name="Sheet1", order=0)
        row.cells.append(
            Cell(id=1, worksheet_id=1, row=1, column=1, value="7", formula=None, style={})
        )
        row.cells.append(
            Cell(
                id=2,
                worksheet_id=1,
                row=2,
                column=3,
                value="14",
                formula="=A1*2",
                style={"fontWeight": "bold"},
            )
        )

        model = WorksheetSchema.from_orm(row)
        assert set(model.cells) == {"A1", "C2"}
        assert model.cells["A1"].value == "7"
        assert model.cells["A1"].formula is None
        assert model.cells["C2"].value == "14"
        assert model.cells["C2"].formula == "=A1*2"
        assert model.cells["C2"].style == {"fontWeight": "bold"}
        # And the serialized shape is still a JSON object of objects.
        assert json.loads(model.json())["cells"]["A1"] == {
            "value": "7",
            "formula": None,
            "style": {},
        }

    @pytest.mark.parametrize(
        "column, letters",
        [(1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (28, "AB"), (702, "ZZ"), (703, "AAA")],
    )
    def test_the_projection_renders_the_spreadsheet_column_alphabet(self, column, letters):
        """A1 notation is bijective base-26, so column 27 is AA rather than A0 or BA. This is
        the notation the product already speaks - ``ChartDialog.tsx`` prompts for ``A1:B10``."""
        from backend.app.schema.workbook_schema import cell_reference

        assert cell_reference(4, column) == "%s4" % letters

    def test_a_null_cell_value_or_style_does_not_break_the_projection(self):
        """``Cell.value`` and ``Cell.style`` are nullable columns while ``value`` and ``style``
        are required here, so a partial projection would have moved the ``500`` rather than
        closed it. A stored NULL renders as what an empty cell holds."""
        from backend.app.db.models import Cell, Worksheet
        from backend.app.schema.workbook_schema import WorksheetSchema

        row = Worksheet(id=1, workbook_id=1, name="Sheet1", order=0)
        row.cells.append(
            Cell(id=1, worksheet_id=1, row=1, column=1, value=None, formula=None, style=None)
        )

        cell = WorksheetSchema.from_orm(row).cells["A1"]
        assert cell.value == ""
        assert cell.style == {}
        assert cell.formula is None

    @pytest.mark.parametrize(
        "row_number, column_number, key",
        [(0, 0, "R0C0"), (-1, 3, "R-1C3"), (1, 0, "R1C0"), (None, None, "RNoneCNone")],
    )
    def test_a_coordinate_outside_the_one_based_domain_still_yields_a_key(
        self, row_number, column_number, key
    ):
        """No stored row may make the route unanswerable again. The fallback cannot collide
        with an A1 key, which always begins with a letter."""
        from backend.app.schema.workbook_schema import cell_reference

        assert cell_reference(row_number, column_number) == key

    def test_a_request_body_map_reaches_the_field_validators_untouched(self):
        """The projection is on the input side of ``from_orm`` only. ``POST /workbooks`` sends
        the declared map inside its ``worksheets`` list, and that request contract is frozen."""
        from backend.app.schema.workbook_schema import WorksheetSchema

        model = WorksheetSchema(
            name="Sheet1",
            cells={"B7": {"value": "9", "formula": None, "style": {"color": "red"}}},
            named_ranges={"total": "B7"},
        )
        assert model.cells["B7"].value == "9"
        assert model.cells["B7"].style == {"color": "red"}
        assert model.named_ranges == {"total": "B7"}

    @pytest.mark.parametrize("value", [5, "A1", ["not-a-cell-row"], [{"value": "1"}]])
    def test_a_value_that_is_neither_a_map_nor_cell_rows_is_still_refused(self, value):
        """The projection widens what ``cells`` accepts by exactly one shape. Anything else is
        reported against what the caller sent rather than against a half-built map."""
        from backend.app.schema.workbook_schema import WorksheetSchema

        with pytest.raises(pydantic.ValidationError, match="not a valid dict"):
            WorksheetSchema(name="Sheet1", cells=value, named_ranges=None)

    def test_the_worksheets_route_serializes_a_populated_worksheet(
        self, worksheets_route_client
    ):
        """The finding as reported, driven through the real handler in
        ``backend/app/api/worksheets.py`` against real created tables.

        Before the projection this answered ``500`` with the error boundary's fixed body for
        any workbook holding a cell.
        """
        client, seed = worksheets_route_client
        seed(
            worksheets=[
                ("Sheet1", [(1, 1, "7", None, {}), (2, 3, "14", "=A1*2", {"bold": "true"})]),
                ("Sheet2", []),
            ]
        )

        response = client.get(
            "/workbooks/1/worksheets", headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert [sheet["name"] for sheet in body] == ["Sheet1", "Sheet2"]
        assert body[0]["cells"] == {
            "A1": {"value": "7", "formula": None, "style": {}},
            "C2": {"value": "14", "formula": "=A1*2", "style": {"bold": "true"}},
        }
        assert body[1]["cells"] == {}

    def test_the_worksheets_route_still_refuses_an_unauthenticated_caller(
        self, worksheets_route_client
    ):
        """The projection must not have widened the route's reachability."""
        client, seed = worksheets_route_client
        seed(worksheets=[("Sheet1", [(1, 1, "7", None, {})])])

        response = client.get("/workbooks/1/worksheets")

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_the_worksheets_route_still_answers_404_for_a_workbook_with_no_worksheets(
        self, worksheets_route_client
    ):
        client, seed = worksheets_route_client
        seed(worksheets=[])

        response = client.get(
            "/workbooks/1/worksheets", headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 404

    def test_the_route_body_the_probe_drives_is_the_committed_one(self):
        """The probe above imports the real module rather than re-implementing it, and this
        pins the one expression that makes it the verification of the projection: a change
        from ``from_orm`` to anything else would leave the probe passing against a handler
        that no longer exercises what is under test."""
        source = code_only((BACKEND_APP / "api" / "worksheets.py").read_text("utf-8"))
        assert "[WorksheetSchema.from_orm(worksheet) for worksheet in worksheets]" in source


# ===========================================================================
# Residuals this change set is not permitted to close
# ===========================================================================
class TestKnownResiduals:
    """Characterises defects in modules the change set may not edit, so closing one fails here.

    These are not assertions that the behaviour is desirable. Each one is recorded as a residual
    and a follow-up in ``documentation/Security Decision Log.md``, and is pinned here so that the
    follow-up landing is visible rather than silent.
    """

    def test_a_worksheet_row_still_carries_no_named_ranges(self):
        """The half of the worksheet seam that is still open, kept separate from the half that
        was closed.

        The ``cells`` list-versus-map mismatch is fixed - ``TestOrmSeam`` drives the real route
        over a populated worksheet and gets ``200`` - but ``Worksheet`` declares no
        ``named_ranges`` attribute at all, so the field a response advertises is always ``null``
        rather than the worksheet's named ranges. ``from_orm`` tolerates the absence because the
        field is optional, which is why it is silent rather than reported: adding the attribute
        means a column and a migration, and no migration tooling exists.
        """
        from backend.app.db.models import Worksheet
        from backend.app.schema.workbook_schema import WorksheetSchema

        assert Worksheet.cells.property.uselist is True
        assert not hasattr(Worksheet, "named_ranges")
        assert WorksheetSchema.__fields__["named_ranges"].required is False

        row = Worksheet(id=1, workbook_id=1, name="Sheet1", order=0)
        assert WorksheetSchema.from_orm(row).named_ranges is None

    def test_the_cells_route_states_how_its_worksheet_id_resolves(self):
        """``worksheet_id`` means different things either side of the route, and nothing at
        the transport layer will say so.

        ``WorksheetSchema`` declares no identifier field, so the worksheet's NAME is the only
        identifier a response ever hands a client - which is why the client sends a name. The
        mapped ``Worksheet.id`` is an Integer primary key. The route annotates the parameter
        ``str``, so a name and a primary key are both accepted with ``200`` and the mismatch
        cannot surface until an implementation resolves it: reading it as a key would write
        every cell to the wrong worksheet, or to none.

        The route body belongs to ``CellService``, which does not exist and which this change
        set may not build, so the resolution rule is stated at the site and tracked instead.
        """
        import sqlalchemy

        from backend.app.db.models import Worksheet
        from backend.app.schema.workbook_schema import WorksheetSchema

        # The two sides of the ambiguity, asserted rather than described.
        assert "id" not in WorksheetSchema.__fields__
        assert isinstance(Worksheet.id.type, sqlalchemy.Integer)

        source = (BACKEND_APP / "api" / "cells.py").read_text(encoding="utf-8")
        assert "worksheet_id: str" in source
        marker = source.split("@router.put")[0]
        assert "CONTRACT" in marker
        assert "(workbook_id, name)" in marker
        assert "F26" in marker, "the resolution rule is stated but not tracked"

    def test_the_cross_tenant_residual_is_published_at_its_measured_scope(self):
        """The authorization residual is wider than a read, and it was published as a read.

        ``current_user`` resolves on all five routes and is referenced ZERO times in every one
        of the five handler bodies, so the credential decides whether a request is served and
        never which rows it may reach. Runtime measurement against the real route modules, with
        a credential naming a user who owned nothing, produced four facets rather than one: the
        list and worksheet reads, a cell write persisted into another user's worksheet, a share
        granting a third party access to another user's workbook, and a create whose
        ``owner_id`` came from the request body.

        The share facet is the one the old "cross-tenant read" wording hid, and it is the facet
        with the widest consequence because a grant outlives the request that made it. So the
        published residual has to name all four, and must not describe the scope as a read
        alone. Asserted here rather than in a document-only check because the thing that makes
        the scope what it is - an unconsulted dependency and a body-supplied owner - is code,
        and the day either changes this test is what says the residual has narrowed.
        """
        # The structural cause: declared on every handler, consulted by none of them.
        for module_file, handler, _method, _path in ROUTE_CONTRACTS:
            function = _function(_parse(BACKEND_APP / "api" / module_file), handler)
            assert "current_user" in [
                argument.arg for argument in function.args.args
            ], handler
            body = ast.dump(ast.Module(body=function.body, type_ignores=[]))
            assert "id='current_user'" not in body, (
                "%s now consults current_user, so residual 1 has narrowed - restate its "
                "measured scope instead of keeping a test that describes the old one"
                % handler
            )

        # The attribution facet: owner_id is a required REQUEST field, not a server value.
        from backend.app.schema.workbook_schema import WorkbookSchema

        assert WorkbookSchema.__fields__["owner_id"].required

        policy = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        residual = policy.split("## Residual risks", 1)[1].split("\n2. ", 1)[0]
        for facet in ("**Read.**", "**Write.**", "**Share.**", "**Attribution.**"):
            assert facet in residual, facet
        assert "read, write to and share another user's workbook" in residual
        assert "referenced **zero times**" in residual
        # The retracted narrower wording, so it cannot come back.
        assert "can read another user's workbook.**" not in policy
        # And the improvement is still stated, because overstating the residual is its own
        # defect: none of this is reachable without a valid credential.
        assert "a valid credential is now required" in residual

    def test_the_application_entry_point_cannot_be_imported(self):
        """No package under ``backend/`` carries ``__init__.py`` and the domain service
        classes do not exist, so the entry point cannot be imported."""
        with pytest.raises(ImportError):
            importlib.import_module("backend.app.main")

    def test_the_specification_endpoints_are_open_and_documented_as_open(self):
        """``/openapi.json``, ``/docs`` and ``/redoc`` answer with no credential.

        That is FastAPI's default rather than anything an application router declares, and runtime
        verification confirmed all four return ``200`` unauthenticated while carrying schema shape
        only - no account address, no database password, no signing key - and still carrying all
        six response headers. Closing it means ``openapi_url=None`` and ``docs_url=None`` on the
        application construction, and this change set may touch that file for the CORS, header and
        throttling wiring only.

        The pairing is the point: the code state and the documented state are asserted together,
        so one cannot move without the other. If the endpoints are ever closed, this fails with
        instructions to retire the residual rather than silently continuing to describe an
        exposure that no longer exists.
        """
        main = (BACKEND_APP / "main.py").read_text(encoding="utf-8")
        collapsed = " ".join(main.split())
        closed = [
            argument
            for argument in ("openapi_url=None", "docs_url=None", "redoc_url=None")
            if argument in collapsed.replace(" ", "")
        ]
        if closed:
            pytest.fail(
                "main.py now closes %s, which is the outcome follow-up F48 exists to reach - "
                "remove residual 36 and its tracking entries instead of keeping a test that "
                "demands the exposure" % ", ".join(closed)
            )
        security = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for endpoint in ("/openapi.json", "/docs", "/redoc"):
            assert endpoint in security, (
                "SECURITY.md does not name %s as an unauthenticated surface" % endpoint
            )

    def test_the_request_schemas_are_permissive_and_documented_as_permissive(self):
        """No schema sets ``extra``, so an unknown field is dropped rather than refused.

        Measured on the real schemas: ``WorkbookSchema(id=12345, ...)`` yields ``id == "12345"``,
        ``CellSchema(value=42, ...)`` yields ``value == "42"``, and a field the schema never
        declares is absent from ``.dict()`` rather than raising. There is no injection consequence
        - a coerced value reaches SQL only as a bound parameter - so the cost is that a client bug
        surfaces as wrong data instead of a ``422``. Setting ``extra = "forbid"`` changes a frozen
        request contract in the refusing direction, so this pins the posture rather than changing
        it, and retires itself if the posture is ever decided the other way.
        """
        import pydantic as _pydantic  # noqa: F401  (already imported at module scope)

        from backend.app.schema.workbook_schema import CellSchema, WorkbookSchema

        source = (BACKEND_APP / "schema" / "workbook_schema.py").read_text(encoding="utf-8")
        if re.search(r"^\s*extra\s*=", source, re.M):
            pytest.fail(
                "workbook_schema.py now sets extra, which is the outcome follow-up F45 exists "
                "to reach - remove residual 33 and its tracking entries instead of keeping a "
                "test that demands the permissiveness"
            )

        coerced = WorkbookSchema(
            id=12345,
            name=99,
            owner_id=7,
            worksheets=[],
            created_at="2024-01-01T00:00:00",
            modified_at="2024-01-02T00:00:00",
            settings={},
            field_the_schema_never_declares="dropped",
        )
        assert coerced.id == "12345" and coerced.name == "99"
        assert "field_the_schema_never_declares" not in coerced.dict()

        cell = CellSchema(
            value=42, formula=None, style={}, field_the_schema_never_declares="dropped"
        )
        assert cell.value == "42"
        assert "field_the_schema_never_declares" not in cell.dict()

        # The create body demands values the server assigns, which is residual 34 - pinned here
        # because it is the same frozen contract and the same measurement.
        required = {
            name for name, field in WorkbookSchema.__fields__.items() if field.required
        }
        assert {"id", "owner_id", "worksheets", "created_at", "modified_at"} <= required, (
            sorted(required)
        )



# ===========================================================================
# Issues 3 and 4 - a list route bounds its page window
# ===========================================================================


#: Route module and handler for each route that serves a page of rows.
LIST_ROUTES = [
    ("workbooks.py", "get_workbooks"),
    ("worksheets.py", "get_worksheets"),
]


def _keyword_names(call):
    """Return the keyword argument names of an ``ast.Call``."""
    return [keyword.arg for keyword in call.keywords]


def _keyword(call, name):
    """Return the named keyword argument node of an ``ast.Call``."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError("no keyword named %r" % name)


def _parameter_defaults(module_file, handler):
    """Return each defaulted parameter of a route handler, mapped to its default node."""
    tree = _parse(BACKEND_APP / "api" / module_file)
    function = _function(tree, handler)
    names = [argument.arg for argument in function.args.args]
    offset = len(function.args.args) - len(function.args.defaults)
    return {
        name: function.args.defaults[index - offset]
        for index, name in enumerate(names)
        if index >= offset
    }


class TestListRoutesBoundTheirPageWindow:
    """Issues 3 and 4: neither list route can be made to serve an unbounded page.

    What is asserted here and what is not. The route modules cannot be imported - no package
    under ``backend/`` carries an ``__init__.py`` and the domain services do not exist - so the
    constraint on the real handler is asserted by parsing it, which pins the exact construct and
    fails if it is weakened or removed. The *semantics* of that construct, that an out-of-range
    value is answered 422 rather than reaching SQL and being answered 500, are exercised
    behaviourally against a probe application built from the same shared constants the routes
    import. Both were also measured end to end against a running server during verification.
    """

    @pytest.fixture
    def page_client(self):
        """A probe route carrying the constraint the list routes carry."""
        from fastapi import Query

        from backend.app.core.pagination import (
            DEFAULT_PAGE_OFFSET,
            DEFAULT_PAGE_SIZE,
            MAX_PAGE_OFFSET,
            MAX_PAGE_SIZE,
        )

        app = FastAPI()

        @app.get("/page")
        def page(
            skip: int = Query(DEFAULT_PAGE_OFFSET, ge=0, le=MAX_PAGE_OFFSET),
            limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        ):
            return {"skip": skip, "limit": limit}

        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize("module_file, handler", LIST_ROUTES)
    @pytest.mark.parametrize("parameter", ["skip", "limit"])
    def test_the_parameter_is_declared_with_a_bound(self, module_file, handler, parameter):
        """The default is a ``Query`` carrying both a floor and a ceiling."""
        default = _parameter_defaults(module_file, handler)[parameter]
        assert isinstance(default, ast.Call), (
            "%s.%s leaves %s as a plain literal, so it carries no bound"
            % (module_file, handler, parameter)
        )
        assert getattr(default.func, "id", None) == "Query"
        assert "ge" in _keyword_names(default), (
            "%s has no lower bound, so a negative value reaches SQL" % parameter
        )
        assert "le" in _keyword_names(default), (
            "%s has no upper bound, so an above-int64 value reaches SQL" % parameter
        )

    @pytest.mark.parametrize("module_file, handler", LIST_ROUTES)
    def test_the_bounds_name_the_shared_constants(self, module_file, handler):
        """The numbers are referenced, not written out, so the two routes cannot drift apart."""
        defaults = _parameter_defaults(module_file, handler)

        offset = defaults["skip"]
        assert getattr(offset.args[0], "id", None) == "DEFAULT_PAGE_OFFSET"
        assert _keyword(offset, "ge").value == 0
        assert getattr(_keyword(offset, "le"), "id", None) == "MAX_PAGE_OFFSET"

        size = defaults["limit"]
        assert getattr(size.args[0], "id", None) == "DEFAULT_PAGE_SIZE"
        assert _keyword(size, "ge").value == 1
        assert getattr(_keyword(size, "le"), "id", None) == "MAX_PAGE_SIZE"

        source = code_only((BACKEND_APP / "api" / module_file).read_text(encoding="utf-8"))
        assert "backend.app.core.pagination" in source

    @pytest.mark.parametrize("module_file, handler", LIST_ROUTES)
    def test_the_page_window_precedes_the_authenticated_caller(self, module_file, handler):
        """``current_user`` stays last, so the added parameters displace nothing."""
        tree = _parse(BACKEND_APP / "api" / module_file)
        names = [argument.arg for argument in _function(tree, handler).args.args]
        assert names[-1] == "current_user"
        assert names.index("skip") < names.index("limit") < names.index("current_user")

    def test_the_worksheets_route_passes_its_window_to_the_query(self):
        """A window applied only to the payload still costs the database the whole read."""
        source = code_only((BACKEND_APP / "api" / "worksheets.py").read_text(encoding="utf-8"))
        assert "get_worksheets(workbook_id, skip=skip, limit=limit)" in " ".join(source.split())

    def test_the_ceiling_is_the_default_so_no_caller_gets_a_larger_page(self):
        """A caller may not ask for more than the route serves unasked."""
        from backend.app.core import pagination

        assert pagination.MAX_PAGE_SIZE == pagination.DEFAULT_PAGE_SIZE == 100
        assert pagination.DEFAULT_PAGE_OFFSET == 0

    def test_the_offset_ceiling_stays_inside_int64(self):
        """The bound exists because a bind parameter above int64 was answered 500."""
        from backend.app.core.pagination import MAX_PAGE_OFFSET

        assert 0 < MAX_PAGE_OFFSET <= 9223372036854775807

    @pytest.mark.parametrize("query", ["skip=-1", "limit=-1", "limit=0"])
    def test_a_value_below_the_floor_is_refused(self, page_client, query):
        """The measured failure was a 500 from PostgreSQL; the answer is now a 422."""
        response = page_client.get("/page?" + query)
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "query",
        [
            "limit=101",
            "limit=1000000",
            "limit=9223372036854775807",
            "limit=9223372036854775808",
            "skip=9223372036854775808",
            "skip=99999999999999999999999",
        ],
    )
    def test_a_value_above_the_ceiling_is_refused(self, page_client, query):
        """Includes both above-int64 cases, each of which was measured as a 500."""
        response = page_client.get("/page?" + query)
        assert response.status_code == 422

    def test_the_refusal_names_the_parameter_and_not_the_database(self, page_client):
        """A validation refusal is actionable; the 500 it replaces named the driver."""
        body = page_client.get("/page?limit=-1").json()
        assert body["detail"][0]["loc"] == ["query", "limit"]
        rendered = json.dumps(body)
        for leak in ("psycopg2", "LIMIT must not be negative", "InvalidRowCount"):
            assert leak not in rendered

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("", {"skip": 0, "limit": 100}),
            ("skip=0&limit=1", {"skip": 0, "limit": 1}),
            ("limit=100", {"skip": 0, "limit": 100}),
            ("skip=1000000", {"skip": 1000000, "limit": 100}),
        ],
    )
    def test_a_value_within_the_window_is_served(self, page_client, query, expected):
        """The bound refuses only what it must; the default is unchanged."""
        response = page_client.get("/page?" + query)
        assert response.status_code == 200
        assert response.json() == expected

    def test_a_repeated_parameter_cannot_escape_the_ceiling(self, page_client):
        """The last value wins, which was how a small limit was overridden by a large one."""
        assert page_client.get("/page?limit=5&limit=999").status_code == 422
        assert page_client.get("/page?limit=999&limit=5").json()["limit"] == 5


# ===========================================================================
# Issue 12 - a handler reports a failure without disclosing its text
# ===========================================================================


#: Route module, the status it answers, and the module constant carrying its wording.
DISCLOSURE_SITES = [
    ("cells.py", 500, "UPDATE_FAILED_DETAIL"),
    ("collaboration.py", 400, "SHARE_FAILED_DETAIL"),
]


class TestHandlersDoNotDiscloseExceptionText:
    """Issue 12: neither handler returns the internal exception's text (CWE-209).

    Asserted by parsing, for the reason given on
    :class:`TestListRoutesBoundTheirPageWindow`: these modules cannot be imported. The
    behaviour - a fixed body out, the cause in the log - was measured against a running server,
    where a NUL byte in a cell value previously returned the psycopg2 message verbatim.
    """

    @pytest.mark.parametrize("module_file, status_code, constant", DISCLOSURE_SITES)
    def test_the_exception_text_is_not_returned(self, module_file, status_code, constant):
        """No form of the disclosure survives, in code or in a name bound to it."""
        source = code_only((BACKEND_APP / "api" / module_file).read_text(encoding="utf-8"))
        collapsed = " ".join(source.split())
        for disclosure in ("str(e)", "str(exc)", "str(error)", "repr(e)", "{e}", "%s' % e"):
            assert disclosure not in collapsed, (
                "%s still discloses the exception text as %s" % (module_file, disclosure)
            )
        assert "except Exception as" not in collapsed, (
            "%s still binds the exception to a name, which is how the text got out"
            % module_file
        )

    @pytest.mark.parametrize("module_file, status_code, constant", DISCLOSURE_SITES)
    def test_the_answer_is_a_fixed_message_at_the_unchanged_status(
        self, module_file, status_code, constant
    ):
        """The status the route has always answered is preserved; only the text is fixed."""
        source = code_only((BACKEND_APP / "api" / module_file).read_text(encoding="utf-8"))
        collapsed = " ".join(source.split())
        assert (
            "raise HTTPException(status_code=%d, detail=%s)" % (status_code, constant)
            in collapsed
        )
        tree = _parse(BACKEND_APP / "api" / module_file)
        assigned = [
            node.targets[0].id
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        ]
        assert constant in assigned, "%s does not define %s" % (module_file, constant)

    @pytest.mark.parametrize("module_file, status_code, constant", DISCLOSURE_SITES)
    def test_a_deliberate_refusal_is_re_raised_unchanged(
        self, module_file, status_code, constant
    ):
        """``except HTTPException: raise`` comes first, so a 404 is not flattened."""
        tree = _parse(BACKEND_APP / "api" / module_file)
        handlers = [
            node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
        ]
        assert [getattr(handler.type, "id", None) for handler in handlers] == [
            "HTTPException",
            "Exception",
        ], "%s does not re-raise a deliberate refusal before catching everything" % module_file
        assert isinstance(handlers[0].body[0], ast.Raise)
        assert handlers[0].body[0].exc is None

    @pytest.mark.parametrize("module_file, status_code, constant", DISCLOSURE_SITES)
    def test_the_cause_is_recorded_server_side(self, module_file, status_code, constant):
        """Suppressing the text from the response is only safe if it is kept somewhere."""
        source = code_only((BACKEND_APP / "api" / module_file).read_text(encoding="utf-8"))
        collapsed = " ".join(source.split())
        assert "logger = logging.getLogger(__name__)" in collapsed
        assert "logger.exception(" in collapsed, (
            "%s suppresses the cause without recording it" % module_file
        )

    @pytest.mark.parametrize("module_file, status_code, constant", DISCLOSURE_SITES)
    def test_the_module_logger_reaches_the_configured_handler(
        self, module_file, status_code, constant
    ):
        """``backend.app.api.*`` propagates to the logger the entry point configures."""
        from backend.app.core.logging_config import APPLICATION_LOGGER_NAME

        module_logger = "backend.app.api." + module_file[: -len(".py")]
        assert module_logger.startswith(APPLICATION_LOGGER_NAME + ".")

    def test_a_recorded_cause_carries_the_exception_and_the_identifiers(self, captured_logs):
        """The log record has to be enough to diagnose what the response no longer says."""
        handler = captured_logs("backend.app.api.probe")
        logger = logging.getLogger("backend.app.api.probe")
        try:
            raise ValueError("A string literal cannot contain NUL (0x00) characters.")
        except Exception:
            logger.exception("Cell update failed for workbook %s worksheet %s", "248", "859")

        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.levelno == logging.ERROR
        assert "248" in record.getMessage() and "859" in record.getMessage()
        assert record.exc_info is not None
        assert "NUL (0x00)" in logging.Formatter().formatException(record.exc_info)



# ===========================================================================
# Issue 5 - a large response is not put on the wire uncompressed
# ===========================================================================


class TestResponsesAreCompressed:
    """Issue 5: the bytes a single request costs are bounded by more than the row count.

    The default workbook page was measured at 2,742,333 bytes with no ``Content-Encoding`` for
    any ``Accept-Encoding`` the caller offered. Bounding the *page* (Issue 3) bounds the row
    count; this bounds what a bounded page costs to deliver.
    """

    @pytest.fixture
    def client(self):
        return TestClient(_build_application(), raise_server_exceptions=False)

    def test_a_large_response_is_compressed_when_the_caller_accepts_it(self, client):
        response = client.get("/bulky", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"

    def test_the_compressed_body_is_smaller_on_the_wire(self, client):
        """``httpx`` decodes for us, so the wire size is read from Content-Length."""
        compressed = client.get("/bulky", headers={"Accept-Encoding": "gzip"})
        plain = client.get("/bulky", headers={"Accept-Encoding": "identity"})
        assert int(compressed.headers["content-length"]) < int(
            plain.headers["content-length"]
        )

    def test_the_decoded_body_is_unchanged(self, client):
        """Compression is a transport concern; the payload a client parses is identical."""
        compressed = client.get("/bulky", headers={"Accept-Encoding": "gzip"})
        plain = client.get("/bulky", headers={"Accept-Encoding": "identity"})
        assert compressed.json() == plain.json()

    def test_a_caller_that_does_not_accept_it_is_not_sent_it(self, client):
        response = client.get("/bulky", headers={"Accept-Encoding": "identity"})
        assert response.status_code == 200
        assert "content-encoding" not in response.headers

    def test_the_response_varies_on_the_accepted_encoding(self, client):
        """Without this a cache can serve a compressed body to a client that cannot read it."""
        for accept in ("gzip", "identity"):
            response = client.get("/bulky", headers={"Accept-Encoding": accept})
            assert "accept-encoding" in response.headers["vary"].lower()

    def test_the_cross_origin_vary_entry_survives_alongside_it(self, client):
        """Both add to ``Vary`` rather than replacing it, so neither loses the other's entry."""
        response = client.get(
            "/bulky",
            headers={"Accept-Encoding": "gzip", "Origin": "https://app.example.com"},
        )
        vary = [entry.strip().lower() for entry in response.headers["vary"].split(",")]
        assert "accept-encoding" in vary
        assert "origin" in vary

    def test_a_short_response_is_left_alone(self, client):
        """Below the threshold gzip framing costs more than it saves."""
        response = client.get("/probe", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert len(response.content) < main_compression_minimum_size()
        assert "content-encoding" not in response.headers

    @pytest.mark.parametrize("path, status", [("/refused", 401), ("/failing", 500)])
    def test_the_security_headers_survive_compression(self, client, path, status):
        response = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert response.status_code == status
        for header in _expected_header_names():
            assert header in response.headers

    def test_the_security_headers_survive_on_a_compressed_success(self, client):
        response = client.get("/bulky", headers={"Accept-Encoding": "gzip"})
        assert response.headers["content-encoding"] == "gzip"
        for header in _expected_header_names():
            assert header in response.headers

    def test_the_cross_origin_headers_survive_compression(self, client):
        response = client.get(
            "/bulky",
            headers={"Accept-Encoding": "gzip", "Origin": "https://app.example.com"},
        )
        assert response.headers["content-encoding"] == "gzip"
        assert response.headers["access-control-allow-origin"] == "https://app.example.com"
        assert response.headers["access-control-allow-credentials"] == "true"

    def test_a_preflight_is_not_compressed(self, client):
        """``CORSMiddleware`` answers it outside compression, so it is never rewritten."""
        response = client.options(
            "/bulky",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
                "Accept-Encoding": "gzip",
            },
        )
        assert response.status_code == 200
        assert "content-encoding" not in response.headers

    def test_a_throttling_refusal_is_not_compressed(self):
        """The tiers answer outside compression, and the refusal is far below the threshold."""
        from backend.app.core.rate_limit import RETRY_AFTER_HEADER

        client = TestClient(
            _build_application(default_limit="1/minute"), raise_server_exceptions=False
        )
        assert client.get("/probe", headers={"Accept-Encoding": "gzip"}).status_code == 200
        refused = client.get("/probe", headers={"Accept-Encoding": "gzip"})
        assert refused.status_code == 429
        assert "content-encoding" not in refused.headers
        assert refused.headers[RETRY_AFTER_HEADER.lower()] == "60"

    def test_the_entry_point_ships_the_measured_settings(self):
        """The values are named constants, so the reason for each is recorded once."""
        assert main_compression_minimum_size() == 500
        assert main_compression_level() == 6
        source = code_only((BACKEND_APP / "main.py").read_text(encoding="utf-8"))
        collapsed = " ".join(source.split())
        assert "minimum_size=COMPRESSION_MINIMUM_SIZE" in collapsed
        assert "compresslevel=COMPRESSION_LEVEL" in collapsed

    def test_the_level_stays_below_the_librarys_default(self):
        """Compression runs on the event loop, so the level is a latency decision.

        Measured on the real 2,742,333-byte page: level 6 reached 74.8x in 16.9 ms where the
        library default of 9 reached 110.9x in 20.1 ms. The extra ratio depends on how
        repetitive the data is; the extra event-loop time does not.
        """
        assert 1 <= main_compression_level() < 9

    def test_a_body_that_already_declares_an_encoding_is_not_re_encoded(self):
        """Double encoding would produce a body no client can read.

        The response below is genuinely gzip - a body that merely *claims* an encoding it does
        not have proves nothing, because a client cannot decode it either way.
        """
        import gzip as gzip_module
        from starlette.responses import Response

        plain = b"a repetitive payload " * 400
        already = gzip_module.compress(plain)

        app = FastAPI()

        @app.get("/preencoded")
        def preencoded():
            return Response(
                content=already,
                media_type="application/octet-stream",
                headers={"Content-Encoding": "gzip"},
            )

        app.add_middleware(
            GZipMiddleware,
            minimum_size=main_compression_minimum_size(),
            compresslevel=main_compression_level(),
        )
        response = TestClient(app).get("/preencoded", headers={"Accept-Encoding": "gzip"})
        assert response.headers["content-encoding"] == "gzip"
        # One layer of decoding recovers the payload, so exactly one was applied.
        assert response.content == plain
        assert int(response.headers["content-length"]) == len(already)
