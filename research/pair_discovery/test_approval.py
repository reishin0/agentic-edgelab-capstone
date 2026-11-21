"""Synthetic coverage of the manifest-bound holdout authorization boundary."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from edgelab.journal import Journal
from research.pair_discovery.approval import (
    ACTION, LEDGER, ArtifactRequest, HoldoutAuthority, HoldoutDenied, VerifiedApproval, manifest_digest,
)


PAYLOAD = b"sealed synthetic artifact"
ARTIFACT = "snapshots/sample.csv"
CHECKSUM = "7" * 64


def manifest():
    return {
        "schema": 1,
        "run_id": str(uuid4()),
        "action": ACTION,
        "protocol": {"id": "synthetic-v1", "sha256": CHECKSUM},
        "code": {"revision": "0" * 40, "files": {"research/calibrate.py": CHECKSUM},
                 "dependency_lock_sha256": CHECKSUM},
        "development": {"start": "2023-10-01", "end": "2025-10-31", "inputs": {"research": CHECKSUM}},
        "parameters": {"strategy": {"enter": 2}, "execution": {"slippage_bps": 5},
                       "acceptance": {"min_round_trips": 20}},
        "holdout": {"universe": "us-equity", "start": "2025-11-03", "end": "2026-10-30", "snapshot_id": "synthetic",
                    "artifacts": [{"path": ARTIFACT, "sha256": sha256(PAYLOAD).hexdigest(), "fields": ["day", "a", "b"]}]},
        "candidates": [{"pair": ["test-A", "test-B"], "intercept": 0.5, "beta": 1.3}],
        "output_directory": "runs/synthetic",
    }


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "authority.sqlite"
        self.receipts = {}
        self.reads = []
        self.environment_ready = True
        self.authority = self.new_authority()

    def new_authority(self):
        return HoldoutAuthority(self.db, expected_principal="synthetic-researcher", verify_receipt=self.receipts.get,
                                verify_environment=lambda _: self.environment_ready)

    def approve_fixture(self, plan):
        ticket = object()
        self.receipts[ticket] = VerifiedApproval(f"fixture-{uuid4()}", "synthetic-researcher", "2026-11-14T12:00:00Z",
                                                 plan["run_id"], manifest_digest(plan), ACTION)
        return ticket

    def read(self, request):
        self.reads.append(request)
        return PAYLOAD

    def test_exact_claim_is_durable_before_first_read(self):
        plan = manifest()
        for receipt in (None, object(), self.approve_fixture(manifest())):
            with self.assertRaises(HoldoutDenied), self.authority.open_run(plan, receipt) as lease:
                lease.read_artifact(ARTIFACT, self.read)
        self.assertEqual(self.reads, [])
        self.assertEqual(Journal(self.db).events(LEDGER), [])

        ticket = self.approve_fixture(plan)
        self.receipts[ticket] = replace(self.receipts[ticket], principal="wrong-approver")
        with self.assertRaises(HoldoutDenied), self.authority.open_run(plan, ticket):
            self.fail("Wrong approver gained access")
        ticket = self.approve_fixture(plan)
        self.environment_ready = False
        with self.assertRaises(HoldoutDenied), self.authority.open_run(plan, ticket):
            self.fail("Stale environment gained access")
        self.assertEqual(Journal(self.db).events(LEDGER), [])
        self.environment_ready = True

        with self.authority.open_run(plan, ticket) as lease:
            with self.assertRaises(HoldoutDenied):
                lease.finish(CHECKSUM)

            def verify_claim_then_read(request):
                recorded = Journal(self.db).get(LEDGER, f"claim/{plan['run_id']}")
                self.assertEqual(recorded["manifest_sha256"], manifest_digest(plan))
                return self.read(request)

            self.assertEqual(lease.read_artifact(ARTIFACT, verify_claim_then_read), PAYLOAD)
            lease.finish(CHECKSUM)
        self.assertEqual(self.reads, [ArtifactRequest("synthetic", ARTIFACT, "2025-11-03", "2026-10-30",
                                                     ("test-A", "test-B"), ("day", "a", "b"))])
        with self.assertRaises(HoldoutDenied):
            lease.read_artifact(ARTIFACT, self.read)
        with self.assertRaises(HoldoutDenied), self.authority.open_run(plan, ticket, resume=True):
            self.fail("A completed run was replayed")

    def test_interruption_locks_window_and_exact_resume(self):
        plan = manifest()
        ticket = self.approve_fixture(plan)
        with self.authority.open_run(plan, ticket) as lease:
            with self.assertRaises(HoldoutDenied), self.new_authority().open_run(plan, ticket, resume=True):
                self.fail("Concurrent resumption obtained access")
            with self.assertRaises(HoldoutDenied):
                lease.read_artifact("not-approved.csv", self.read)
            with self.assertRaises(HoldoutDenied):
                lease.read_artifact(ARTIFACT, lambda _: b"changed")
        self.assertEqual(self.reads, [])
        restarted = self.new_authority()
        replacement = manifest()
        replacement["holdout"]["snapshot_id"] = "later-copy"
        for attempt, approval, resume in (
            (replacement, self.approve_fixture(replacement), False),
            (plan, ticket, False),
            (plan, self.approve_fixture(plan), True),
        ):
            with self.assertRaises(HoldoutDenied), restarted.open_run(attempt, approval, resume=resume):
                self.fail("An interrupted approved window was opened out of scope")
        with restarted.open_run(plan, ticket, resume=True) as lease:
            lease.read_artifact(ARTIFACT, self.read)
            lease.finish(CHECKSUM)
        events = Journal(self.db).events(LEDGER)
        self.assertEqual(len(events), 4)
        self.assertTrue(events[-2]["value"]["resume"])

    def test_manifests_bind_thresholds_filters_and_windows(self):
        for mutate in (
            lambda plan: plan["holdout"]["artifacts"][0].update(path="../outside"),
            lambda plan: plan["holdout"]["artifacts"][0].update(path="snapshots/aux.csv"),
            lambda plan: plan["holdout"].update(start="2025-10-31"),
            lambda plan: plan.update(candidates=[]),
            lambda plan: plan["candidates"][0].update(beta=float("nan")),
        ):
            plan = manifest()
            mutate(plan)
            with self.assertRaises(HoldoutDenied):
                manifest_digest(plan)
        plan = manifest()
        ticket = self.approve_fixture(plan)
        plan["parameters"]["execution"]["slippage_bps"] = 0
        with self.assertRaises(HoldoutDenied), self.authority.open_run(plan, ticket) as lease:
            lease.read_artifact(ARTIFACT, self.read)
        self.assertEqual(self.reads, [])

    def test_process_death_releases_writer_lock_but_retains_claim(self):
        plan = manifest()
        ticket = self.approve_fixture(plan)
        claims = self.receipts[ticket]
        script = """
