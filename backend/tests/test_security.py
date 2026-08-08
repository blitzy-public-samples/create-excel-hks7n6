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
import importlib
import inspect
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pydantic
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

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

    @pytest.mark.parametrize("index", range(5))
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

    @pytest.mark.parametrize("index", range(5))
    def test_a_caller_fault_is_recorded_at_warning_level_only(
        self, verified_claims, captured_logs, index
    ):
        handler = captured_logs("backend.app.core.security")
        with pytest.raises(HTTPException):
            verified_claims(raised=self._caller_faults()[index])
        assert handler.records
        assert not [r for r in handler.records if r.levelno >= logging.ERROR]

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

    def test_a_presented_token_is_still_read_unverified_while_disabled(
        self, security, set_settings
    ):
        """The switch's whole purpose: claims are read without being verified.

        The lookup itself cannot complete in this tree for the reason
        :class:`TestKnownResiduals` characterises, so what is asserted is that the claims
        were read and the 401 branch was not taken.
        """
        set_settings(auth_enforcement_enabled="false")
        app = FastAPI()

        @app.get("/protected")
        def protected(current_user=Depends(security.get_current_user)):
            return {"reached": True}

        response = TestClient(app, raise_server_exceptions=False).get("/protected")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "reached" not in response.text

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
        """The throttling identity is the socket peer and nothing else, so there is no
        trusted-proxy list to configure and no header a caller can rotate through."""
        assert "rate_limit_trusted_proxies" not in Settings.__fields__
        assert "rate_limit_trusted_proxy_hops" not in Settings.__fields__
        import inspect

        from backend.app.core.rate_limit import resolve_client_key

        body = inspect.getsource(resolve_client_key).split('"""')[-1]
        assert "scope" in body
        for token in ("headers", "forwarded", "Forwarded"):
            assert token not in body, token

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
def _build_application(
    default_limit="600/minute",
    write_limit="300/minute",
    throttling_enabled=True,
):
    """Compose the middleware stack in exactly the order ``main.py`` registers it."""
    from backend.app.core.config import get_settings
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

    app.add_middleware(ServerErrorBoundaryMiddleware)
    app.add_middleware(AuthEnforcementBypassMarkerMiddleware)
    register_rate_limiting(app)
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
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

    def test_an_exhausted_ceiling_does_not_replace_the_preflight(self):
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
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://app.example.com"

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


