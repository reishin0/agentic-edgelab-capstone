"""Convert the frozen September 2023 IWB filing and dated security master.

Use the raw SEC primary_doc.xml (not rendered HTML) and a UTF-8 crosswalk CSV
with the header in CROSSWALK_FIELDS. Dates are YYYY-MM-DD. End dates are
exclusive and may be empty. Alias intervals cover the filing effective date;
classification intervals cover the eligibility cutoff. The known_on column
attests when those mapping and classification facts were available. Source
must identify the historical security-master receipt. Country is the country
of the primary listing; primary_listing is its ISO MIC.

The coverage output keeps filing row ordinals, aliases, exclusions and gaps.
An unresolved import does not write universe.json. This command reads local
development files only; it does not acquire or read market holdout data.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET


ACCESSION = "0001752724-23-265165"
FILING = "https://www.sec.gov/Archives/edgar/data/1100663/000175272423265165/primary_doc.xml"
EDGAR_DIRECTORY = FILING.removesuffix("/primary_doc.xml")
FILING_BYTES = 916104
FILER_CIK = "0001100663"
SERIES = "S000004347"
SHARE_CLASS = "C000012077"
EFFECTIVE = "2023-09-30"
PUBLISHED = "2023-11-24"
ELIGIBILITY = "2023-12-29"
NPORT = {"n": "http://www.sec.gov/edgar/nport"}
CROSSWALK_FIELDS = (
    "cusip", "isin", "instrument_id", "issuer_id", "kind", "sector", "country",
    "primary_listing", "alias_from", "alias_to", "class_from", "class_to", "known_on", "source",
)
KINDS = frozenset({"common_stock", "preferred_stock", "fund", "depositary_receipt", "other"})
CUSIP = re.compile(r"[A-Z0-9*@#]{9}")
ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
PERMANENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
COUNTRY = re.compile(r"[A-Z]{2}")
MIC = re.compile(r"[A-Z0-9]{4}")
HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Holding:
    row: int
    issuer: str
    title: str
    cusip: str
    isin: str
    asset_category: str
    issuer_category: str
    payoff: str

    @property
    def aliases(self):
        return matching_aliases(self.cusip, self.isin)


@dataclass(frozen=True)
class CrosswalkRow:
    line: int
    cusip: str
    isin: str
    member: dict
    alias_from: str
    alias_to: str
    class_from: str
    class_to: str
    source: str

    @property
    def aliases(self):
        return matching_aliases(self.cusip, self.isin)

    def available(self):
        return (self.alias_from <= EFFECTIVE and (not self.alias_to or EFFECTIVE < self.alias_to)
                and self.class_from <= ELIGIBILITY and (not self.class_to or ELIGIBILITY < self.class_to)
                and self.member["known_on"] <= ELIGIBILITY)


def matching_aliases(cusip, isin):
    return {kind: value for kind, value, pattern in (("cusip", cusip, CUSIP), ("isin", isin, ISIN))
            if pattern.fullmatch(value) and value not in ("000000000", "XXXXXXXXX")}


def iso_day(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError(f"Expected ISO date, found {value!r}")
    return value


def field(node, path, attribute=None):
    children = node.findall(path, namespaces=NPORT)
    if len(children) > 1:
        raise ValueError(f"Repeated filing field {path}")
    if not children:
        return ""
    value = children[0].get(attribute) if attribute else children[0].text
    return (value or "").strip()


def parse_filing(raw):
    # The SEC source is UTF-8. Reject DTDs and entity declarations, including
    # alternative encodings that could hide the declarations from inspection.
    document = raw.decode("utf-8-sig")
    if "\x00" in document or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", document, re.I):
        raise ValueError("Expected the SEC UTF-8 filing without a document type or entity declaration")
    root = ET.fromstring(document)
    expected = {
        "n:headerData/n:submissionType": "NPORT-P",
        "n:headerData/n:filerInfo/n:filer/n:issuerCredentials/n:cik": FILER_CIK,
        "n:headerData/n:filerInfo/n:seriesClassInfo/n:seriesId": SERIES,
        "n:headerData/n:filerInfo/n:seriesClassInfo/n:classId": SHARE_CLASS,
        "n:formData/n:genInfo/n:regCik": FILER_CIK,
        "n:formData/n:genInfo/n:seriesId": SERIES,
        "n:formData/n:genInfo/n:repPdDate": EFFECTIVE,
    }
    if root.tag != "{" + NPORT["n"] + "}edgarSubmission" or any(
            field(root, path) != value for path, value in expected.items()):
        raise ValueError(f"XML identity does not match the frozen IWB N-PORT filing {ACCESSION}")

    tables = root.findall("n:formData/n:invstOrSecs", namespaces=NPORT)
    if len(tables) != 1 or not len(tables[0]):
        raise ValueError("Filing has no unique holdings table")
    holdings = []
    for row, element in enumerate(tables[0], 1):
        if element.tag != "{" + NPORT["n"] + "}invstOrSec":
            raise ValueError("Unexpected filing holdings table element")
        holdings.append(Holding(
            row, field(element, "n:name"), field(element, "n:title"),
            field(element, "n:cusip").upper(), field(element, "n:identifiers/n:isin", "value").upper(),
            field(element, "n:assetCat") or field(element, "n:assetConditional", "assetCat"),
            field(element, "n:issuerCat") or field(element, "n:issuerConditional", "issuerCat"),
            field(element, "n:payoffProfile").lower(),
        ))
    return holdings


def parse_crosswalk(raw):
    reader = csv.DictReader(StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    if reader.fieldnames != list(CROSSWALK_FIELDS):
        raise ValueError("Crosswalk CSV requires these columns in order: " + ", ".join(CROSSWALK_FIELDS))
    result = []
    for row in reader:
        line = reader.line_num
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Crosswalk line {line} has the wrong number of fields")
        row = {key: value.strip() for key, value in row.items()}
        try:
            aliases = matching_aliases(row["cusip"], row["isin"])
            if not aliases or any(row[key] and key not in aliases for key in ("cusip", "isin")):
                raise ValueError("Supply at least one valid filing alias")
            if not all(PERMANENT_ID.fullmatch(row[key]) for key in ("instrument_id", "issuer_id")):
                raise ValueError("Instrument and issuer need stable identifiers")
            if (row["kind"] not in KINDS or not COUNTRY.fullmatch(row["country"])
                    or not MIC.fullmatch(row["primary_listing"]) or not row["source"]
                    or not row["sector"] or row["sector"].casefold() in {"unknown", "n/a", "none"}
                    or not all(row[key].isprintable() for key in ("source", "sector"))):
                raise ValueError("Classification, listing, sector and source provenance are required")
            for prefix in ("alias", "class"):
                begin, end = row[f"{prefix}_from"], row[f"{prefix}_to"]
                iso_day(begin)
                if end and iso_day(end) <= begin:
                    raise ValueError(f"Invalid {prefix} interval")
            iso_day(row["known_on"])
        except ValueError as error:
            raise ValueError(f"Crosswalk line {line}: {error}") from None
        member = {key: row[key] for key in ("kind", "sector", "country", "primary_listing", "known_on")}
        member.update(id=row["instrument_id"], issuer=row["issuer_id"])
        result.append(CrosswalkRow(line, row["cusip"], row["isin"], member, row["alias_from"], row["alias_to"],
                                   row["class_from"], row["class_to"], row["source"]))
    return result


def match(holding, crosswalk):
    aliases = holding.aliases
    if not aliases:
        return "missing_filing_identifier", []
    candidates = {entry.line: entry for alias in aliases.items() for entry in crosswalk[alias]}
    if not candidates:
        return "missing_crosswalk", []
    available = [entry for entry in candidates.values() if entry.available()]
    if not available:
        return "no_asof_crosswalk", sorted(candidates)
    conflicts = any(value != aliases[kind] for entry in available for kind, value in entry.aliases.items()
                    if kind in aliases)
    facts = {tuple((key, value) for key, value in entry.member.items() if key != "known_on") for entry in available}
    if conflicts or len(facts) != 1:
        return "conflicting_crosswalk", sorted(entry.line for entry in available)
    return "mapped", available


def reconcile(holdings, crosswalk_rows):
    crosswalk = defaultdict(list)
    for entry in crosswalk_rows:
        for alias in entry.aliases.items():
            crosswalk[alias].append(entry)
    records, members, groups = [], {}, defaultdict(list)
    for holding in holdings:
        record = {"row": holding.row, "issuer_name": holding.issuer, "title": holding.title, "cusip": holding.cusip,
                  "isin": holding.isin, "asset_category": holding.asset_category, "issuer_category": holding.issuer_category,
                  "payoff": holding.payoff}
        records.append(record)
        if not holding.asset_category or (holding.asset_category == "EC" and holding.payoff not in {"short", "long"}):
            record["status"] = "missing_filing_classification"
            continue
        if holding.asset_category != "EC" or holding.issuer_category in {"RF", "PF"} or holding.payoff == "short":
            record["status"] = "excluded_filing_instrument"
            continue
        status, evidence = match(holding, crosswalk)
        record["status"] = status
        if status != "mapped":
            record["crosswalk_lines"] = evidence
            continue
        record["crosswalk_lines"] = sorted(entry.line for entry in evidence)
        record["crosswalk_sources"] = sorted({entry.source for entry in evidence})
        member = dict(evidence[0].member)
        member["known_on"] = max(entry.member["known_on"] for entry in evidence)
        record["instrument_id"] = member["id"]
        record["status"] = "mapped_eligible" if member["kind"] == "common_stock" and member["country"] == "US" else "mapped_excluded"
        groups[member["id"]].append((record, member, holding.aliases))

    for identifier, group in groups.items():
        facts = {tuple((key, value) for key, value in member.items() if key != "known_on") for _, member, _ in group}
        alias_conflict = any(len({aliases[kind] for _, _, aliases in group if kind in aliases}) > 1
                             for kind in ("cusip", "isin"))
        if len(facts) != 1 or alias_conflict:
            for record, _, _ in group:
                record["status"] = "conflicting_permanent_id"
            continue
        if group[0][0]["status"] == "mapped_eligible":
            members[identifier] = dict(group[0][1])
            members[identifier]["known_on"] = max(member["known_on"] for _, member, _ in group)

    counts = Counter(record["status"] for record in records)
    closed = {"excluded_filing_instrument", "mapped_eligible", "mapped_excluded"}
    unresolved = sum(count for reason, count in counts.items() if reason not in closed)
    universe = None
    if not unresolved and members:
        universe = {"index": "IWB", "effective": EFFECTIVE, "published": PUBLISHED,
                    "members": [members[key] for key in sorted(members)]}
    return universe, {"holdings": len(records), "unresolved_holdings": unresolved, "eligible_instruments": len(members),
                      "counts": dict(sorted(counts.items())), "records": records}


def raw_bytes(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds the input size limit")
    return data


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def inspect(xml_path, crosswalk_path=None, *, filing_sha256=None):
    raw = raw_bytes(xml_path, FILING_BYTES)
    xml_hash = sha256(raw).hexdigest()
    if len(raw) != FILING_BYTES:
        raise ValueError(f"SEC directory lists {FILING_BYTES} bytes for the frozen filing, found {len(raw)}")
    if filing_sha256 is not None and (not HEX.fullmatch(filing_sha256) or filing_sha256 != xml_hash):
        raise ValueError("Filing SHA-256 does not match the supplied source pin")
    holdings = parse_filing(raw)
    master = None if crosswalk_path is None else raw_bytes(crosswalk_path, 32 * 1024 * 1024)
    rows = [] if master is None else parse_crosswalk(master)
    universe, coverage = reconcile(holdings, rows)
    output = json_bytes(universe) if universe is not None else None
    report = {
        "schema_version": 1,
        "status": "complete" if output is not None else "unresolved",
        "holdout": "locked and untested",
        "filing": {"source": FILING, "directory": EDGAR_DIRECTORY, "accession": ACCESSION, "cik": FILER_CIK,
                   "series": SERIES, "class": SHARE_CLASS, "effective": EFFECTIVE, "published": PUBLISHED,
                   "bytes": len(raw), "sha256": xml_hash, "checked_against_supplied_pin": filing_sha256 is not None},
        "crosswalk": None if master is None else {"sha256": sha256(master).hexdigest(), "rows": len(rows),
                                                 "as_of": ELIGIBILITY},
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "universe_sha256": None if output is None else sha256(output).hexdigest(),
        "coverage": coverage,
    }
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--filing-xml", type=Path, required=True)
    parser.add_argument("--security-master", type=Path)
    parser.add_argument("--filing-sha256")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        output, report = inspect(args.filing_xml, args.security_master, filing_sha256=args.filing_sha256)
        args.output_dir.mkdir(exist_ok=False)
        (args.output_dir / "coverage.json").write_bytes(json_bytes(report))
        if output is not None:
            (args.output_dir / "universe.json").write_bytes(output)
    except (OSError, ValueError, csv.Error, ET.ParseError) as error:
        parser.exit(1, f"{error}\n")
    print(f"{report['status']}: {report['coverage']['unresolved_holdings']} unresolved filing holdings")
    return 0 if output is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