import json
import os
from pathlib import Path
import sys
from research.pair_discovery.approval import HoldoutAuthority, VerifiedApproval

packet = json.load(sys.stdin)
receipt = VerifiedApproval(*packet['claims'])
authority = HoldoutAuthority(Path(packet['journal']), expected_principal=receipt.principal,
                             verify_receipt=lambda token: receipt if token == 'fixture' else None,
                             verify_environment=lambda plan: True)
with authority.open_run(packet['manifest'], 'fixture'):
    os._exit(91)
"""
        run = subprocess.run([sys.executable, "-B", "-c", script], timeout=10, capture_output=True, text=True,
                             input=json.dumps({"manifest": plan, "journal": str(self.db),
                                               "claims": [claims.receipt_id, claims.principal, claims.issued_at,
                                                          claims.run_id, claims.manifest_sha256, claims.action]}))
        self.assertEqual(run.returncode, 91, run.stderr)
        self.assertIsNotNone(Journal(self.db).get(LEDGER, f"claim/{plan['run_id']}"))
        with self.new_authority().open_run(plan, ticket, resume=True) as lease:
            self.assertEqual(lease.read_artifact(ARTIFACT, self.read), PAYLOAD)
            lease.finish(CHECKSUM)


if __name__ == "__main__":
    unittest.main()
