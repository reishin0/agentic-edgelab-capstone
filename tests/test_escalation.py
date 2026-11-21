import unittest

from edgelab.contracts import fingerprint
from edgelab.escalation import escalation_for


class EscalationTests(unittest.TestCase):
    def test_handoff_preserves_evidence_and_never_approves(self):
        packet = {"run_id": "test-run", "bundle": {"ledger_sha256": "saved-digest"},
                  "report": {"status": "reconciliation required", "verdict": None,
                             "reason": "No result was saved", "evidence_refs": [],
                             "holdout": "locked and untested"}}
        ticket = escalation_for(packet)
        digest = ticket.pop("ticket_sha256")
        self.assertEqual(digest, fingerprint(ticket))
        self.assertEqual(ticket["ledger_sha256"], "saved-digest")
        self.assertEqual(ticket["holdout"], "locked and untested")
        self.assertIn("without replaying", ticket["action"])
        self.assertNotIn("approval", ticket)

    def test_normal_stop_does_not_request_review(self):
        self.assertIsNone(escalation_for({"bundle": {}, "report": {"status": "finished", "verdict": "stop"}}))
