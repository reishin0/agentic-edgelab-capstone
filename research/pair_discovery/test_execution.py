"""Synthetic execution and holdout-fence regression checks."""

from dataclasses import replace
from datetime import date
import unittest

from research.pair_discovery.execution import (
    Borrow,
    CAPITAL,
    LEG_DELAY_MS,
    LegDay,
    OPEN_MS,
    Quote,
    Session,
    WINDOW_END_MS,
    decode_leg,
    replay_calibration,
)


CLOSE = 57_600_000
DAYS = tuple(date(2025, 1, day) for day in (2, 3, 6, 7, 8, 9))


def quote(mid=100, at=OPEN_MS, size=10_000):
    return Quote(at, mid - .01, mid + .01, size, size)


def leg(mid=100, *, times=(OPEN_MS, OPEN_MS + LEG_DELAY_MS), borrow=None):
    return LegDay(
        tuple(quote(mid, at) for at in times), quote(mid, CLOSE), 1_000_000,
        (Borrow(OPEN_MS - 1000, 1_000, 365),) if borrow is None else borrow,
        365, .01, 1,
    )


def sample():
    return tuple(Session(day, z, True, (leg(a), leg(b))) for day, z, a, b in zip(
        DAYS, (0, 2.1, .4, .3, .1, 0), (100, 100, 100, 99, 99, 99),
        (100, 100, 100, 101, 101, 101), strict=True,
    ))


def replay(sessions=None, *, stress=False):
    return replay_calibration(sample() if sessions is None else sessions, beta=1, previous_z=0, stress=stress)


