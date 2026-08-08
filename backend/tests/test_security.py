"""Regression tests for the security remediation (V1-V12 and the review findings).

Scope note, stated plainly because it decides how several tests are written:
``backend.app.main`` and the four route modules **cannot be imported**. They do
``from backend.app.db import get_db``, ``from backend.app.schema import ...`` and
``from backend.app.services import ...``, none of which resolves because no package under
``backend/`` carries an ``__init__.py`` and the named service classes do not exist. Those
are pre-existing blockers outside this change set's scope.

So the controls are verified two ways, and both are real verification rather than a
substitute for it:

* Behaviour is exercised against the middleware stack and the dependency themselves,
  composed on a throwaway application in exactly the order ``backend/app/main.py``
  registers them.
* The shape of the files that cannot be imported - the authentication dependency on each
  handler, the unchanged route contracts, and the registration order in the entry point -
  is asserted by parsing their source, so a change to any of them fails a test.
"""

import ast
import importlib
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pydantic
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = REPOSITORY_ROOT / "backend" / "app"

#: Route module, handler name, decorator method and path, as delivered.
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


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("no function named %r" % name)


# ===========================================================================
# V2 - the security module imports at all
# ===========================================================================
class TestSecurityModuleImportability:
    """V2: ``Optional`` was annotated but never imported, so the module raised at import."""

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
    """V1: ``get_current_user`` was defined and referenced nowhere."""

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
        # No response_model or status_code was introduced alongside the dependency.
        assert [keyword.arg for keyword in decorator.keywords] == []

    @pytest.mark.parametrize("module_file, handler, method, path", ROUTE_CONTRACTS)
    def test_current_user_is_the_last_parameter(self, module_file, handler, method, path):
        """The dependency is appended, so no existing parameter's position moved."""
        tree = _parse(BACKEND_APP / "api" / module_file)
        function = _function(tree, handler)
        assert function.args.args[-1].arg == "current_user"

    def test_no_token_route_was_added(self):
        tree = _parse(BACKEND_APP / "main.py")
        source = (BACKEND_APP / "main.py").read_text(encoding="utf-8")
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
        response = probe_client.get("/protected")
        assert response.json() == {"detail": "Could not validate credentials"}

    def test_refusal_is_recorded_server_side(self, probe_client, captured_logs):
        handler = captured_logs("backend.app.core.security")
        probe_client.get("/protected")
        messages = [record.getMessage() for record in handler.records]
        assert any("no bearer credential" in message for message in messages), messages


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
        provider_exception = HTTPException(
            status_code=503,
            detail=security.AUTH_PROVIDER_UNAVAILABLE_DETAIL,
            headers={"Retry-After": security.AUTH_PROVIDER_UNAVAILABLE_RETRY_AFTER_SECONDS},
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
                provider_exception,
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
    def test_provider_faults_are_503(self, verified_claims, index):
        fault = self._provider_faults()[index]
        with pytest.raises(HTTPException) as raised:
            verified_claims(raised=fault)
        assert raised.value.status_code == 503
        assert raised.value.headers["Retry-After"]

    def test_an_unconfigured_project_is_503(self, verified_claims):
        with pytest.raises(HTTPException) as raised:
            verified_claims(raised_at_initialisation=ValueError("no project"))
        assert raised.value.status_code == 503

    def test_unresolvable_credentials_are_503(self, verified_claims):
        from google.auth import exceptions as google_auth_exceptions

        with pytest.raises(HTTPException) as raised:
            verified_claims(
                raised_at_initialisation=google_auth_exceptions.DefaultCredentialsError(
                    "no ADC"
                )
            )
        assert raised.value.status_code == 503

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
                HTTPException(status_code=503, detail="unavailable"),
            )
        assert raised.value.status_code == 401


