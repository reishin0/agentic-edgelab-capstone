import unittest

from research.pair_discovery.acceptance import decide


class EconomicGates(unittest.TestCase):
    def test_boundaries_and_strictly_positive_stress(self):
        base = {"status": "complete", "missing_evidence": [], "stability_failure": False,
                "terminal_shares": [0, 0], "round_trips": 20, "entry_fill_fraction": .8,
                "quote_coverage_fraction": .95, "net_return": .02, "max_drawdown": .05}
        stress = {**base, "round_trips": 15, "net_return": .0001, "max_drawdown": .075}
        self.assertTrue(decide(base, stress)["passed_numeric_gates"])
        self.assertEqual(decide(base, {**stress, "net_return": 0})["verdict"], "stop")
        self.assertEqual(decide({**base, "status": "unresolved"}, stress)["verdict"], "unresolved")
        self.assertEqual(decide({**base, "stability_failure": True}, stress)["verdict"], "stop")
