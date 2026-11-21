"""Freeze an approval manifest from trusted paths and custodian metadata only."""

from hashlib import sha256
import json

from .approval import ACTION, UNIVERSE, HoldoutDenied, manifest_digest
from .holdout_host import DEVELOPMENT_INPUTS, _check_lock, _hash_file, _read_file, _strict_json
from .screen import CALIBRATION_END, HOLDOUT_START


def prepare(host, *, run_id, output_directory):
    """Return the exact unsigned manifest and digest for an independent approver."""
    from . import holdout

    development_paths = host._development_paths
    report = _strict_json(_read_file(host._development, development_paths["report"]),
                          "development report", max_bytes=512 * 1024**2)
    if not isinstance(report, dict) or not isinstance(report.get("manifest"), dict):
        raise HoldoutDenied("Invalid frozen development report")
    selection = report.get("shortlist")
    if (not isinstance(selection, list) or not selection or report.get("holdout") != "locked and untested"
            or report.get("manifest", {}).get("classification") != "equity-development"):
        raise HoldoutDenied("A frozen real-data development shortlist is required for approval preparation")
    candidates = holdout.development_candidates(report)

    snapshot = host._snapshot
    approved_fields = {name: list(fields) for name, fields in holdout.ARTIFACT_FIELDS.items()}
    if ((snapshot["start"], snapshot["end"]) != (HOLDOUT_START, holdout.HOLDOUT_END)
            or {row["path"]: row["fields"] for row in snapshot["artifacts"]} != approved_fields
            or not holdout.REQUIRED_CODE_PATHS <= set(host._code_paths) or host._protocol_id != holdout.PROTOCOL_ID):
        raise HoldoutDenied("Host settings do not match the frozen runner scope")
    lock = _read_file(host._code, host._dependency_lock)
    _check_lock(lock)
    manifest = {
        "schema": 1, "run_id": run_id, "action": ACTION, "output_directory": output_directory,
        "protocol": {"id": host._protocol_id, "sha256": _hash_file(host._code, host._protocol)},
        "code": {"revision": host._revision,
                 "files": {name: _hash_file(host._code, name) for name in host._code_paths},
                 "dependency_lock_sha256": sha256(lock).hexdigest()},
        "development": {"start": holdout.development_start(), "end": CALIBRATION_END,
                        "inputs": {name: _hash_file(host._development, development_paths[name])
                                   for name in sorted(DEVELOPMENT_INPUTS)}},
        "holdout": {"universe": UNIVERSE, "start": snapshot["start"], "end": snapshot["end"],
                    "snapshot_id": snapshot["snapshot_id"],
                    "artifacts": [{key: item[key] for key in ("path", "fields", "sha256")}
                                  for item in snapshot["artifacts"]]},
        "candidates": candidates, "parameters": holdout.frozen_parameters(),
    }
    digest = holdout.validate_run_manifest(manifest)
    if not host._verify_environment(manifest):
        raise HoldoutDenied("Custodian receipt or current environment does not match the approval manifest")
    return manifest, digest


def manifest_bytes(manifest):
    """Render the recorded manifest with readable formatting for operator review."""
    manifest_digest(manifest)
    return (json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
