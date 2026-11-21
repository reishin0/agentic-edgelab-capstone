from datetime import date, datetime, time, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from edgelab.dashboard import run_view
from edgelab.history import FrozenHistory
from edgelab.journal import Journal, JournalConflict
from edgelab.market_history import calendar_2025, freeze_pair, store_history
from edgelab.methods import local_notes
from edgelab.planning import search
from edgelab.providers import ProviderError
from edgelab.reporting import report_from_journal
from edgelab.research import ResearchService
from observation_fixtures import RUNTIME, bound, roots


START = date(2025, 1, 2)
END = date(2025, 10, 31)
SYMBOLS = ("SPY", "QQQ")
KEYS = {"EDGELAB_NEWS_KEY": "fixture-key", "EDGELAB_NEWS_SECRET": "fixture-secret"}


def reply():
    dates = calendar_2025(START, END)
    tz = ZoneInfo("America/New_York")
    stamps = [datetime.combine(date.fromisoformat(day), time.min, tz).astimezone(timezone.utc).isoformat()
              for day in dates]
    return {"bars": {symbol: [{"t": stamp, "c": 300 + i / (1 + j)} for i, stamp in enumerate(stamps)]
                     for j, symbol in enumerate(SYMBOLS)}, "next_page_token": None}


class HistoricalIntakeTests(unittest.TestCase):
    def test_one_request_frozen_before_holdout_and_does_not_save_credentials(self):
        transport = Mock(return_value=reply())
        manifest, csv = freeze_pair(KEYS, SYMBOLS, START, END, feed="iex", transport=transport)
        transport.assert_called_once()
        url = urlsplit(transport.call_args.args[0])
        query = parse_qs(url.query)
        self.assertEqual(url.netloc, "data.alpaca.markets")
        self.assertEqual(query["adjustment"], ["raw"])
        self.assertEqual(query["timeframe"], ["1Day"])
        self.assertTrue(query["end"][0].startswith(str(END)))
        self.assertEqual(len(manifest["sessions"]), 209)
        self.assertNotIn("2025-01-09", manifest["sessions"])
        self.assertEqual(manifest["window"]["holdout_start"], "2025-11-03")
        self.assertNotIn(b"fixture-secret", csv)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = store_history(root, manifest, csv)
            self.assertEqual(store_history(root, manifest, csv), path)
            self.assertEqual(len(FrozenHistory.open(path).sample()), 209)
            path.with_name("paired.csv").write_bytes(csv + b"2025-11-03,1,2\n")
            with self.assertRaisesRegex(ValueError, "frozen SHA-256"):
                store_history(root, manifest, csv)

    def test_missing_duplicate_extra_and_paginated_bars_are_not_admitted(self):
        for case in ("missing", "duplicate", "extra", "paginated"):
            with self.subTest(case=case):
                packet = reply()
                rows = packet["bars"]["QQQ"]
                if case == "missing":
                    rows.pop()
                elif case == "duplicate":
                    rows[-1] = rows[0]
                elif case == "extra":
                    rows[-1] = {"t": "2025-11-03T05:00:00Z", "c": 300.0}
                else:
                    packet["next_page_token"] = "would-have-retried"
                with self.assertRaises(ProviderError):
                    freeze_pair(KEYS, SYMBOLS, START, END, feed="iex", transport=Mock(return_value=packet))

    def test_report_and_explicit_local_chart_follow_approval_and_evidence(self):
        manifest, csv = freeze_pair(KEYS, SYMBOLS, START, END, feed="iex", transport=Mock(return_value=reply()))
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = store_history(root, manifest, csv)
            history = FrozenHistory.open(path)
            journal = Journal(root / "journal.sqlite")
            reader = Journal(journal.path, read_only=True)
            tool = Mock(side_effect=lambda *args, **kwargs: bound(*args, {"a": roots("unresolved"), "b": roots()}))
            service = ResearchService(journal, history=history, numerical_tool=tool, runtime_identity=lambda: RUNTIME)
            run = str(uuid4())
            plan = service.record_plan(run, None, search(local_notes("cointegration")), "local")
            self.assertIsNone(report_from_journal(reader, run)["report"]["verdict"])
            self.assertIsNone(run_view(reader, run, history)["chart"])
            service.approve(run, plan["candidates"][0]["digest"])
            service.action(run, "diagnostics")
            service.critique(run, "diagnostics")
            service.finalize(run)
            report = report_from_journal(reader, run)
            self.assertEqual(report["report"]["verdict"], "unresolved")
            self.assertEqual(report["report"]["evidence_refs"], ["result:diagnostics"])
            self.assertFalse(report["bundle"]["raw_series_included"])
            self.assertIsNone(run_view(reader, run)["chart"])
            chart = run_view(reader, run, history)["chart"]
            self.assertEqual(chart["days"][-1], str(END))
            self.assertEqual(len(chart["a"]), 209)
            path.with_name("paired.csv").write_bytes(csv + b"\n")
            self.assertIsNone(run_view(reader, run, history)["chart"])
            self.assertEqual(report_from_journal(reader, run), report)

    def test_report_refuses_a_result_without_its_bound_reservation(self):
        with TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "journal.sqlite")
            run = str(uuid4())
            service = ResearchService(journal)
            service.record_plan(run, "shared-trend", search(local_notes("cointegration")), "local")
            journal.append(run, "result:diagnostics", {"id": "result:diagnostics"})
            with self.assertRaisesRegex(JournalConflict, "binding"):
                report_from_journal(journal, run)