class TestIdentityClaimValidation:
    """V3: a claim the identity column cannot hold never reaches the query."""

    @pytest.fixture
    def resolve(self):
        security = importlib.import_module("backend.app.core.security")
        exception = HTTPException(status_code=401, detail="no")

        def _resolve(claims, verifier="firebase"):
            return security._resolve_identity(claims, verifier, exception)

        return _resolve

    def test_a_string_email_selects_the_email_column(self, resolve):
        from backend.app.db.models import User

        column, value = resolve({"email": "user@example.com"})
        assert column is User.email
        assert value == "user@example.com"

    @pytest.mark.parametrize(
        "claims",
        [{}, {"email": None}, {"email": ""}, {"email": 7}, {"email": "a\x00b"}],
    )
    def test_an_unusable_email_claim_is_refused(self, resolve, claims):
        with pytest.raises(HTTPException) as raised:
            resolve(claims)
        assert raised.value.status_code == 401

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


class TestAuthenticationEnforcementSwitch:
    """M10: the break-glass switch is read before a missing credential is refused."""

    @pytest.fixture
    def security(self):
        return importlib.import_module("backend.app.core.security")

    def test_bearer_extraction_does_not_refuse_by_itself(self, security):
        assert security.oauth2_scheme.auto_error is False

    def test_the_public_contract_is_unchanged(self, security):
        import inspect

        from backend.app.db.models import User

        signature = inspect.signature(security.get_current_user)
        assert list(signature.parameters) == ["token"]
        assert signature.return_annotation is User

    def test_a_missing_credential_is_refused_while_enforcement_is_enabled(
        self, security, set_settings
    ):
        set_settings(auth_enforcement_enabled="true")
        state = {security._BYPASS_STATE_KEY: False}
        with pytest.raises(HTTPException) as raised:
            security._resolve_current_user(None, state)
        assert raised.value.status_code == 401
        assert raised.value.headers["WWW-Authenticate"] == "Bearer"

    def test_a_missing_credential_is_not_refused_while_enforcement_is_disabled(
        self, security, set_settings
    ):
        """The 401 branch must not be taken, which is the whole point of the switch.

        Admission itself cannot complete in this tree: the placeholder caller is a
        ``User``, and configuring that mapper fails for the reason
        :class:`TestKnownResiduals` characterises. What is asserted here is therefore that
        the refusal branch is not the one taken.
        """
        set_settings(auth_enforcement_enabled="false")
        state = {security._BYPASS_STATE_KEY: False}
        with pytest.raises(Exception) as raised:
            security._resolve_current_user(None, state)
        assert not isinstance(raised.value, HTTPException), raised.value

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
    """V12: the lifetime was hardcoded to fifteen minutes and the setting never read."""

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

    def test_the_six_original_field_names_are_preserved(self, Settings):
        for name in (
            "PROJECT_ID",
            "DATABASE_URL",
            "REDIS_URL",
            "SECRET_KEY",
            "ALGORITHM",
            "ACCESS_TOKEN_EXPIRE_MINUTES",
        ):
            assert name in Settings.__fields__

    def test_the_application_declares_no_unread_api_origin(self, Settings):
        """M13: the field was declared, validated and never read by any Python code."""
        assert "api_origin" not in Settings.__fields__
        sources = list(BACKEND_APP.rglob("*.py"))
        assert sources
        for source in sources:
            assert "api_origin" not in source.read_text(encoding="utf-8"), source

    @pytest.mark.parametrize("value", ["firebase", "legacy_jwt"])
    def test_supported_verifiers_are_accepted(self, Settings, value):
        assert Settings(auth_token_verifier=value).auth_token_verifier == value

    @pytest.mark.parametrize("value", ["firebse", "legacy-jwt", "", "FIREBASE"])
    def test_an_unsupported_verifier_is_refused(self, Settings, value):
        """N1: anything but ``legacy_jwt`` silently took the Firebase path."""
        with pytest.raises(pydantic.ValidationError):
            Settings(auth_token_verifier=value)

    @pytest.mark.parametrize("value", [0, -1, 1441, 100000])
    def test_an_out_of_range_token_lifetime_is_refused(self, Settings, value):
        """N4: the setting is honoured now, so it needs a bound."""
        with pytest.raises(pydantic.ValidationError):
            Settings(ACCESS_TOKEN_EXPIRE_MINUTES=value)

    @pytest.mark.parametrize("value", [1, 15, 1440])
    def test_an_in_range_token_lifetime_is_accepted(self, Settings, value):
        assert Settings(ACCESS_TOKEN_EXPIRE_MINUTES=value).ACCESS_TOKEN_EXPIRE_MINUTES == value

    @pytest.mark.parametrize("value", ["disable", "allow", "prefer", "", "REQUIRE"])
    def test_a_database_mode_that_can_negotiate_plaintext_is_refused(self, Settings, value):
        """V6: the mode reaches psycopg2 unchecked, so it is constrained here."""
        with pytest.raises(pydantic.ValidationError):
            Settings(db_sslmode=value)

    @pytest.mark.parametrize("value", ["require", "verify-ca", "verify-full"])
    def test_an_encrypting_database_mode_is_accepted(self, Settings, value):
        assert Settings(db_sslmode=value).db_sslmode == value

    @pytest.mark.parametrize(
        "origins",
        [
            ["*"],
            ["https://*.example.com"],
            ["http://app.example.com"],
            ["https://app.example.com/api"],
            ["app.example.com"],
            ["https://user:pass@app.example.com"],
            [""],
        ],
    )
    def test_a_weak_cors_origin_is_refused(self, Settings, origins):
        """V5: the credentialed policy was built from an unvalidated list."""
        with pytest.raises(pydantic.ValidationError):
            Settings(ALLOWED_ORIGINS=origins)

    def test_loopback_http_stays_usable_for_development(self, Settings):
        assert Settings(
            ALLOWED_ORIGINS=["http://localhost:3000", "https://app.example.com"]
        ).ALLOWED_ORIGINS == ["http://localhost:3000", "https://app.example.com"]

    @pytest.mark.parametrize("value", [0, -1, 10081])
    def test_an_out_of_range_signed_url_lifetime_is_refused(self, Settings, value):
        with pytest.raises(pydantic.ValidationError):
            Settings(signed_url_expiry_minutes=value)

    @pytest.mark.parametrize("value", ["not-an-address", "10.0.0.0/99", "", "example.com"])
    def test_an_unparseable_trusted_proxy_is_refused(self, Settings, value):
        """M15: an entry that never matches would silently disable the control."""
        with pytest.raises(pydantic.ValidationError):
            Settings(rate_limit_trusted_proxies=[value])

    def test_a_trusted_proxy_address_is_normalised_to_a_network(self, Settings):
        assert Settings(
            rate_limit_trusted_proxies=["35.191.0.5", "130.211.0.0/22", "::1"]
        ).rate_limit_trusted_proxies == ["35.191.0.5/32", "130.211.0.0/22", "::1/128"]

    def test_the_hop_counting_field_is_gone(self, Settings):
        assert "rate_limit_trusted_proxy_hops" not in Settings.__fields__


