"""Use only deterministic fixture keys and synthetic sealed payloads."""

from base64 import b64encode
from dataclasses import asdict, replace
from hashlib import sha256
from importlib import import_module
from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from edgelab.journal import Journal
from research.pair_discovery import holdout_host as host
from research.pair_discovery.approval import ACTION, LEDGER, ArtifactRequest, HoldoutDenied, VerifiedApproval, manifest_digest
from research.pair_discovery.test_approval import ARTIFACT, PAYLOAD, manifest


@unittest.skipUnless(find_spec("cryptography"), "The signed holdout host tests require cryptography")
class LocalHostTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve(strict=True)
        self.code, self.development, self.sealed, self.logs, self.outputs = (
            root / component for component in ("code", "development", "sealed", "authority", "outputs")
        )
        for directory in (self.code, self.development, self.sealed, self.logs, self.outputs):
            directory.mkdir()
        self.plan = manifest()
        self.plan["code"]["files"] = {"runner.py": self.write(self.code, "runner.py", b"synthetic runner")}
        self.plan["code"]["dependency_lock_sha256"] = self.write(
            self.code, "lock.json", json.dumps(host.runtime_lock()).encode()
        )
        self.plan["protocol"]["sha256"] = self.write(self.code, "protocol.md", b"synthetic protocol")
        self.development_paths = {key: f"fixture/{key}.json" for key in host.DEVELOPMENT_INPUTS}
        self.plan["development"]["inputs"] = {
            key: self.write(self.development, path, key.encode()) for key, path in self.development_paths.items()
        }
        self.plan["holdout"]["artifacts"][0]["sha256"] = self.write(self.sealed, ARTIFACT, PAYLOAD)
        scope = self.plan["holdout"]
        self.metadata = json.dumps({
            "schema": 1, "snapshot_id": scope["snapshot_id"], "start": scope["start"], "end": scope["end"],
            "instrument_ids": ["test-A", "test-B"],
            "artifacts": [{**item, "size": len(PAYLOAD)} for item in scope["artifacts"]],
        }).encode()
        self.signer = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        self.approval_public_key = self.signer.public_key().public_bytes_raw()
        self.db = self.logs / "ledger.sqlite"
        self.host = self.new_host()

    @staticmethod
    def write(root, name, payload):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return sha256(payload).hexdigest()

    def new_host(self, **kwargs):
        settings = {
            "approval_public_key": self.approval_public_key, "expected_principal": "fixture-approver",
            "journal_path": self.db, "protocol_id": self.plan["protocol"]["id"], "protocol_path": "protocol.md",
            "code_root": self.code, "code_revision": self.plan["code"]["revision"], "code_paths": ["runner.py"],
            "dependency_lock": "lock.json", "development_root": self.development,
            "development_paths": self.development_paths, "snapshot_root": self.sealed,
            "snapshot_metadata": self.metadata, "snapshot_metadata_sha256": sha256(self.metadata).hexdigest(),
            "output_root": self.outputs,
        }
        return host.LocalHoldoutHost(**(settings | kwargs))

    def sign(self, claims):
        encoded = json.dumps(claims, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        signature = b64encode(self.signer.sign(host.RECEIPT_CONTEXT + encoded)).decode()
        return json.dumps({"claims": claims, "signature": signature}).encode()

    def ticket(self, **changes):
        claims = VerifiedApproval("fixture-authorization", "fixture-approver", "2026-11-14T12:00:00Z",
                                  self.plan["run_id"], manifest_digest(self.plan), ACTION)
        return self.sign(asdict(replace(claims, **changes)))

    def request(self):
        scope = self.plan["holdout"]
        return ArtifactRequest(scope["snapshot_id"], ARTIFACT, scope["start"], scope["end"],
                               ("test-A", "test-B"), ("day", "a", "b"))

    def use_runner_stub(self):
        def runner(authority, manifest, receipt, *, development_manifest_path, development_report_path,
                   read_artifact, output_root, resume):
            self.assertEqual(development_manifest_path, self.development / "fixture/manifest.json")
            self.assertEqual(development_report_path, self.development / "fixture/report.json")
            self.assertEqual(output_root, self.outputs)
            with authority.open_run(manifest, receipt, resume=resume) as lease:
                self.assertEqual(lease.read_artifact(ARTIFACT, read_artifact), PAYLOAD)
                lease.finish("7" * 64)
                return PAYLOAD

        return patch.dict("sys.modules", {"research.pair_discovery.holdout": SimpleNamespace(
            run_holdout=runner, REQUIRED_CODE_PATHS=frozenset())})

    def test_signed_approval_claim_and_pin_checks_precede_payload_read(self):
        original = host._read_file
        reads = []

        def observe(root, *args, **kwargs):
            if root == self.sealed:
                reads.append(args[0])
                claim = Journal(self.db).get(LEDGER, f"claim/{self.plan['run_id']}")
                self.assertEqual(claim["manifest_sha256"], manifest_digest(self.plan))
            return original(root, *args, **kwargs)

        with self.use_runner_stub(), patch.object(host, "_read_file", observe):
            for invalid in (None, b"{}", self.ticket(principal="different"), self.ticket(manifest_sha256="a" * 64)):
                with self.assertRaises(HoldoutDenied):
                    self.host.run(self.plan, invalid)
            self.assertEqual(reads, [])
            self.assertEqual(Journal(self.db).events(LEDGER), [])
            self.assertEqual(self.host.run(self.plan, self.ticket()), PAYLOAD)
            self.assertEqual(reads, [ARTIFACT])
            with self.assertRaises(HoldoutDenied):
                self.host.run(self.plan, self.ticket(), resume=True)
            self.assertEqual(reads, [ARTIFACT])

    def test_rejects_wrong_key_tampering_and_duplicate_claims(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        verifier = host.SignedReceiptVerifier(self.approval_public_key)
        good = self.ticket()
        self.assertEqual(verifier(good).principal, "fixture-approver")
        wrong = Ed25519PrivateKey.from_private_bytes(bytes(range(2, 34))).public_key().public_bytes_raw()
        self.assertIsNone(host.SignedReceiptVerifier(wrong)(good))
        self.assertIsNone(verifier(good.replace(b"fixture-approver", b"somebody-else")))
        self.assertIsNone(verifier(good.replace(b'"claims": {', b'"claims": {"principal":"first",')))

    def test_pins_reject_changed_development_code_and_runtime(self):
        with self.use_runner_stub(), patch.object(self.host, "_read_sealed") as payload:
            for root, name in ((self.code, "runner.py"), (self.code, "protocol.md"),
                               (self.development, self.development_paths["execution"])):
                path = root / name
                original = path.read_bytes()
                path.write_bytes(b"changed")
                with self.assertRaises(HoldoutDenied):
                    self.host.run(self.plan, self.ticket())
                path.write_bytes(original)
            lock = host.runtime_lock()
            lock["packages"]["numpy"] = "0.0"
            self.plan["code"]["dependency_lock_sha256"] = self.write(
                self.code, "lock.json", json.dumps(lock).encode()
            )
            with self.assertRaises(HoldoutDenied):
                self.host.run(self.plan, self.ticket())
            payload.assert_not_called()

    def test_copied_source_pin_must_match_the_executing_module(self):
        relative = "research/pair_discovery/holdout_host.py"
        actual = sha256(Path(host.__file__).read_bytes()).hexdigest()
        host._check_loaded_sources({relative: actual}, {relative})
        with self.assertRaises(HoldoutDenied):
            host._check_loaded_sources({relative: "a" * 64}, {relative})

    def test_rejects_custodian_scope_and_snapshot_overlap(self):
        with self.assertRaises(HoldoutDenied):
            self.new_host(snapshot_metadata_sha256="b" * 64)
        with self.assertRaises(HoldoutDenied):
            self.new_host(development_root=self.sealed)
        self.plan["holdout"]["artifacts"][0]["fields"].remove("b")
        with self.use_runner_stub(), patch.object(self.host, "_read_sealed") as payload:
            with self.assertRaises(HoldoutDenied):
                self.host.run(self.plan, self.ticket())
            payload.assert_not_called()
        for invalid in (replace(self.request(), start="2025-11-04"),
                        replace(self.request(), path="../sample.csv"),
                        replace(self.request(), instrument_ids=("test-A",)),
                        replace(self.request(), fields=("day", "a"))):
            with self.assertRaises(HoldoutDenied):
                self.host._read_sealed(invalid)

    def test_sealed_payload_check_rejects_changed_bytes(self):
        original = self.sealed / ARTIFACT
        original.write_bytes(bytes(len(PAYLOAD)))
        with self.use_runner_stub(), self.assertRaisesRegex(HoldoutDenied, "custodian checksum"):
            self.host.run(self.plan, self.ticket())
        self.assertTrue(Journal(self.db).get(LEDGER, f"claim/{self.plan['run_id']}"))
        self.assertIsNone(Journal(self.db).get(LEDGER, f"complete/{self.plan['run_id']}"))

    def test_reparse_points_and_multi_link_identities_are_rejected(self):
        for target, invoke in ((self.db, self.host._authority),
                               (self.sealed / ARTIFACT, lambda: self.host._read_sealed(self.request()))):
            if not target.exists():
                target.touch()
            original = target.stat()
            probe = Path.lstat

            def inspect(path):
                if path == target:
                    return SimpleNamespace(st_mode=original.st_mode, st_nlink=2, st_size=original.st_size)
                return probe(path)

            with patch.object(Path, "lstat", inspect), self.assertRaises(HoldoutDenied):
                invoke()

        self.assertTrue(host._is_link(SimpleNamespace(st_mode=0, st_file_attributes=0x400)))


@unittest.skipUnless(find_spec("cryptography"), "The signed holdout host tests require cryptography")
class SignedEndToEnd(unittest.TestCase):
    def test_signed_real_runner_scores_synthetic_sealed_holdout(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from research.pair_discovery import holdout
        from research.pair_discovery.test_holdout import synthetic_plan

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            development, code, sealed, logs, output = (root / name for name in
                                                        ("development", "code", "sealed", "authority", "output"))
            for directory in (development, code, sealed, logs, output):
                directory.mkdir()
            plan, source_manifest, report, payloads = synthetic_plan(development)
            report_path = Path(report)
            source_manifest = Path(source_manifest)
            self.assertTrue(source_manifest.is_relative_to(development))
            self.assertTrue(report_path.is_relative_to(development))
            paths = {name: (source_manifest.parent / f"{name}.json").relative_to(development).as_posix()
                     for name in host.DEVELOPMENT_INPUTS - {"manifest", "report"}}
            paths["manifest"] = source_manifest.relative_to(development).as_posix()
            paths["report"] = report_path.relative_to(development).as_posix()

            def pin(relative, raw):
                return LocalHostTests.write(code, relative, raw)

            pinned_files = {}
            for relative in sorted(holdout.REQUIRED_CODE_PATHS):
                module = import_module(relative[:-3].replace("/", "."))
                pinned_files[relative] = pin(relative, Path(module.__file__).read_bytes())
            protocol = "research/pair_discovery/DISCOVERY_PROTOCOL_DRAFT.md"
            plan["protocol"]["sha256"] = pin(protocol, Path(holdout.__file__).with_name(Path(protocol).name).read_bytes())
            plan["code"]["files"] = pinned_files
            plan["code"]["dependency_lock_sha256"] = pin("lock.json", json.dumps(host.runtime_lock()).encode())
            plan["development"]["inputs"] = {name: sha256((development / path).read_bytes()).hexdigest()
                                               for name, path in paths.items()}
            scope = plan["holdout"]
            artifacts = []
            for item in scope["artifacts"]:
                payload = payloads[item["path"]]
                self.assertEqual(LocalHostTests.write(sealed, item["path"], payload), item["sha256"])
                artifacts.append({**item, "size": len(payload)})
            metadata = json.dumps({
                "schema": 1, "snapshot_id": scope["snapshot_id"], "start": scope["start"], "end": scope["end"],
                "instrument_ids": sorted({name for candidate in plan["candidates"] for name in candidate["pair"]}),
                "artifacts": artifacts,
            }).encode()
            key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
            authority_path = logs / "ledger.sqlite"
            local = host.LocalHoldoutHost(
                approval_public_key=key.public_key().public_bytes_raw(), expected_principal="fixture-approver",
                journal_path=authority_path, protocol_id=plan["protocol"]["id"], protocol_path=protocol,
                code_root=code, code_revision=plan["code"]["revision"], code_paths=pinned_files,
                dependency_lock="lock.json", development_root=development, development_paths=paths,
                snapshot_root=sealed, snapshot_metadata=metadata, snapshot_metadata_sha256=sha256(metadata).hexdigest(),
                output_root=output,
            )
            claims = asdict(VerifiedApproval("signed-fixture-only", "fixture-approver", "2026-11-14T12:00:00Z",
                                             plan["run_id"], manifest_digest(plan), ACTION))
            signature = key.sign(host.RECEIPT_CONTEXT + host._canonical_json(claims))
            receipt = json.dumps({"claims": claims, "signature": b64encode(signature).decode()}).encode()
            with patch.object(local, "_read_sealed", wraps=local._read_sealed) as read:
                with self.assertRaises(HoldoutDenied):
                    local.run(plan, None)
                read.assert_not_called()
                published = local.run(plan, receipt)
                self.assertEqual(read.call_count, len(artifacts))
                result = json.loads(published.read_text(encoding="utf-8"))
                self.assertEqual(result["scope"], "synthetic-holdout")
                self.assertEqual(result["tradable_pairs"], [])
                self.assertEqual(result["manifest_sha256"], manifest_digest(plan))
                self.assertEqual(local.run(plan, receipt, resume=True), published)
                self.assertEqual(read.call_count, len(artifacts))
            complete = Journal(authority_path).get(LEDGER, f"complete/{plan['run_id']}")
            self.assertIsNotNone(complete)
