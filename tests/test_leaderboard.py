import unittest

from edgelab.leaderboard import rank_snapshot


def row(a, b, p=None, holm=None, status="stop"):
    return {"symbols": [a, b], "p": p, "holm_p": holm, "status": status,
            "stability": None, "reason": "Fixture"}


def group(kind, rows):
    return {"kind": kind, "label": "Fixture", "possible": 10, "complete_history": 8, "rows": rows}


class LeaderboardTests(unittest.TestCase):
    def test_full_screen_coverage_is_independent_of_display_limit(self):
        full = group("stock-stock-adjusted", [row("A", "B", .01, .09)])
        full["screened"] = 7
        output = rank_snapshot({"schema": 1, "groups": [full]})["groups"][0]
        self.assertEqual(output["unlisted"], 3)
        full["screened"] = 9
        with self.assertRaises(ValueError):
            rank_snapshot({"schema": 1, "groups": [full]})

    def test_adjusted_equities_rank_separately_from_raw_pilot(self):
        raw = group("stock-stock", [row("A", "B", .05, .30)])
        adjusted = group("stock-stock-adjusted", [row("C", "D", .02, .10)])
        groups = rank_snapshot({"schema": 1, "groups": [raw, adjusted]})["groups"]
        self.assertEqual([g["rows"][0]["rank"] for g in groups], [1, 1])

    def test_rank_is_within_family_and_blocked_pairs_have_no_rank(self):
        equities = group("stock-stock", [row("C", "D", .02, .12), row("A", "B", .03, .06),
                                        row("X", "Y", .01, .12), row("E", "F", status="data_blocked")])
        mixed = group("fx-crypto", [row("EUR/USD", "BTC/USD", .005, .03),
                                   row("GBP/USD", "ETH/USD", status="data_blocked")])
        output = rank_snapshot({"schema": 1, "groups": [equities, mixed]})["groups"]
        self.assertEqual([(r["symbols"][0], r["rank"]) for r in output[0]["rows"]],
                         [("A", 1), ("X", 2), ("C", 3), ("E", None)])
        self.assertEqual([r["rank"] for r in output[1]["rows"]], [1, None])
        self.assertEqual(output[0]["unlisted"], 6)
        self.assertEqual(output[1]["data_blocked"], 1)

    def test_fx_fx_and_duplicates_are_rejected(self):
        for kind, rows in (
            ("fx-crypto", [row("EUR/USD", "GBP/USD")]),
            ("fx-crypto", [row("EUR/USD", "BRL/USD")]),
            ("stock-stock", [row("A", "B"), row("B", "A")]),
            ("stock-stock", [row("A", "B", .02, .03, "data_blocked")]),
            ("stock-stock", [row("A", "B", .03, .02)]),
        ):
            with self.subTest(kind=kind, rows=rows), self.assertRaises(ValueError):
                rank_snapshot({"schema": 1, "groups": [group(kind, rows)]})