# ===========================================================================
# V6 - the database connection requires TLS
# ===========================================================================
class TestDatabaseTransportSecurity:
    """V6: ``create_engine`` was called with no transport arguments at all.

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
        from backend.app.core.config import get_settings

        assert get_settings().db_sslmode in ("require", "verify-ca", "verify-full")

    def test_the_engine_and_the_mode_come_from_one_settings_read(self):
        """Two reads would mean two Secret Manager fetches and two chances to disagree."""
        source = (BACKEND_APP / "db" / "database.py").read_text(encoding="utf-8")
        assert source.count("get_settings()") == 1, source


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
    """V4: every uploaded object was world-readable by a permanent URL."""

    @pytest.fixture
    def service_for(self):
        from backend.app.services.file_storage import FileStorageService

        def _build(blob):
            service = FileStorageService.__new__(FileStorageService)
            service._bucket = _FakeBucket(blob)
            service._signed_url_expiration = timedelta(minutes=15)
            service._configured_signer = (
                "excel-app-url-signer@excel-clone-test.iam.gserviceaccount.com"
            )
            service._signing_credentials = _FakeSigningCredentials()
            service._auth_request = None
            return service

        return _build

    def test_no_public_acl_is_ever_granted(self, service_for):
        blob = _FakeBlob()
        service_for(blob).upload_file(b"content", blob.name)
        assert blob.made_public is False

    def test_the_source_contains_no_public_acl_call(self):
        source = (BACKEND_APP / "services" / "file_storage.py").read_text(encoding="utf-8")
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

    def test_signing_goes_through_the_configured_signer(self, service_for):
        blob = _FakeBlob()
        service_for(blob).upload_file(b"content", blob.name)
        assert blob.signed_with["service_account_email"].startswith("excel-app-url-signer@")
        assert blob.signed_with["access_token"] == "an-access-token"

    def test_an_object_that_cannot_be_signed_is_removed(self, service_for):
        blob = _FakeBlob(signing_fails=True)
        with pytest.raises(RuntimeError):
            service_for(blob).upload_file(b"content", blob.name)
        assert blob.deleted_generation == blob.generation


class TestStorageLogSanitisation:
    """N3: the cleanup log carried a caller-supplied object name verbatim."""

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
    write_limit="120/minute",
    throttling_enabled=True,
    trusted_proxies=(),
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
    os.environ["rate_limit_trusted_proxies"] = "[%s]" % ",".join(
        '"%s"' % proxy for proxy in trusted_proxies
    )

    app = FastAPI()

    @app.get("/probe")
    def probe():
        return {"ok": True}

    @app.post("/probe")
    def probe_write():
        return {"ok": True}

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
    """V7 and M8: no response carried a security header, and a 500 escaped the stack."""

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


class TestCrossOriginPolicy:
    """V5: the preflight echoed any origin and reflected any requested header."""

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
    """V9, M14 and N2: request volume was unbounded on every route."""

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

    @pytest.mark.parametrize(
        "default_limit, write_limit, method",
        [("1000/minute", "1/minute", "post"), ("1/minute", "1000/minute", "get")],
    )
    def test_a_refusal_reaches_the_browser_with_its_cors_headers(
        self, default_limit, write_limit, method
    ):
        """M14: a 429 produced inside CORS surfaced as an opaque cross-origin failure."""
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
        """N2: a wholly unthrottled deployment was indistinguishable from an enforcing one."""
        handler = captured_logs("backend.app.core.rate_limit")
        app = _build_application(throttling_enabled=False)
        warnings = [
            record.getMessage()
            for record in handler.records
            if record.levelno == logging.WARNING
        ]
        assert any("DISABLED" in message for message in warnings), warnings
        assert app.state.rate_limit_enabled is False
        assert not hasattr(app.state, "limiter")

    def test_enabling_throttling_is_recorded_too(self):
        assert _build_application().state.rate_limit_enabled is True


class TestThrottlingIdentity:
    """M15: a forwarded chain was trusted by position, so a quota key could be forged."""

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

    def test_with_no_trusted_proxy_the_socket_peer_is_used(self, resolve):
        scope = self._scope("203.0.113.9", "1.2.3.4, 5.6.7.8")
        assert resolve(scope, ()) == "203.0.113.9"

    def test_an_untrusted_peer_cannot_choose_its_quota_key(self, resolve):
        scope = self._scope("203.0.113.9", "9.9.9.9")
        assert resolve(scope, ("10.0.0.0/8",)) == "203.0.113.9"

    def test_a_forged_chain_of_any_length_is_ignored(self, resolve):
        scope = self._scope("203.0.113.9", "1.1.1.1, 2.2.2.2, 3.3.3.3, 4.4.4.4")
        assert resolve(scope, ("10.0.0.0/8",)) == "203.0.113.9"

    def test_a_trusted_peer_yields_the_rightmost_untrusted_entry(self, resolve):
        scope = self._scope("10.1.2.3", "198.51.100.7, 10.4.5.6")
        assert resolve(scope, ("10.0.0.0/8",)) == "198.51.100.7"

    def test_entries_a_client_prepended_are_never_reached(self, resolve):
        scope = self._scope("10.1.2.3", "1.1.1.1, 198.51.100.7, 10.4.5.6")
        assert resolve(scope, ("10.0.0.0/8",)) == "198.51.100.7"

    def test_a_chain_of_only_trusted_proxies_falls_back_to_the_peer(self, resolve):
        scope = self._scope("10.1.2.3", "10.9.9.9, 10.4.5.6")
        assert resolve(scope, ("10.0.0.0/8",)) == "10.1.2.3"

    def test_a_trusted_peer_sending_no_chain_is_metered_on_itself(self, resolve):
        assert resolve(self._scope("10.1.2.3"), ("10.0.0.0/8",)) == "10.1.2.3"

    def test_an_exact_address_may_be_trusted(self, resolve):
        scope = self._scope("35.191.0.5", "198.51.100.7, 35.191.0.5")
        assert resolve(scope, ("35.191.0.5",)) == "198.51.100.7"

    def test_a_request_with_no_address_shares_one_budget(self, resolve):
        assert resolve({"type": "http", "headers": []}, ()) == "unknown"

    def test_a_malformed_forwarded_entry_is_not_trusted(self, resolve):
        scope = self._scope("10.1.2.3", "not-an-address, 10.4.5.6")
        assert resolve(scope, ("10.0.0.0/8",)) == "not-an-address"


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
            "SlowAPIMiddleware",
            "_WriteMethodRateLimitMiddleware",
            "_RequestBodySizeLimitMiddleware",
            "AuthEnforcementBypassMarkerMiddleware",
            "ServerErrorBoundaryMiddleware",
        ]

    def test_the_entry_point_registers_them_in_that_order(self):
        """The order is a contract, so a later reordering has to fail a test."""
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
    """V10: no rules file existed while the browser reached Firestore directly."""

    @pytest.fixture
    def rules(self):
        return (REPOSITORY_ROOT / "firestore.rules").read_text(encoding="utf-8")

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
            "upgrade-insecure-requests",
        ):
            assert directive in document_policy, directive
        assert document_policy["script-src"] == ["'self'"]
        assert document_policy["object-src"] == ["'none'"]

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


class TestDeploymentSurface:
    """V8 and V11: the edge served plaintext and the function was publicly invocable."""

    @pytest.fixture
    def deploy_script(self):
        return (REPOSITORY_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    def test_no_public_invoker_is_granted(self, deploy_script):
        assert "--allow-unauthenticated" not in deploy_script

    def test_no_plaintext_listener_is_created(self, deploy_script):
        assert "target-http-proxies create" not in deploy_script
        assert "--ports=80 " not in deploy_script

    def test_the_terraform_edge_terminates_tls(self):
        terraform = (
            REPOSITORY_ROOT / "infrastructure" / "terraform" / "main.tf"
        ).read_text(encoding="utf-8")
        assert "google_compute_managed_ssl_certificate" in terraform
        assert "google_compute_target_https_proxy" in terraform
        assert "ENCRYPTED_ONLY" in terraform
        assert "public_access_prevention" in terraform
        assert "uniform_bucket_level_access" in terraform


# ===========================================================================
# Residuals this change set is not permitted to close
# ===========================================================================
class TestKnownResiduals:
    """Characterises defects in reference-only modules, so closing one fails here.

    These are not assertions that the behaviour is desirable. Each one belongs to a module
    the change set may not edit, is recorded as a residual and a follow-up in
    ``documentation/Security Decision Log.md``, and is pinned here so that the follow-up
    landing is visible rather than silent.
    """

    def test_the_user_mapper_cannot_be_configured(self, in_memory_database):
        """``Workbook.owner`` declares ``back_populates='workbooks'`` and ``User`` has no
        such attribute, so every query against ``User`` - and therefore every admitted
        request - raises. Follow-up: add the reverse relationship."""
        from sqlalchemy.exc import InvalidRequestError

        from backend.app.db.models import User

        session = in_memory_database()
        try:
            with pytest.raises(InvalidRequestError, match="has no property 'workbooks'"):
                session.query(User).filter(User.email == "user@example.com").first()
        finally:
            session.close()

    def test_the_worksheet_schema_cannot_be_built_from_an_orm_row(self):
        """``WorksheetSchema`` has no ``orm_mode``, so the worksheets route's
        ``from_orm`` call raises. Follow-up: the absent ``WorksheetService`` owns the
        projection this needs."""
        from pydantic import ConfigError

        from backend.app.schema.workbook_schema import WorksheetSchema

        class _Row:
            id = "1"
            name = "Sheet1"
            cells = {}
            named_ranges = None

        with pytest.raises(ConfigError):
            WorksheetSchema.from_orm(_Row())

    def test_the_application_entry_point_cannot_be_imported(self):
        """No package under ``backend/`` carries ``__init__.py`` and the domain service
        classes do not exist, so the application cannot start. Follow-up: the excluded
        service layer and package structure."""
        with pytest.raises(ImportError):
            importlib.import_module("backend.app.main")
