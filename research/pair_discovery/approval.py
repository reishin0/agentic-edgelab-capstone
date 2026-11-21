"""Bind a one-use holdout run to verified approval and a frozen manifest.

The host supplies the receipt verifier, environment verifier and input reader.
This module provides no market data connector or approval issuer.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
import sqlite3
from threading import get_ident
from uuid import UUID

from edgelab.contracts import fingerprint
from edgelab.journal import Journal


ACTION = "read-only-historical-simulation"
UNIVERSE = "us-equity"
LEDGER = "equity-holdout/v1"
_HEX = re.compile(r"[0-9a-f]{64}")
_CODE_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_RESERVED_WINDOWS_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])", re.I)


class HoldoutDenied(ValueError):
    """The requested access does not satisfy the frozen authorization."""


def _shape(value, fields: str, label: str):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise HoldoutDenied(f"Invalid {label} fields")


def _matches(value, pattern, label: str):
    if type(value) is not str or not pattern.fullmatch(value):
        raise HoldoutDenied(f"Invalid {label}")


def _day(value) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HoldoutDenied("Invalid session date") from None
    if parsed.isoformat() != value:
        raise HoldoutDenied("Use canonical session dates")
    return parsed


def _relative_path(value):
    if not isinstance(value, str):
        raise HoldoutDenied("Use canonical relative paths")
    for part in value.split("/"):
        _matches(part, _ID, "relative path")
        if part.endswith(".") or _RESERVED_WINDOWS_NAME.fullmatch(part.split(".")[0]):
            raise HoldoutDenied("Use portable relative paths")


def _digests(value, *, paths=False):
    if type(value) is not dict or not value:
        raise HoldoutDenied("Missing frozen input or code hashes")
    for name, digest in value.items():
        if paths:
            _relative_path(name)
        else:
            _matches(name, _ID, "input ID")
        _matches(digest, _HEX, "SHA-256")
    if len({key.casefold() for key in value}) != len(value):
        raise HoldoutDenied("Duplicate input or code names")


def _run_id(value):
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HoldoutDenied("Invalid run UUID") from None
    if str(parsed) != value or parsed.int == 0:
        raise HoldoutDenied("Use a canonical nonzero run UUID")


def _json_data(value) -> None:
    """Reject coercions before hashing, including tuple and object-key aliases."""
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise HoldoutDenied("Manifest object keys must be strings")
        for child in value.values():
            _json_data(child)
    elif type(value) is list:
        for child in value:
            _json_data(child)
    elif value is not None and type(value) not in (str, bool, int):
        if type(value) is not float or not isfinite(value):
            raise HoldoutDenied("Manifest requires finite JSON values")


def validate_manifest(manifest: dict) -> None:
    """Validate an approval manifest from metadata alone."""
    _json_data(manifest)
    _shape(manifest, "schema run_id action protocol code development parameters holdout candidates output_directory",
           "manifest")
    if type(manifest["schema"]) is not int or manifest["schema"] != 1 or manifest["action"] != ACTION:
        raise HoldoutDenied("Unsupported holdout action or manifest version")
    _run_id(manifest["run_id"])
    _relative_path(manifest["output_directory"])

    protocol = manifest["protocol"]
    _shape(protocol, "id sha256", "protocol")
    _matches(protocol["id"], _ID, "protocol ID")
    _matches(protocol["sha256"], _HEX, "protocol SHA-256")

    code = manifest["code"]
    _shape(code, "revision files dependency_lock_sha256", "code identity")
    _matches(code["revision"], _CODE_REVISION, "immutable code revision")
    _matches(code["dependency_lock_sha256"], _HEX, "dependency lock SHA-256")
    _digests(code["files"], paths=True)

    development = manifest["development"]
    _shape(development, "start end inputs", "development scope")
    _digests(development["inputs"])

    parameters = manifest["parameters"]
    _shape(parameters, "strategy execution acceptance", "frozen parameters")
    if any(type(group) is not dict or not group for group in parameters.values()):
        raise HoldoutDenied("Freeze strategy, execution and acceptance parameters")

    holdout = manifest["holdout"]
    _shape(holdout, "universe start end snapshot_id artifacts", "holdout scope")
    if holdout["universe"] != UNIVERSE:
        raise HoldoutDenied("Only the frozen US equity scope is supported")
    if not _day(development["start"]) <= _day(development["end"]) < _day(holdout["start"]) <= _day(holdout["end"]):
        raise HoldoutDenied("Development and holdout windows must be ordered and disjoint")
    _matches(holdout["snapshot_id"], _ID, "snapshot ID")
    if type(holdout["artifacts"]) is not list or not holdout["artifacts"]:
        raise HoldoutDenied("Missing custodian artifact checksums")
    paths = set()
    for artifact in holdout["artifacts"]:
        _shape(artifact, "path sha256 fields", "custodian artifact")
        _relative_path(artifact["path"])
        _matches(artifact["sha256"], _HEX, "artifact SHA-256")
        if artifact["path"].casefold() in paths:
            raise HoldoutDenied("Duplicate artifact paths")
        paths.add(artifact["path"].casefold())
        fields = artifact["fields"]
        if type(fields) is not list or not fields:
            raise HoldoutDenied("Missing scoped artifact fields")
        for name in fields:
            _matches(name, _ID, "field name")
        if len(set(fields)) != len(fields):
            raise HoldoutDenied("Duplicate artifact fields")

    candidates = manifest["candidates"]
    if type(candidates) is not list or not 1 <= len(candidates) <= 10:
        raise HoldoutDenied("A holdout manifest requires a nonempty frozen shortlist of at most ten pairs")
    pairs = set()
    for entry in candidates:
        _shape(entry, "pair intercept beta", "candidate")
        pair = entry["pair"]
        if type(pair) is not list or len(pair) != 2:
            raise HoldoutDenied("A pair requires two permanent IDs")
        for identifier in pair:
            _matches(identifier, _ID, "instrument ID")
        if pair[0] >= pair[1] or tuple(pair) in pairs:
            raise HoldoutDenied("Require distinct candidates in canonical instrument orientation")
        pairs.add(tuple(pair))
        if (any(type(entry[key]) not in (int, float) for key in ("intercept", "beta"))
                or entry["beta"] <= 0):
            raise HoldoutDenied("Require finite formation estimates and a positive hedge coefficient")


def manifest_digest(manifest: dict) -> str:
    validate_manifest(manifest)
    return fingerprint(manifest)


@dataclass(frozen=True, slots=True)
class VerifiedApproval:
    """Claims returned only by the host's trusted receipt verifier."""

    receipt_id: str
    principal: str
    issued_at: str
    run_id: str
    manifest_sha256: str
    action: str


