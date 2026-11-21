import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from research.pair_discovery import universe_ingest as ingest, universe_nport_archive as nport


def tables():
    exact = ingest.ACCESSION
    return {
        "SUBMISSION.tsv": [("0001752724-23-000001", "24-NOV-2023", "NPORT-P", "30-SEP-2023"),
                           (exact, "24-NOV-2023", "NPORT-P", "30-SEP-2023")],
        "REGISTRANT.tsv": [(exact, ingest.FILER_CIK)],
        "FUND_REPORTED_INFO.tsv": [(exact, ingest.SERIES, "iShares Russell 1000 ETF")],
        "FUND_REPORTED_HOLDING.tsv": [("0001752724-23-000001", "1", "Excluded", "Excluded", "111111111", "EC", "CORP", "Long"),
                                      (exact, "2", "Sample Co", "Sample Common", "123456789", "EC", "CORP", "Long")],
        "IDENTIFIERS.tsv": [("1", "US1111111111"), ("2", "US1234567890")],
    }


def make_zip(path, members):
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, rows in members.items():
            data = StringIO(newline="")
            writer = csv.writer(data, delimiter="\t")
            writer.writerow(nport.TABLES[name])
            writer.writerows(rows)
            archive.writestr(name, data.getvalue())


def make_crosswalk(path):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(ingest.CROSSWALK_FIELDS)
        writer.writerow(("123456789", "US1234567890", "sec-1234", "issuer-12", "common_stock", "Technology", "US",
                         "XNYS", "2023-01-01", "", "2023-01-01", "", "2023-01-01", "synthetic security master"))


class QuarterlyArchiveTests(unittest.TestCase):
    def test_archive_can_reproduce_pinned_universe(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "nport.zip"
            crosswalk = root / "master.csv"
            make_zip(archive, tables())
            make_crosswalk(crosswalk)
            expected = sha256(archive.read_bytes()).hexdigest()
            output, report = nport.inspect_archive(archive, crosswalk, archive_sha256=expected)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["filing"]["archive_sha256"], expected)
            self.assertEqual(report["coverage"]["holdings"], 1)
            self.assertEqual(json.loads(output)["members"][0]["id"], "sec-1234")

    def test_missing_crosswalk_generates_coverage_only(self):
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "nport.zip"
            make_zip(archive, tables())
            output, report = nport.inspect_archive(archive)
            self.assertIsNone(output)
            self.assertEqual(report["coverage"]["counts"], {"missing_crosswalk": 1})
            self.assertEqual(report["holdout"], "locked and untested")

    def test_identifier_join_requires_unambiguous_holding_ids(self):
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "nport.zip"
            cases = tables()
            other = cases["FUND_REPORTED_HOLDING.tsv"][0]
            cases["FUND_REPORTED_HOLDING.tsv"][0] = (other[0], "2", *other[2:])
            make_zip(archive, cases)
            with self.assertRaisesRegex(ValueError, "overlap another accession"):
                nport.inspect_archive(archive)

    def test_wrong_dates_duplicates_and_pin_never_produce_a_universe(self):
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "nport.zip"
            cases = tables()
            correct = cases["SUBMISSION.tsv"][-1]
            cases["SUBMISSION.tsv"][-1] = (*correct[:-1], "31-DEC-2023")
            make_zip(archive, cases)
            with self.assertRaisesRegex(ValueError, "frozen IWB filing"):
                nport.inspect_archive(archive)
            cases["SUBMISSION.tsv"][-1] = correct
            cases["IDENTIFIERS.tsv"].append(("2", "US1234567888"))
            make_zip(archive, cases)
            with self.assertRaisesRegex(ValueError, "Conflicting ISINs"):
                nport.inspect_archive(archive)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                nport.inspect_archive(archive, archive_sha256="1" * 64)
