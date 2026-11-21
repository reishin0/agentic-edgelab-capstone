"""Trusted local composition for an explicitly approved, custodian-sealed holdout."""

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import fields
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path, PurePosixPath
import platform
import sqlite3
import stat
import sys

from .approval import ArtifactRequest, HoldoutAuthority, HoldoutDenied, VerifiedApproval
from .approval import _HEX, _ID, _day, _matches, _relative_path, _shape


RECEIPT_CONTEXT = b"Agentic EdgeLab holdout approval v1\n"
REQUIRED_DEPENDENCIES = frozenset({"cryptography", "numpy", "scipy", "statsmodels", "pandas", "patsy", "packaging"})
OPTIONAL_RUNTIME_DEPENDENCIES = frozenset({"cffi", "pycparser", "tzdata", "python-dateutil", "six"})
DEVELOPMENT_INPUTS = frozenset({"manifest", "report", "calendar", "universe", "bars", "execution"})
MAX_METADATA_BYTES = 1_000_000
MAX_ARTIFACT_BYTES = 512 * 1024**2


def _strict_json(raw, label, max_bytes=MAX_METADATA_BYTES):
    if type(raw) is not bytes or len(raw) > max_bytes:
        raise HoldoutDenied(f"Invalid {label} size or type")

    def unique_keys(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HoldoutDenied(f"Duplicate key in {label}")
            result[key] = value
        return result

    def invalid_constant(_):
        raise HoldoutDenied(f"Nonfinite constant in {label}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise HoldoutDenied(f"Invalid {label} JSON") from error


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class SignedReceiptVerifier:
    """Verify a receipt issued outside EdgeLab by the pinned approver key."""

    def __init__(self, public_key: bytes):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if type(public_key) is not bytes or len(public_key) != 32:
            raise ValueError("An independently supplied 32-byte Ed25519 public key is required")
        self._key = Ed25519PublicKey.from_public_bytes(public_key)

    def __call__(self, receipt):
        from cryptography.exceptions import InvalidSignature

        try:
            envelope = _strict_json(receipt, "approval receipt", max_bytes=4096)
            _shape(envelope, "claims signature", "approval receipt")
            claims = envelope["claims"]
            _shape(claims, " ".join(field.name for field in fields(VerifiedApproval)), "signature claims")
            if any(type(value) is not str or not value.isascii() or not value for value in claims.values()):
                return None
            if type(envelope["signature"]) is not str:
                return None
            signature = b64decode(envelope["signature"], validate=True)
            self._key.verify(signature, RECEIPT_CONTEXT + _canonical_json(claims))
            return VerifiedApproval(**claims)
        except (HoldoutDenied, Base64Error, InvalidSignature, ValueError):
            return None


def _is_link(info):
    # Windows junctions and other reparse points also fail even if they look like directories.
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _checked_directory(path):
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise HoldoutDenied("The trusted directory path cannot contain a link or non-directory")
    return path


def _read_file(root, relative, *, expected_size=None, max_bytes=MAX_ARTIFACT_BYTES):
    _relative_path(relative)
    relative = PurePosixPath(relative)
    parent = _checked_directory(root.joinpath(*relative.parts[:-1]))
    path = parent / relative.name
    before = path.lstat()
    if (_is_link(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size > max_bytes or (expected_size is not None and before.st_size != expected_size)):
        raise HoldoutDenied("File is outside its pinned type, size or link constraints")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not os.path.samestat(before, opened) or before.st_size != opened.st_size
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
            raise HoldoutDenied("File identity changed before opening")
        raw = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise HoldoutDenied("File changed while reading")
    return raw


def _hash_file(root, relative):
    return sha256(_read_file(root, relative)).hexdigest()


def _check_loaded_sources(pins, invoked):
    if not invoked <= set(pins):
        raise HoldoutDenied("Pin every required holdout source")
    for relative, expected in pins.items():
        source = PurePosixPath(relative)
        if source.suffix != ".py" or source.parts[0] not in {"edgelab", "research"}:
            continue
        parts = source.with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        module = sys.modules.get(name)
        if module is None:
            if relative in invoked:
                raise HoldoutDenied("A required holdout module is missing from the active process")
            continue
        actual = Path(module.__file__)
        if _hash_file(actual.parent, actual.name) != expected:
            raise HoldoutDenied("A loaded module differs from its approved source checksum")


def runtime_lock():
    """Describe the reproducibility-critical runtime identities without network I/O."""
    packages = {name: version(name) for name in sorted(REQUIRED_DEPENDENCIES)}
    for name in sorted(OPTIONAL_RUNTIME_DEPENDENCIES):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            continue
    return {
        "schema": 1,
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "system": platform.system(), "machine": platform.machine(), "sqlite": sqlite3.sqlite_version},
        "packages": packages,
    }


def _check_lock(raw):
    lock = _strict_json(raw, "dependency lock")
    _shape(lock, "schema python packages", "dependency lock")
    expected = runtime_lock()
    if (type(lock["schema"]) is not int or lock["schema"] != expected["schema"]
            or lock["python"] != expected["python"] or type(lock["packages"]) is not dict
            or not set(expected["packages"]) <= set(lock["packages"]) or any(
                type(name) is not str or type(pin) is not str or version(name) != pin
                for name, pin in lock["packages"].items())):
        raise HoldoutDenied("The running environment differs from the dependency lock")


def _snapshot_pins(raw, digest):
    _matches(digest, _HEX, "custodian metadata SHA-256")
    receipt = _strict_json(raw, "custodian receipt")
    if sha256(raw).hexdigest() != digest:
        raise HoldoutDenied("Custodian receipt differs from the trusted metadata checksum")
    _shape(receipt, "schema snapshot_id start end instrument_ids artifacts", "custodian receipt")
    _matches(receipt["snapshot_id"], _ID, "custodian snapshot ID")
    if type(receipt["schema"]) is not int or receipt["schema"] != 1 or _day(receipt["start"]) > _day(receipt["end"]):
        raise HoldoutDenied("Invalid custodian scope")
    instruments = receipt["instrument_ids"]
    if type(instruments) is not list or not instruments:
        raise HoldoutDenied("Missing custodian instrument scope")
    for instrument in instruments:
        _matches(instrument, _ID, "custodian instrument ID")
    if instruments != sorted(set(instruments)) or len(instruments) > 20:
        raise HoldoutDenied("Custodian instrument IDs must be unique and in canonical order")
    if type(receipt["artifacts"]) is not list or not receipt["artifacts"]:
        raise HoldoutDenied("Missing custodian artifact scope")

    paths = set()
    for artifact in receipt["artifacts"]:
        _shape(artifact, "path sha256 fields size", "custodian artifact")
        _relative_path(artifact["path"])
        _matches(artifact["sha256"], _HEX, "custodian artifact SHA-256")
        if (type(artifact["fields"]) is not list or not artifact["fields"]
                or type(artifact["size"]) is not int or not 0 < artifact["size"] <= MAX_ARTIFACT_BYTES):
            raise HoldoutDenied("Invalid custodian artifact fields or size")
        for field in artifact["fields"]:
            _matches(field, _ID, "custodian field name")
        path = artifact["path"].casefold()
        if len(set(artifact["fields"])) != len(artifact["fields"]) or path in paths:
            raise HoldoutDenied("Duplicate custodian field or path")
        paths.add(path)
    return receipt


class LocalHoldoutHost:
    """Compose the runner with protected host configuration and a fixed authority database.

    The approver key and custodian checksum come from independent trust channels.
    Protect the configured paths from untrusted writes, including directory replacement
    and SQLite removal. The checkout revision is supplied by trusted deployment
    metadata. The current file digests and running dependency versions are verified.
    """

    def __init__(self, *, approval_public_key, expected_principal, journal_path, protocol_id,
                 protocol_path, code_root, code_revision, code_paths, dependency_lock,
                 development_root, development_paths, snapshot_root, snapshot_metadata,
                 snapshot_metadata_sha256, output_root):
        self._verify_receipt = SignedReceiptVerifier(approval_public_key)
        self._snapshot = _snapshot_pins(snapshot_metadata, snapshot_metadata_sha256)
        self._sealed = _checked_directory(snapshot_root)
        self._code = _checked_directory(code_root)
        self._development = _checked_directory(development_root)
        self._outputs = _checked_directory(output_root)
        self._journal = Path(os.path.abspath(journal_path))
        _checked_directory(self._journal.parent)
        for path in (self._code, self._development, self._outputs, self._journal.parent):
            if path.is_relative_to(self._sealed) or self._sealed.is_relative_to(path):
                raise HoldoutDenied("Sealed storage must be disjoint from code, development, ledger and outputs")
        self._principal = expected_principal
        self._protocol_id = protocol_id
        self._protocol = protocol_path
        self._revision = code_revision
        self._code_paths = tuple(code_paths)
        self._dependency_lock = dependency_lock
        self._development_paths = dict(development_paths)
        if set(self._development_paths) != DEVELOPMENT_INPUTS:
            raise ValueError("Pin the development manifest, report and all four payloads")
        manifest_parent = PurePosixPath(self._development_paths["manifest"]).parent
        for name in ("calendar", "universe", "bars", "execution"):
            if self._development_paths[name] != str(manifest_parent / f"{name}.json"):
                raise ValueError("Development payload locations must match the screen contract")
        for relative in (self._protocol, self._dependency_lock, *self._code_paths, *self._development_paths.values()):
            _relative_path(relative)
        if len({name.casefold() for name in self._code_paths}) != len(self._code_paths) or not self._code_paths:
            raise ValueError("Pin a nonempty set of unambiguous source paths")
        if len({name.casefold() for name in self._development_paths.values()}) != len(DEVELOPMENT_INPUTS):
            raise ValueError("Pin distinct development files")

    def _verify_environment(self, manifest):
        try:
            holdout = manifest["holdout"]
            if any(holdout[key] != self._snapshot[key] for key in ("snapshot_id", "start", "end")):
                return False
            instruments = sorted({name for candidate in manifest["candidates"] for name in candidate["pair"]})
            if instruments != self._snapshot["instrument_ids"]:
                return False
            pinned = {item["path"]: (item["sha256"], item["fields"]) for item in self._snapshot["artifacts"]}
            if {item["path"]: (item["sha256"], item["fields"]) for item in holdout["artifacts"]} != pinned:
                return False
            if manifest["protocol"] != {"id": self._protocol_id, "sha256": _hash_file(self._code, self._protocol)}:
                return False
            code = manifest["code"]
            if code["revision"] != self._revision or set(code["files"]) != set(self._code_paths):
                return False
            lock = _read_file(self._code, self._dependency_lock, max_bytes=MAX_METADATA_BYTES)
            if sha256(lock).hexdigest() != code["dependency_lock_sha256"]:
                return False
            _check_lock(lock)
            return (all(_hash_file(self._code, path) == digest for path, digest in code["files"].items())
                    and set(manifest["development"]["inputs"]) == DEVELOPMENT_INPUTS
                    and all(_hash_file(self._development, path) == manifest["development"]["inputs"][name]
                            for name, path in self._development_paths.items()))
        except (OSError, ValueError, PackageNotFoundError):
            return False

    def _read_sealed(self, request: ArtifactRequest) -> bytes:
        snapshot = self._snapshot
        if (type(request) is not ArtifactRequest or request.snapshot_id != snapshot["snapshot_id"]
                or (request.start, request.end) != (snapshot["start"], snapshot["end"])
                or request.instrument_ids != tuple(snapshot["instrument_ids"])):
            raise HoldoutDenied("Artifact request exceeds the trusted custodian scope")
        artifact = next((item for item in snapshot["artifacts"] if item["path"] == request.path), None)
        if artifact is None or request.fields != tuple(artifact["fields"]):
            raise HoldoutDenied("Artifact or fields are outside the trusted custodian scope")
        # HoldoutAuthority verifies the returned bytes against the matching manifest digest.
        try:
            return _read_file(self._sealed, artifact["path"], expected_size=artifact["size"])
        except OSError as error:
            raise HoldoutDenied("The pinned custodian artifact could not be read") from error

    def _authority(self, verify_environment=None):
        _checked_directory(self._journal.parent)
        for database in (self._journal, self._journal.with_name(self._journal.name + ".lease.sqlite")):
            for suffix in ("", "-journal", "-wal", "-shm"):
                path = database.with_name(database.name + suffix)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise HoldoutDenied("Authority state must be in ordinary, protected files")
        return HoldoutAuthority(self._journal, expected_principal=self._principal, verify_receipt=self._verify_receipt,
                                verify_environment=verify_environment or self._verify_environment)

    def run(self, manifest, receipt, *, resume=False):
        from .holdout import REQUIRED_CODE_PATHS, run_holdout

        def verify_environment(candidate):
            if not self._verify_environment(candidate):
                return False
            try:
                _check_loaded_sources(candidate["code"]["files"], REQUIRED_CODE_PATHS)
            except (OSError, ValueError, AttributeError, TypeError):
                return False
            return True

        return run_holdout(
            self._authority(verify_environment), manifest, receipt,
            development_manifest_path=self._development / self._development_paths["manifest"],
            development_report_path=self._development / self._development_paths["report"],
            read_artifact=self._read_sealed, output_root=self._outputs, resume=resume,
        )
