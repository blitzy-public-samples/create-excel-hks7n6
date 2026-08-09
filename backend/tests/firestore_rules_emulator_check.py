"""Executable acceptance assertions for ``firestore.rules`` (V10).

Run under the Firebase emulator, which is the only place these rules can be evaluated:
Firestore security rules run inside Google's rules engine, and the backend's
``firestore.Client()`` is a server client library that bypasses rules entirely, so no
in-process test can reach them.

    firebase emulators:exec --only firestore --project demo-excel-clone \
        "python backend/tests/firestore_rules_emulator_check.py"

Pass condition: the process exits 0 and prints ``ALL n ASSERTIONS PASSED``. Any denied
request that should have been allowed, or any allowed request that should have been
denied, prints the offending case and exits 1.

Uses only the Python standard library, so it adds no dependency to either manifest and in
particular does not introduce the ``@firebase/rules-unit-testing`` frontend dependency.
Documents are seeded through the emulator's owner credential, which bypasses rules, so the
fixtures themselves are never subject to the policy under test.

The emulator address is read from ``FIRESTORE_EMULATOR_HOST``, which
``firebase emulators:exec`` sets, and the project from ``--project`` via
``GCLOUD_PROJECT``/``FIREBASE_PROJECT``. Nothing here contacts a real Firebase project.

Prerequisites: the Firebase CLI and a Java runtime at version 21 or above. Current
firebase-tools releases refuse to start the Firestore emulator on Java 17 or below, so
``JAVA_HOME`` must point at a JDK/JRE 21+ install before ``emulators:exec`` will run.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

EMULATOR_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
PROJECT_ID = (
    os.environ.get("GCLOUD_PROJECT")
    or os.environ.get("FIREBASE_PROJECT")
    or os.environ.get("GOOGLE_CLOUD_PROJECT")
    or "demo-excel-clone"
)
DOCUMENTS_URL = "http://{0}/v1/projects/{1}/databases/(default)/documents".format(
    EMULATOR_HOST, PROJECT_ID
)

# The emulator treats this bearer value as an administrative credential and does not
# evaluate rules for it. Used only to seed and to clear fixtures.
OWNER_CREDENTIAL = "owner"

OWNER_UID = "owner-uid"
COLLABORATOR_UID = "collaborator-uid"
STRANGER_UID = "stranger-uid"

ALLOWED = "allowed"
DENIED = "denied"


def unsigned_identity_token(uid: str) -> str:
    """Return an unsigned Firebase-shaped ID token for ``uid``.

    The emulator accepts a token with an empty signature and evaluates rules against its
    claims, which is how a rules test presents a signed-in caller without a real project.
    """

    def segment(payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    issued_at = int(time.time())
    header = {"alg": "none", "kid": "", "typ": "JWT"}
    claims = {
        "iss": "https://securetoken.google.com/{0}".format(PROJECT_ID),
        "aud": PROJECT_ID,
        "iat": issued_at,
        "exp": issued_at + 3600,
        "auth_time": issued_at,
        "sub": uid,
        "user_id": uid,
        "email": "{0}@example.com".format(uid),
        "email_verified": True,
        "firebase": {"identities": {}, "sign_in_provider": "custom"},
    }
    return "{0}.{1}.".format(segment(header), segment(claims))


def to_firestore_value(value: Any) -> Dict[str, Any]:
    """Convert a Python value to the Firestore REST ``Value`` representation."""
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, list):
        return {"arrayValue": {"values": [to_firestore_value(item) for item in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": to_firestore_fields(value)}}
    if value is None:
        return {"nullValue": None}
    raise TypeError("unsupported fixture value: {0!r}".format(value))


def to_firestore_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a mapping to Firestore REST ``fields``."""
    return {name: to_firestore_value(value) for name, value in data.items()}


def request(
    method: str,
    path: str,
    caller: Optional[str],
    body: Optional[Dict[str, Any]] = None,
    query: str = "",
) -> Tuple[int, str]:
    """Issue one emulator request and return its status code and body.

    ``caller`` is a bearer value: ``None`` for an anonymous request,
    :data:`OWNER_CREDENTIAL` for the rules-bypassing seed credential, or an unsigned
    identity token for a signed-in caller.
    """
    url = "{0}/{1}{2}".format(DOCUMENTS_URL, path, query)
    payload = None if body is None else json.dumps(body).encode("utf-8")
    http_request = urllib.request.Request(url, data=payload, method=method)
    http_request.add_header("Content-Type", "application/json")
    if caller is not None:
        http_request.add_header("Authorization", "Bearer {0}".format(caller))
    try:
        with urllib.request.urlopen(http_request) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def outcome(status: int) -> str:
    """Classify a status code as an allowed or denied request."""
    if 200 <= status < 300:
        return ALLOWED
    if status in (401, 403):
        return DENIED
    raise AssertionError(
        "the emulator answered {0}, which is neither an allow nor a deny. Is the "
        "Firestore emulator running at {1}?".format(status, EMULATOR_HOST)
    )


