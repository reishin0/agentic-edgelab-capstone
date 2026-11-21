import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from research.pair_discovery import holdout, prepare_holdout, screen
from research.pair_discovery.approval import HoldoutDenied, manifest_digest
from research.pair_discovery.holdout_host import DEVELOPMENT_INPUTS, runtime_lock


class ManifestPreparation(unittest.TestCase):
    def setup_host(self, folder, classification):
        work = folder / "dev"
        code = folder / "code"
        work.mkdir()
        code.mkdir()
        paths = {name: f"{name}.json" for name in DEVELOPMENT_INPUTS}
        for name in paths.values():
            (work / name).write_bytes(b"{}")
        report = {"manifest": {"classification": classification}, "holdout": "locked and untested",
                  "shortlist": [["A", "B"]], "pairs": [{"pair": ["A", "B"], "verdict": "continue researching",
                  "reason": "passed_development_gates", "formation": {"cointegration": {
                      "a_on_b": {"intercept": 0.1, "slope": 0.9}}}}]}
        (work / paths["report"]).write_text(json.dumps(report), encoding="utf-8")
        for name in holdout.REQUIRED_CODE_PATHS:
            path = code / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture code")
        (code / "runtime.json").write_text(json.dumps(runtime_lock()), encoding="utf-8")
        (code / "protocol.md").write_bytes(holdout.PROTOCOL_PATH.read_bytes())
        snapshot = {"snapshot_id": "missing-payloads", "start": screen.HOLDOUT_START, "end": holdout.HOLDOUT_END,
                    "artifacts": [{"path": name, "fields": list(fields), "sha256": "2" * 64}
                                  for name, fields in holdout.ARTIFACT_FIELDS.items()]}
        return SimpleNamespace(_development_paths=paths, _development=work, _snapshot=snapshot, _code=code,
                               _dependency_lock="runtime.json", _code_paths=tuple(sorted(holdout.REQUIRED_CODE_PATHS)),
                               _protocol_id=holdout.PROTOCOL_ID, _protocol="protocol.md", _revision="1" * 40,
                               _verify_environment=lambda manifest: True)

    def test_manifests_are_reviewable_without_holdout_files(self):
        with TemporaryDirectory() as tmp:
            host = self.setup_host(Path(tmp), "equity-development")
            manifest, digest = prepare_holdout.prepare(host, run_id=str(uuid4()), output_directory="result")
            self.assertEqual(manifest["candidates"], [{"pair": ["A", "B"], "intercept": .1, "beta": .9}])
            self.assertEqual(manifest_digest(json.loads(prepare_holdout.manifest_bytes(manifest))), digest)

    def test_synthetic_results_are_ineligible_for_real_approval_manifest(self):
        with TemporaryDirectory() as tmp:
            host = self.setup_host(Path(tmp), "synthetic-development")
            with self.assertRaisesRegex(HoldoutDenied, "real-data development shortlist"):
                prepare_holdout.prepare(host, run_id=str(uuid4()), output_directory="result")