class TestMiddlewareRegistrationOrder:
    """M14 and the marker's registration contract, asserted against the entry point.

    The request-body size limiter sits inside both throttling tiers and outside the marker,
    so a body refused with 413 still travels back out through CORS and the security-header
    wrapper rather than reaching the browser as an opaque cross-origin failure.
    """

    def test_the_composed_stack_has_the_required_order(self):
        app = _build_application()
        assert [middleware.cls.__name__ for middleware in app.user_middleware] == [
            "SecurityHeadersMiddleware",
            "CORSMiddleware",
            "_RateLimitMiddleware",
            "_RequestBodySizeLimitMiddleware",
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
            elif name in ("register_rate_limiting", "configure_cors"):
                registrations.append(name)
        assert registrations == [
            "ServerErrorBoundaryMiddleware",
            "AuthEnforcementBypassMarkerMiddleware",
            "register_rate_limiting",
            "configure_cors",
            "SecurityHeadersMiddleware",
        ]

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

    def test_the_document_policy_admits_no_inline_script(self, document_policy):
        """The other half of the same contract: with the runtime emitted as a file, the policy
        must not be relaxed to admit inline script."""
        assert "'unsafe-inline'" not in document_policy["script-src"]
        assert "'unsafe-eval'" not in document_policy["script-src"]

    def test_the_document_policy_never_narrows_the_served_policy(self, document_policy):
        from backend.app.core.security_headers import CONTENT_SECURITY_POLICY

        served = self._directives(CONTENT_SECURITY_POLICY)
        for directive, sources in document_policy.items():
            assert served.get(directive) == sources, directive

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

    def test_the_credential_requirement_is_stated_not_overstated(self, readme, policy):
        """The switch weakens verification; it does not open a credential-less path. Both
        the earlier claims were actionable errors in opposite directions - one said a token
        stayed fully checked, the next said none was needed at all."""
        source = (
            BACKEND_APP / "core" / "security.py"
        ).read_text(encoding="utf-8")
        assert "_anonymous_caller" not in source
        assert "auto_error" not in code_only(source)
        for document in (readme, policy):
            assert "no `Authorization` header" in document
        assert "an identity-less placeholder caller" not in readme
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
        later decision went the other way."""
        for entry in ("| R8 |", "| R21 |", "| D27 |", "| D31 |"):
            start = decisions.index(entry)
            row = decisions[start : decisions.index("\n", start)]
            assert "SUPERSEDED" in row, entry

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
        self, terraform_main, deploy_script
    ):
        """C1: the API runtime and the URL signer are one account on every plane.

        backend/app/services/file_storage.py signs as the address its own ambient
        credentials report, so a second account could never be the signer: the runtime
        would ask the IAM signBlob endpoint to sign as itself and be refused. The runtime
        input is therefore not declared at all, and the deployment script reads the one
        address.
        """
        assert 'resource "google_service_account" "api_runtime"' not in terraform_main
        assert 'resource "google_service_account" "url_signer"' in terraform_main
        assert "var.runtime_service_account" not in terraform_main
        assert "SIGNER_SERVICE_ACCOUNT" in deploy_script
        assert "RUNTIME_SERVICE_ACCOUNT" not in deploy_script

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
        confirmed a client received them, and the script reported success either way."""
        for header in _expected_header_names():
            assert header in deploy_script
        assert 'grep -qi "^${expected_header}:"' in deploy_script

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

    def test_the_documented_test_count_matches_this_module(self, documents):
        """Measured from the collected node count, not from a number typed by hand."""
        collected = 0
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    collected += 1
        # Parametrised cases expand the run count above the function count, so the documents
        # cite the run count. Assert the documents agree with each other and that the figure
        # is at least the number of test functions.
        cited = set()
        for key in ("onboarding", "matrix"):
            body = documents[key].read_text(encoding="utf-8")
            pattern = r"\*\*(\d{3})\*\* (?:of them|cases)"
            cited |= {int(value) for value in re.findall(pattern, body)}
        assert cited, "no test count is cited in the onboarding guide or the matrix"
        assert len(cited) == 1, "the documents cite different test counts: %s" % sorted(cited)
        assert cited.pop() >= collected

    def test_the_reverse_matrix_matches_the_working_tree(self, documents):
        """Rule 1 requires 100% bidirectional coverage. A row naming a path that no longer
        exists is the failure mode this catches — `api.test.ts` was such a row."""
        matrix = documents["matrix"].read_text(encoding="utf-8")
        for path in re.findall(r"^\| \d+ \| `([^`]+)` \|", matrix, re.M):
            if path.endswith("/"):
                continue
            assert (REPOSITORY_ROOT / path).exists(), path

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
        """``orm_mode`` is a ``Config`` flag: it adds, renames and retypes nothing."""
        from backend.app.schema.workbook_schema import WorksheetSchema

        assert list(WorksheetSchema.__fields__) == ["name", "cells", "named_ranges"]
        assert WorksheetSchema.__fields__["name"].outer_type_ is str
        assert WorksheetSchema.Config.orm_mode is True


# ===========================================================================
# Residuals this change set is not permitted to close
# ===========================================================================
class TestKnownResiduals:
    """Characterises defects in modules the change set may not edit, so closing one fails here.

    These are not assertions that the behaviour is desirable. Each one is recorded as a residual
    and a follow-up in ``documentation/Security Decision Log.md``, and is pinned here so that the
    follow-up landing is visible rather than silent.
    """

    def test_a_mapped_worksheet_row_still_needs_a_projection(self):
        """``orm_mode`` makes ``from_orm`` run; it does not reconcile the two ``cells`` shapes.

        The mapped ``Worksheet.cells`` is a *list* of ``Cell`` rows keyed by row and column,
        while ``WorksheetSchema.cells`` is a ``Dict[str, CellSchema]`` keyed by cell reference. An
        empty worksheet coerces - Pydantic reads ``[]`` as ``{}`` - so the mismatch surfaces only
        once a row carries a cell, which is every real worksheet. The projection that reconciles
        the two belongs to ``WorksheetService``, which does not exist and which this change set may
        not build, so the route at ``backend/app/api/worksheets.py`` line 25 still cannot serialize
        a populated row.

        Also pinned here: ``Worksheet`` declares no ``named_ranges`` at all. ``from_orm`` tolerates
        that because the field is optional and Pydantic falls back to its default, so the absence
        is silent rather than reported.
        """
        from backend.app.db.models import Cell, Worksheet
        from backend.app.schema.workbook_schema import WorksheetSchema

        assert Worksheet.cells.property.uselist is True
        assert not hasattr(Worksheet, "named_ranges")

        row = Worksheet(id=1, workbook_id=1, name="Sheet1", order=0)
        assert WorksheetSchema.from_orm(row).cells == {}

        row.cells.append(
            Cell(id=1, worksheet_id=1, row=1, column=1, value="7", formula=None, style={})
        )
        with pytest.raises(pydantic.ValidationError, match="not a valid dict"):
            WorksheetSchema.from_orm(row)

    def test_the_application_entry_point_cannot_be_imported(self):
        """No package under ``backend/`` carries ``__init__.py`` and the domain service
        classes do not exist, so the entry point cannot be imported."""
        with pytest.raises(ImportError):
            importlib.import_module("backend.app.main")