def seed(document_id: str, data: Dict[str, Any]) -> None:
    """Create or replace ``workbooks/{document_id}``, bypassing the rules."""
    request("DELETE", "workbooks/{0}".format(document_id), OWNER_CREDENTIAL)
    status, body = request(
        "POST",
        "workbooks",
        OWNER_CREDENTIAL,
        {"fields": to_firestore_fields(data)},
        "?documentId={0}".format(document_id),
    )
    if outcome(status) != ALLOWED:
        raise AssertionError(
            "could not seed workbooks/{0}: {1} {2}".format(document_id, status, body)
        )


def workbook_content(name: str = "Quarterly") -> Dict[str, Any]:
    """Content fields a collaborator is permitted to write."""
    return {
        "name": name,
        "worksheets": {"ws-1": {"cells": {"A1": {"value": "1"}}}},
        "settings": {"locale": "en"},
    }


def authorized_workbook() -> Dict[str, Any]:
    """A document carrying the complete authorization contract."""
    data = workbook_content()
    data["ownerUid"] = OWNER_UID
    data["collaboratorUids"] = [COLLABORATOR_UID]
    return data


FAILURES: List[str] = []
CHECKS = 0


def expect(description: str, expected: str, status: int, body: str = "") -> None:
    """Record whether a request's outcome matched ``expected``."""
    global CHECKS
    CHECKS += 1
    try:
        actual = outcome(status)
    except AssertionError as error:
        FAILURES.append("{0}: {1}".format(description, error))
        return
    if actual != expected:
        FAILURES.append(
            "{0}: expected {1}, got {2} (HTTP {3}) {4}".format(
                description, expected, actual, status, body[:200]
            )
        )
    else:
        print("  ok   {0} -> {1}".format(description, actual))


def patch(
    document_id: str, caller: Optional[str], data: Dict[str, Any]
) -> Tuple[int, str]:
    """Patch exactly the supplied fields of a workbook document."""
    mask = "&".join(
        "updateMask.fieldPaths={0}".format(field) for field in sorted(data)
    )
    return request(
        "PATCH",
        "workbooks/{0}".format(document_id),
        caller,
        {"fields": to_firestore_fields(data)},
        "?{0}".format(mask),
    )


def create(
    document_id: str, caller: Optional[str], data: Dict[str, Any]
) -> Tuple[int, str]:
    """Attempt to create a workbook document as ``caller``."""
    request("DELETE", "workbooks/{0}".format(document_id), OWNER_CREDENTIAL)
    return request(
        "POST",
        "workbooks",
        caller,
        {"fields": to_firestore_fields(data)},
        "?documentId={0}".format(document_id),
    )


def check_reads(owner: str, collaborator: str, stranger: str) -> None:
    """Reads are confined to the owner and the listed collaborators."""
    print("reads")
    seed("wb-read", authorized_workbook())
    expect("anonymous read", DENIED, *request("GET", "workbooks/wb-read", None))
    expect("stranger read", DENIED, *request("GET", "workbooks/wb-read", stranger))
    expect("owner read", ALLOWED, *request("GET", "workbooks/wb-read", owner))
    expect(
        "collaborator read", ALLOWED, *request("GET", "workbooks/wb-read", collaborator)
    )


def check_documents_without_the_contract(owner: str, collaborator: str) -> None:
    """A document that carries no authorization contract denies every caller."""
    print("documents missing the authorization contract")
    seed("wb-no-fields", workbook_content())
    expect(
        "read of a document with no authorization fields",
        DENIED,
        *request("GET", "workbooks/wb-no-fields", owner),
    )

    seed("wb-owner-only", dict(workbook_content(), ownerUid=OWNER_UID))
    expect(
        "read of a document missing collaboratorUids",
        ALLOWED,
        *request("GET", "workbooks/wb-owner-only", owner),
    )
    expect(
        "collaborator read of a document missing collaboratorUids",
        DENIED,
        *request("GET", "workbooks/wb-owner-only", collaborator),
    )

    # A map stored under collaboratorUids would satisfy the `in` operator through its keys
    # if the field were not type-checked first.
    seed(
        "wb-malformed",
        dict(
            workbook_content(),
            ownerUid=OWNER_UID,
            collaboratorUids={COLLABORATOR_UID: True},
        ),
    )
    expect(
        "collaborator read where collaboratorUids is a map, not a list",
        DENIED,
        *request("GET", "workbooks/wb-malformed", collaborator),
    )

    seed("wb-numeric-owner", dict(workbook_content(), ownerUid=7, collaboratorUids=[]))
    expect(
        "read where ownerUid is not a string",
        DENIED,
        *request("GET", "workbooks/wb-numeric-owner", owner),
    )