class ExecutionTests(unittest.TestCase):
    def test_realistic_round_trip_uses_next_session_and_reconciles_costs(self):
        output = replay()
        self.assertEqual(output["status"], "complete")
        self.assertEqual((output["entered"], output["entry_attempts"], output["round_trips"]), (1, 1, 1))
        self.assertEqual(output["terminal_shares"], [0, 0])
        self.assertEqual(output["holding_sessions"], [2])
        fills = [row for row in output["ledger"] if row["event"] == "fill"]
        self.assertEqual([row["date"] for row in fills], ["2025-01-06"] * 2 + ["2025-01-07"] * 2)
        self.assertEqual([row["signed_shares"] for row in fills], [-125, 124, 125, -124])
        self.assertAlmostEqual(output["gross_return"] * CAPITAL, 249)
        self.assertAlmostEqual((output["gross_return"] - output["net_return"]) * CAPITAL,
                               sum(output["costs"].values()))
        self.assertGreater(output["max_drawdown"], 0)

    def test_slow_second_quote_skips_attempt_without_exposure(self):
        days = list(sample())
        days[2] = replace(days[2], legs=(leg(times=(OPEN_MS,)), leg(times=(OPEN_MS + 1001,))))
        result = replay(days)
        self.assertEqual(result["entered"], 0)
        self.assertEqual(result["entry_attempts"], 1)
        self.assertEqual(result["reasons"], {"missing_synchronized_quote": 1})
        self.assertEqual(result["net_return"], 0)

    def test_opening_short_requires_asof_locate_and_borrow_rate(self):
        days = list(sample())
        a, b = days[2].legs
        days[2] = replace(days[2], legs=(replace(a, borrow=(Borrow(OPEN_MS + 1, 1000, 365),)), b))
        result = replay(days)
        self.assertEqual(result["entry_fill_fraction"], 0)
        self.assertEqual(result["reasons"], {"missing_locate_or_borrow_rate": 1})

    def test_missing_mark_and_unclosed_position_fail_calibration(self):
        days = list(sample())
        a, b = days[2].legs
        days[2] = replace(days[2], legs=(replace(a, mark=None), b))
        for index in range(3, len(days)):
            a, b = days[index].legs
            days[index] = replace(days[index], legs=(replace(a, halted=True), b))
        result = replay(days)
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNone(result["max_drawdown"])
        self.assertIsNone(result["net_return"])
        self.assertIn("open_terminal_inventory", result["missing_evidence"])
        self.assertIn("missing_liquidation_mark", result["missing_evidence"])

    def test_signed_dividends_and_split_preserve_gross_inventory(self):
        days = list(sample())
        cash_and_split = replace(leg(49.5), split_ratio=2, dividend=.25)
        days[3] = replace(days[3], legs=(cash_and_split, replace(leg(101), dividend=.5)))
        result = replay(days)
        dividends = [row["cash_flow"] for row in result["ledger"] if row["event"] == "dividend"]
        self.assertEqual(dividends, [-62.5, 62.0])
        self.assertEqual(result["round_trips"], 1)
        self.assertAlmostEqual(result["gross_return"] * CAPITAL, 248.5)

    def test_stress_delay_uses_later_quote_and_doubles_borrow_rate(self):
        result = replay(stress=True)
        self.assertEqual(result["status"], "complete")
        fills = [row for row in result["ledger"] if row["event"] == "fill"]
        self.assertEqual([row["at_ms"] for row in fills], [OPEN_MS, OPEN_MS + LEG_DELAY_MS] * 2)
        self.assertEqual([item["duration_ms"] for item in result["legging_intervals"]], [LEG_DELAY_MS] * 2)
        self.assertGreater(result["costs"]["borrow"], replay()["costs"]["borrow"])
        self.assertLess(result["net_return"], replay()["net_return"])

    def test_stress_failed_second_leg_flattens_at_first_subsequent_quote(self):
        days = list(sample())
        days[2] = replace(days[2], legs=(leg(times=(OPEN_MS, WINDOW_END_MS + 90_000)), leg(times=(OPEN_MS,))))
        result = replay(days, stress=True)
        self.assertEqual((result["entry_attempts"], result["entered"], result["round_trips"]), (1, 0, 0))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["terminal_shares"], [0, 0])
        interval, = result["unhedged_intervals"]
        self.assertEqual(interval["end_ms"], WINDOW_END_MS + 90_000)
        self.assertLess(interval["failed_entry_net_pnl"], 0)
        self.assertAlmostEqual(interval["failed_entry_net_pnl"], result["net_return"] * CAPITAL)

    def test_observed_recall_forces_earliest_prospective_liquidation(self):
        days = list(sample())
        recall_at = OPEN_MS + 15_000
        at_fill = recall_at + 500
        events = (Borrow(OPEN_MS - 1000, 1000, 365), Borrow(recall_at, 0, 365, recalled=True))
        days[2] = replace(days[2], legs=(leg(times=(OPEN_MS, at_fill), borrow=events), leg(times=(OPEN_MS, at_fill))))
        result = replay(days)
        self.assertEqual(result["round_trips"], 1)
        fills = [row for row in result["ledger"] if row["event"] == "fill"]
        self.assertEqual([row["at_ms"] for row in fills], [OPEN_MS] * 2 + [at_fill] * 2)
        self.assertIn("borrow_recall", [row["event"] for row in result["ledger"]])

    def test_capacity_checks_and_short_restriction_cannot_open_position(self):
        days = list(sample())
        a, b = days[2].legs
        days[2] = replace(days[2], legs=(a, replace(b, median_volume=100)))
        result = replay(days)
        self.assertEqual(result["reasons"], {"insufficient_capacity": 1})
        self.assertEqual(result["entered"], 0)
        days[2] = replace(days[2], legs=(replace(a, borrow=(Borrow(0, 1000, 365, restricted=True),)), b))
        result = replay(days)
        self.assertEqual(result["reasons"], {"insufficient_or_restricted_borrow": 1})
        self.assertEqual(result["entered"], 0)

    def test_stability_failure_drives_prospective_exit(self):
        days = list(sample())
        days[2] = replace(days[2], z=2.1, stable=False)
        result = replay(days)
        self.assertTrue(result["stability_failure"])
        self.assertEqual(result["round_trips"], 1)
        opening = next(record for record in result["ledger"] if record["event"] == "fill")
        signal = next(record for record in result["ledger"] if record["event"] == "exit_signal")
        flat = next(record for record in result["ledger"] if record["event"] == "flat")
        self.assertEqual((opening["date"], signal["date"], flat["date"]),
                         ("2025-01-06", "2025-01-06", "2025-01-07"))
        self.assertEqual(signal["reason"], "stability_failure")

    def test_decoder_and_holdout_fence(self):
        self.assertEqual(decode_leg({"quotes": [], "mark": None, "median_volume": None, "borrow": [],
                                     "commission_per_share": None, "sell_fee_bps": None,
                                     "financing_bps": None}), LegDay((), None, None, (), None, None, None))
        with self.assertRaises(ValueError):
            decode_leg({"quotes": [dict(at_ms=0, bid=0, ask=1, bid_size=1, ask_size=2)], "mark": None})
        days = list(sample())
        days[-1] = replace(days[-1], day=date(2025, 11, 3))
        with self.assertRaisesRegex(ValueError, "locked development"):
            replay(days)


if __name__ == "__main__":
    unittest.main()
