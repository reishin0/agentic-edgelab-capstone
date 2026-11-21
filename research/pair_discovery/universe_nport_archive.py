"""Import the exact IWB accession from a local SEC 2023 Q4 N-PORT ZIP."""

import argparse
from collections import defaultdict
import csv
from datetime import date
from hashlib import file_digest, sha256
from io import TextIOWrapper
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

from . import universe_ingest as ingest


ARCHIVE = "https://www.sec.gov/files/dera/data/form-n-port-data-sets/2023q4_nport.zip"
ARCHIVE_MAX_BYTES = 512 * 1024**2
TABLE_MAX_BYTES = 2 * 1024**3
TABLES = {
    "SUBMISSION.tsv": ("ACCESSION_NUMBER", "FILING_DATE", "SUB_TYPE", "REPORT_DATE"),
    "REGISTRANT.tsv": ("ACCESSION_NUMBER", "CIK"),
    "FUND_REPORTED_INFO.tsv": ("ACCESSION_NUMBER", "SERIES_ID", "SERIES_NAME"),
    "FUND_REPORTED_HOLDING.tsv": ("ACCESSION_NUMBER", "HOLDING_ID", "ISSUER_NAME", "ISSUER_TITLE", "ISSUER_CUSIP",
                                  "ASSET_CAT", "ISSUER_TYPE", "PAYOFF_PROFILE"),
    "IDENTIFIERS.tsv": ("HOLDING_ID", "IDENTIFIER_ISIN"),
}
MONTHS = {name: index for index, name in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def _sec_date(value):
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value).isoformat()
    if match := re.fullmatch(r"([0-9]{2})-([A-Z]{3})-([0-9]{4})", value):
        day, month, year = match.groups()
        if month in MONTHS:
            return date(int(year), MONTHS[month], int(day)).isoformat()
    raise ValueError(f"Unsupported SEC table date {value!r}")


def _rows(archive, name):
    matches = [info for info in archive.infolist() if info.filename.casefold() == name.casefold()]
    if len(matches) != 1 or not 0 < matches[0].file_size <= TABLE_MAX_BYTES:
        raise ValueError(f"Expected one supported {name} table")
    with archive.open(matches[0]) as stream, TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
        reader = csv.reader(text, delimiter="\t", strict=True)
        header = next(reader, [])
        columns = TABLES[name]
        if len(set(header)) != len(header) or not set(columns) <= set(header):
            raise ValueError(f"Unexpected {name} column names")
        indices = [header.index(column) for column in columns]
        for row in reader:
            if len(row) != len(header):
                raise ValueError(f"Unexpected column count in {name} at line {reader.line_num}")
            yield tuple(row[index].strip() for index in indices)


def _target_rows(archive, table):
    return (row[1:] for row in _rows(archive, table) if row[0] == ingest.ACCESSION)


def _unique(archive, table):
    rows = list(_target_rows(archive, table))
    if len(rows) != 1:
        raise ValueError(f"Expected one row for {ingest.ACCESSION} in {table}")
    return rows[0]


def _holdings(archive):
    filed, form, effective = _unique(archive, "SUBMISSION.tsv")
    (cik,) = _unique(archive, "REGISTRANT.tsv")
    series, series_name = _unique(archive, "FUND_REPORTED_INFO.tsv")
    if (_sec_date(filed), form, _sec_date(effective), cik.zfill(10), series, series_name) != (
            ingest.PUBLISHED, "NPORT-P", ingest.EFFECTIVE, ingest.FILER_CIK, ingest.SERIES, "iShares Russell 1000 ETF"):
        raise ValueError("SEC bulk archive metadata does not match the frozen IWB filing")

    rows = list(_target_rows(archive, "FUND_REPORTED_HOLDING.tsv"))
    ids = {row[0] for row in rows}
    if not rows or len(ids) != len(rows) or any(not value.isascii() or not value.isdecimal() for value in ids):
        raise ValueError("Invalid target accession holding IDs")
    if any(row[0] != ingest.ACCESSION and row[1] in ids for row in _rows(archive, "FUND_REPORTED_HOLDING.tsv")):
        raise ValueError("Target holding IDs overlap another accession")
    aliases = defaultdict(set)
    for holding_id, isin in _rows(archive, "IDENTIFIERS.tsv"):
        if holding_id in ids and ingest.ISIN.fullmatch(isin.upper()):
            aliases[holding_id].add(isin.upper())
    if any(len(isins) > 1 for isins in aliases.values()):
        raise ValueError("Conflicting ISINs for a holding")
    return [ingest.Holding(index, issuer, title, cusip.upper(), next(iter(aliases[holding_id]), ""),
                           asset, issuer_type, payoff.lower())
            for index, (holding_id, issuer, title, cusip, asset, issuer_type, payoff) in enumerate(rows, 1)]


def inspect_archive(archive_path, crosswalk_path=None, *, archive_sha256=None):
    with Path(archive_path).open("rb") as stream:
        size = stream.seek(0, 2)
        if not 0 < size <= ARCHIVE_MAX_BYTES:
            raise ValueError("Unsupported SEC N-PORT archive size")
        stream.seek(0)
        digest = file_digest(stream, "sha256").hexdigest()
        if archive_sha256 is not None and (not ingest.HEX.fullmatch(archive_sha256) or digest != archive_sha256):
            raise ValueError("SEC archive SHA-256 does not match the supplied source pin")
        with ZipFile(stream) as archive:
            holdings = _holdings(archive)

    master = None if crosswalk_path is None else ingest.raw_bytes(Path(crosswalk_path), 32 * 1024**2)
    rows = [] if master is None else ingest.parse_crosswalk(master)
    universe, coverage = ingest.reconcile(holdings, rows)
    output = None if universe is None else ingest.json_bytes(universe)
    report = {
        "schema_version": 1,
        "status": "complete" if output is not None else "unresolved",
        "holdout": "locked and untested",
        "filing": {"source": ARCHIVE, "accession": ingest.ACCESSION, "cik": ingest.FILER_CIK,
                   "series": ingest.SERIES, "effective": ingest.EFFECTIVE, "published": ingest.PUBLISHED,
                   "bytes": size, "archive_sha256": digest, "checked_against_supplied_pin": archive_sha256 is not None},
        "crosswalk": None if master is None else {"sha256": sha256(master).hexdigest(), "rows": len(rows),
                                                 "as_of": ingest.ELIGIBILITY},
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "reconciler_sha256": sha256(Path(ingest.__file__).read_bytes()).hexdigest(),
        "universe_sha256": None if output is None else sha256(output).hexdigest(),
        "coverage": coverage,
    }
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256")
    parser.add_argument("--security-master", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        universe, report = inspect_archive(args.archive, args.security_master, archive_sha256=args.archive_sha256)
        args.output_dir.mkdir(exist_ok=False)
        (args.output_dir / "coverage.json").write_bytes(ingest.json_bytes(report))
        if universe is not None:
            (args.output_dir / "universe.json").write_bytes(universe)
    except (OSError, ValueError, csv.Error, BadZipFile) as error:
        parser.exit(1, f"{error}\n")
    return 0 if universe is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