def check_updates(owner: str, collaborator: str, stranger: str) -> None:
    """Content is writable by the owner and collaborators; authorization is not."""
    print("updates")
    seed("wb-update", authorized_workbook())
    expect(
        "owner content update",
        ALLOWED,
        *patch("wb-update", owner, {"name": "Renamed by owner"}),
    )
    expect(
        "collaborator content update",
        ALLOWED,
        *patch("wb-update", collaborator, {"name": "Renamed by collaborator"}),
    )
    expect(
        "stranger content update", DENIED, *patch("wb-update", stranger, {"name": "no"})
    )
    expect(
        "anonymous content update", DENIED, *patch("wb-update", None, {"name": "no"})
    )

    print("authorization-field tampering")
    expect(
        "collaborator adds itself to collaboratorUids",
        DENIED,
        *patch(
            "wb-update", collaborator, {"collaboratorUids": [COLLABORATOR_UID, STRANGER_UID]}
        ),
    )
    expect(
        "collaborator reassigns ownership",
        DENIED,
        *patch("wb-update", collaborator, {"ownerUid": COLLABORATOR_UID}),
    )
    expect(
        "owner reassigns ownership away from itself",
        DENIED,
        *patch("wb-update", owner, {"ownerUid": STRANGER_UID}),
    )
    expect(
        "owner changes the collaborator list",
        ALLOWED,
        *patch("wb-update", owner, {"collaboratorUids": [COLLABORATOR_UID, STRANGER_UID]}),
    )

    print("authorization fields cannot be dropped or retyped")
    seed("wb-drop", authorized_workbook())
    replacement = workbook_content("Rewritten")
    replacement["ownerUid"] = OWNER_UID
    expect(
        "owner rewrites the document without collaboratorUids",
        DENIED,
        *request(
            "PATCH",
            "workbooks/wb-drop",
            owner,
            {"fields": to_firestore_fields(replacement)},
        ),
    )
    expect(
        "owner retypes collaboratorUids to a string",
        DENIED,
        *patch("wb-drop", owner, {"collaboratorUids": "everyone"}),
    )
    expect(
        "owner empties ownerUid",
        DENIED,
        *patch("wb-drop", owner, {"ownerUid": ""}),
    )


def check_creates(owner: str, stranger: str) -> None:
    """A client may only create a workbook it owns, with a complete contract."""
    print("creates")
    expect(
        "owner creates a workbook it owns",
        ALLOWED,
        *create("wb-create", owner, authorized_workbook()),
    )
    expect(
        "signed-in caller creates a workbook owned by somebody else",
        DENIED,
        *create("wb-create-foreign", stranger, authorized_workbook()),
    )
    expect(
        "create with no authorization fields",
        DENIED,
        *create("wb-create-bare", owner, workbook_content()),
    )
    expect(
        "create with an empty ownerUid",
        DENIED,
        *create(
            "wb-create-empty-owner",
            owner,
            dict(workbook_content(), ownerUid="", collaboratorUids=[]),
        ),
    )
    expect(
        "anonymous create",
        DENIED,
        *create("wb-create-anon", None, authorized_workbook()),
    )


def check_deletes(owner: str, collaborator: str, stranger: str) -> None:
    """Destroying a workbook is owner-only."""
    print("deletes")
    seed("wb-delete", authorized_workbook())
    expect(
        "anonymous delete", DENIED, *request("DELETE", "workbooks/wb-delete", None)
    )
    expect(
        "stranger delete", DENIED, *request("DELETE", "workbooks/wb-delete", stranger)
    )
    expect(
        "collaborator delete",
        DENIED,
        *request("DELETE", "workbooks/wb-delete", collaborator),
    )
    expect(
        "owner delete", ALLOWED, *request("DELETE", "workbooks/wb-delete", owner)
    )


def check_unmatched_paths(owner: str) -> None:
    """Any path the rules do not match is denied by Firestore's default."""
    print("unmatched paths")
    expect(
        "read of a collection no rule matches",
        DENIED,
        *request("GET", "secrets/anything", owner),
    )
    expect(
        "write to a collection no rule matches",
        DENIED,
        *create_in("secrets", "anything", owner),
    )
    expect(
        "read of a subcollection under a workbook",
        DENIED,
        *request("GET", "workbooks/wb-read/revisions/r1", owner),
    )


def create_in(collection: str, document_id: str, caller: str) -> Tuple[int, str]:
    """Attempt to create a document in an arbitrary collection."""
    return request(
        "POST",
        collection,
        caller,
        {"fields": to_firestore_fields({"name": "x"})},
        "?documentId={0}".format(document_id),
    )


def main() -> int:
    print(
        "Evaluating firestore.rules against the emulator at {0} "
        "for project {1}".format(EMULATOR_HOST, PROJECT_ID)
    )
    owner = unsigned_identity_token(OWNER_UID)
    collaborator = unsigned_identity_token(COLLABORATOR_UID)
    stranger = unsigned_identity_token(STRANGER_UID)

    check_reads(owner, collaborator, stranger)
    check_documents_without_the_contract(owner, collaborator)
    check_updates(owner, collaborator, stranger)
    check_creates(owner, stranger)
    check_deletes(owner, collaborator, stranger)
    check_unmatched_paths(owner)

    if FAILURES:
        print("")
        print("{0} of {1} ASSERTIONS FAILED".format(len(FAILURES), CHECKS))
        for failure in FAILURES:
            print("  FAIL {0}".format(failure))
        return 1
    print("")
    print("ALL {0} ASSERTIONS PASSED".format(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