@dataclass(frozen=True, slots=True)
class ArtifactRequest:
    snapshot_id: str
    path: str
    start: str
    end: str
    instrument_ids: tuple[str, ...]
    fields: tuple[str, ...]


class _RunLease:
    def __init__(self, journal: Journal, manifest: dict) -> None:
        self._journal = journal
        self._manifest = manifest
        self._active = True
        self._owner = get_ident()
        self._verified = set()

    def _require_active(self):
        if not self._active or get_ident() != self._owner:
            raise HoldoutDenied("Holdout lease must be active in its owning thread")

    def read_artifact(self, path: str, read: Callable[[ArtifactRequest], bytes]) -> bytes:
        """Call the trusted reader with a frozen scope and verify returned bytes."""
        self._require_active()
        holdout = self._manifest["holdout"]
        artifact = next((item for item in holdout["artifacts"] if item["path"] == path), None)
        if artifact is None:
            raise HoldoutDenied("Artifact is outside the frozen scope")
        instruments = tuple(sorted({name for candidate in self._manifest["candidates"] for name in candidate["pair"]}))
        request = ArtifactRequest(holdout["snapshot_id"], path, holdout["start"], holdout["end"],
                                  instruments, tuple(artifact["fields"]))
        # The reader must yield a custodian-sealed artifact containing exactly the requested scope.
        data = read(request)
        self._require_active()
        if type(data) is not bytes or sha256(data).hexdigest() != artifact["sha256"]:
            raise HoldoutDenied("Holdout bytes differ from the custodian checksum")
        self._verified.add(path)
        return data

    def finish(self, result_sha256: str, *, result: dict | None = None) -> None:
        self._require_active()
        _matches(result_sha256, _HEX, "result SHA-256")
        if self._verified != {entry["path"] for entry in self._manifest["holdout"]["artifacts"]}:
            raise HoldoutDenied("Every frozen holdout artifact must be verified before completion")
        record = {"result_sha256": result_sha256}
        if result is not None:
            _json_data(result)
            if type(result) is not dict or fingerprint(result) != result_sha256:
                raise HoldoutDenied("Result content differs from its completion digest")
            record["result"] = result
        self._journal.append(LEDGER, f"complete/{self._manifest['run_id']}", record)
        self._active = False


