import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.pair_discovery import screen, universe_ingest as importer


def holding(cusip="123456789", isin="US1234567890", category="EC", issuer="CORP", payoff="Long"):
    return f"""<invstOrSec>
      <name>Fixture issuer</name><title>Fixture shares</title><cusip>{cusip}</cusip>
      <identifiers><isin value="{isin}"/><ticker value="EXAMPLE"/></identifiers>
      <payoffProfile>{payoff}</payoffProfile><assetCat>{category}</assetCat><issuerCat>{issuer}</issuerCat>
    </invstOrSec>"""


def filing(*holdings, effective=importer.EFFECTIVE, official_length=False):
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <edgarSubmission xmlns="{importer.NPORT['n']}">
      <headerData><submissionType>NPORT-P</submissionType><filerInfo>
        <filer><issuerCredentials><cik>{importer.FILER_CIK}</cik></issuerCredentials></filer>
        <seriesClassInfo><seriesId>{importer.SERIES}</seriesId><classId>{importer.SHARE_CLASS}</classId></seriesClassInfo>
      </filerInfo></headerData>
      <formData><genInfo><regCik>{importer.FILER_CIK}</regCik><seriesId>{importer.SERIES}</seriesId>
        <repPdDate>{effective}</repPdDate></genInfo>
        <invstOrSecs>{''.join(holdings)}</invstOrSecs>
      </formData>
    </edgarSubmission>""".encode()
    return xml.ljust(importer.FILING_BYTES, b" ") if official_length else xml


def crosswalk_row(**changes):
    return {"cusip": "123456789", "isin": "US1234567890", "instrument_id": "INST-A", "issuer_id": "ISS-A",
            "kind": "common_stock", "sector": "Industrials", "country": "US", "primary_listing": "XNYS",
            "alias_from": "2019-02-01", "alias_to": "", "class_from": "2020-04-01", "class_to": "",
            "known_on": "2023-10-01", "source": "Fixture security master receipt 1", **changes}


def csv_bytes(*rows):
    stream = StringIO()
    writer = csv.DictWriter(stream, importer.CROSSWALK_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


class UniverseImport(unittest.TestCase):
    def reconcile(self, xml, *rows):
        return importer.reconcile(importer.parse_filing(xml), importer.parse_crosswalk(csv_bytes(*rows)))

    def test_valid_universe_passes_installed_screen_schema_with_deduplication(self):
        xml = filing(holding(), holding(), holding(cusip="N/A", isin="", category="STIV"),
                     holding(cusip="N/A", isin="", issuer="RF"))
        result, coverage = self.reconcile(xml, crosswalk_row())
        self.assertEqual(set(screen.load_universe(result)), {"INST-A"})
        self.assertEqual(coverage["counts"], {"excluded_filing_instrument": 2, "mapped_eligible": 2})
        self.assertEqual(coverage["eligible_instruments"], 1)
        self.assertEqual(coverage["unresolved_holdings"], 0)

    def test_no_name_or_ticker_match_and_no_future_edgar_input(self):
        result, coverage = self.reconcile(filing(holding(cusip="N/A", isin="")),
                                          crosswalk_row(instrument_id="EXAMPLE"))
        self.assertIsNone(result)
        self.assertEqual(coverage["counts"], {"missing_filing_identifier": 1})
        with self.assertRaisesRegex(ValueError, "XML identity"):
            importer.parse_filing(filing(holding(), effective="2025-11-03"))

    def test_crosswalk_intervals_and_knowledge_cutoff(self):
        variants = ({"alias_from": "2023-10-01"}, {"alias_to": importer.EFFECTIVE},
                    {"class_from": "2023-12-30"}, {"class_to": importer.ELIGIBILITY},
                    {"known_on": "2023-12-30"})
        for variant in variants:
            with self.subTest(variant=variant):
                result, coverage = self.reconcile(filing(holding()), crosswalk_row(**variant))
                self.assertIsNone(result)
                self.assertEqual(coverage["counts"], {"no_asof_crosswalk": 1})

    def test_conflicting_identifiers_or_classifications_are_reported(self):
        cases = ((crosswalk_row(isin="US0000000001"),),
                 (crosswalk_row(), crosswalk_row(isin="", instrument_id="INST-B")),
                 (crosswalk_row(), crosswalk_row(isin="", sector="Technology")))
        for rows in cases:
            with self.subTest(rows=rows):
                result, coverage = self.reconcile(filing(holding()), *rows)
                self.assertIsNone(result)
                self.assertEqual(coverage["counts"], {"conflicting_crosswalk": 1})

    def test_permanent_id_cannot_merge_different_issuers(self):
        result, coverage = self.reconcile(
            filing(holding(), holding(cusip="987654321", isin="CA9876543210")),
            crosswalk_row(), crosswalk_row(cusip="987654321", isin="CA9876543210", issuer_id="ISS-B"),
        )
        self.assertIsNone(result)
        self.assertEqual(coverage["counts"], {"conflicting_permanent_id": 2})

    def test_permanent_id_cannot_merge_distinct_security_classes(self):
        result, coverage = self.reconcile(
            filing(holding(), holding(cusip="987654321", isin="US9876543210")),
            crosswalk_row(), crosswalk_row(cusip="987654321", isin="US9876543210"),
        )
        self.assertIsNone(result)
        self.assertEqual(coverage["counts"], {"conflicting_permanent_id": 2})

    def test_xml_does_not_accept_dtd(self):
        with self.assertRaisesRegex(ValueError, "document type or entity"):
            importer.parse_filing(b"<!DOCTYPE test [ <!ENTITY value 'test'> ]><test>&value;</test>")

    def test_cli_coverage_and_pinned_complete_universe(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            xml = folder / "primary_doc.xml"
            raw = filing(holding(), official_length=True)
            xml.write_bytes(raw)
            master = folder / "crosswalk.csv"
            master.write_bytes(csv_bytes(crosswalk_row()))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                importer.inspect(xml, master, filing_sha256="0" * 64)
            for mode, additions, expected in (
                ("audit", [], 2),
                ("complete", ["--security-master", str(master), "--filing-sha256", sha256(raw).hexdigest()], 0),
            ):
                output = folder / mode
                argv = ["universe_ingest", "--filing-xml", str(xml), "--output-dir", str(output), *additions]
                with patch.object(sys, "argv", argv):
                    self.assertEqual(importer.main(), expected)
                receipt = json.loads((output / "coverage.json").read_bytes())
                self.assertEqual(receipt["filing"]["sha256"], sha256(raw).hexdigest())
                if expected == 0:
                    payload = (output / "universe.json").read_bytes()
                    self.assertEqual(sha256(payload).hexdigest(), receipt["universe_sha256"])
                    self.assertEqual(set(screen.load_universe(json.loads(payload))), {"INST-A"})
                else:
                    self.assertFalse((output / "universe.json").exists())
                    self.assertEqual(receipt["coverage"]["counts"], {"missing_crosswalk": 1})