class HoldoutAuthority:
    """Host-owned journal and verifiers for an approved US equity holdout.

    The host pins one persistent journal for the entire scope. The environment
    verifier checks current protocol, code, dependencies, development inputs
    and custodian metadata without reading any holdout payload.
    """

    def __init__(self, journal_path: Path, *, expected_principal: str,
                 verify_receipt: Callable[[object], VerifiedApproval | None],
                 verify_environment: Callable[[dict], bool]) -> None:
        if type(expected_principal) is not str or not expected_principal.strip():
            raise ValueError("Configure an authenticated holdout approver")
        if not callable(verify_receipt) or not callable(verify_environment):
            raise TypeError("Configure trusted holdout verifiers")
        self._principal = expected_principal
        self._verify_receipt = verify_receipt
        self._verify_environment = verify_environment
        path = Path(journal_path).resolve()
        self._journal = Journal(path)
        # The separate SQLite writer lease survives throughout payload processing.
        # OS locks release if the process dies, while the one-use claim remains.
        self._lock_path = path.with_name(path.name + ".lease.sqlite")

    def _approve(self, manifest: dict, digest: str, receipt: object) -> VerifiedApproval:
        if receipt is None:
            raise HoldoutDenied("Explicit authenticated holdout approval is required")
        claims = self._verify_receipt(receipt)
        if (type(claims) is not VerifiedApproval or claims.principal != self._principal
                or claims.run_id != manifest["run_id"] or claims.manifest_sha256 != digest or claims.action != ACTION):
            raise HoldoutDenied("Approval does not bind the approver, exact run, digest and action")
        _matches(claims.receipt_id, _ID, "approval receipt ID")
        try:
            issued = datetime.fromisoformat(claims.issued_at)
        except (TypeError, ValueError):
            raise HoldoutDenied("Approval timestamp is invalid") from None
        if issued.utcoffset() != timedelta(0):
            raise HoldoutDenied("Approval timestamp must identify UTC")
        return claims

    def _claim(self, manifest: dict, digest: str, claims: VerifiedApproval, resume: bool) -> None:
        holdout = manifest["holdout"]
        run_id = manifest["run_id"]
        claim_key = f"claim/{run_id}"
        claim = {"manifest_sha256": digest, "approval": asdict(claims),
                 "start": holdout["start"], "end": holdout["end"]}
        events = {event["key"]: event["value"] for event in self._journal.events(LEDGER)}
        if f"complete/{run_id}" in events:
            raise HoldoutDenied("The approved run is already complete")
        old = events.get(claim_key)
        if old is not None:
            if old != claim or not resume:
                raise HoldoutDenied("An interrupted run requires explicit resume of the exact approved claim")
        else:
            if resume:
                raise HoldoutDenied("There is no interrupted approved run to resume")
            for key, prior in events.items():
                if key.startswith("claim/") and (prior["approval"]["receipt_id"] == claims.receipt_id
                        or claim["start"] <= prior["end"] and prior["start"] <= claim["end"]):
                    raise HoldoutDenied("The receipt or holdout window has already been claimed")
            self._journal.append(LEDGER, claim_key, claim)
        attempt = sum(key.startswith(f"attempt/{run_id}/") for key in events) + 1
        self._journal.append(LEDGER, f"attempt/{run_id}/{attempt}", {"resume": resume, "manifest_sha256": digest})

    def completed_result(self, manifest: dict, receipt: object) -> dict | None:
        """Confirm an existing result commit for publication recovery, without granting a read lease."""
        digest = manifest_digest(manifest)
        run_id = manifest["run_id"]
        claims = self._approve(manifest, digest, receipt)
        completed = self._journal.get(LEDGER, f"complete/{run_id}")
        if completed is None:
            return None
        claim = self._journal.get(LEDGER, f"claim/{run_id}")
        if claim is None or claim["manifest_sha256"] != digest or claim["approval"] != asdict(claims):
            raise HoldoutDenied("The publication requires the original exact approved claim")
        if "result" in completed and fingerprint(completed["result"]) != completed["result_sha256"]:
            raise HoldoutDenied("Journal result content differs from its completion digest")
        return completed

    @contextmanager
    def open_run(self, manifest: dict, receipt: object, *, resume: bool = False) -> Iterator[_RunLease]:
        """Acquire exclusive access after approval and expose the active lease."""
        digest = manifest_digest(manifest)
        frozen = json.loads(json.dumps(manifest, allow_nan=False))
        if fingerprint(frozen) != digest or type(resume) is not bool:
            raise HoldoutDenied("Invalid frozen manifest or resume request")
        claims = self._approve(frozen, digest, receipt)
        lock = sqlite3.connect(self._lock_path, timeout=0, isolation_level=None)
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise HoldoutDenied("An approved holdout run is already active") from None
                raise
            if self._verify_environment(json.loads(json.dumps(frozen))) is not True:
                raise HoldoutDenied("The current protocol, code or input identity does not match the manifest")
            self._claim(frozen, digest, claims, resume)
            lease = _RunLease(self._journal, frozen)
            try:
                yield lease
            finally:
                lease._active = False
        finally:
            lock.close()
