"""Seal normalized development exports for the EdgeLab discovery runner."""

import argparse
import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path

from . import screen


NORMALIZED_BAR_FIELDS = ("instrument_id", "session", "raw_close_usd", "consolidated_volume",
                         "split_ratio", "distribution_usd_per_post_split_share", "action_known_on")
REQUIRED = {"universe": "universe.json", "daily_history": "daily_history.csv"}
OPTIONAL = {"execution": "execution.json"}


def read_receipt(path):
    path = Path(path).resolve(strict=True)
    receipt = json.loads(path.read_bytes())
    screen.require(isinstance(receipt, dict) and set(receipt) == {"schema", "classification", "files"}
                   and type(receipt["schema"]) is int and receipt["schema"] == 1
                   and receipt["classification"] in ("equity-development", "synthetic-development"),
                   "Invalid normalized development receipt")
    records = receipt["files"]
    allowed = {**REQUIRED, **OPTIONAL}
    screen.require(isinstance(records, dict) and set(REQUIRED) <= set(records) <= set(allowed),
                   "Receipt requires normalized universe and daily history sources")
    for key, entry in records.items():
        screen.require(isinstance(entry, dict) and set(entry) == {"name", "source", "sha256", "through"}
                       and entry["name"] == allowed[key] and entry["source"] and isinstance(entry["source"], str)
                       and screen.day(entry["through"]) <= (screen.ELIGIBILITY_END if key == "universe"
                                                            else screen.CALIBRATION_END)
                       and isinstance(entry["sha256"], str) and len(entry["sha256"]) == 64
                       and all(c in "0123456789abcdef" for c in entry["sha256"]),
                       f"Unsafe or incomplete normalized source scope for {key}")
    return path.parent, receipt


def source_bytes(folder, record):
    path = folder / record["name"]
    screen.require(path.resolve(strict=True) == path and path.is_file(), "Source export must be a plain file")
    raw = path.read_bytes()
    screen.require(sha256(raw).hexdigest() == record["sha256"], f"Source SHA-256 mismatch for {record['name']}")
    return raw


def normalized_bars(raw, members, sessions, through):
    reader = csv.DictReader(StringIO(raw.decode("utf-8-sig"), newline=""))
    screen.require(tuple(reader.fieldnames or ()) == NORMALIZED_BAR_FIELDS, "Invalid normalized daily history header")
    allowed = set(sessions)
    result = {}
    for row in reader:
        screen.require(None not in row and all(value is not None for value in row.values()),
                       f"Malformed normalized history row {reader.line_num}")
        instrument, stamp = row["instrument_id"], screen.day(row["session"])
        screen.require(instrument in members and stamp in allowed and stamp <= through,
                       f"Daily history outside declared development scope at row {reader.line_num}")
        history = result.setdefault(instrument, {})
        screen.require(stamp not in history, f"Duplicate instrument/session at row {reader.line_num}")
        try:
            history[stamp] = {"close": float(row["raw_close_usd"]), "volume": int(row["consolidated_volume"]),
                              "split": float(row["split_ratio"]),
                              "distribution": float(row["distribution_usd_per_post_split_share"]),
                              "action_known_on": row["action_known_on"]}
        except (ValueError, OverflowError) as error:
            raise ValueError(f"Invalid price, volume or action at row {reader.line_num}") from error
    screen.check_bars(result, members, sessions)
    return result


def prepare(receipt_path, output):
    folder, receipt = read_receipt(receipt_path)
    records = receipt["files"]
    sources = {name: source_bytes(folder, record) for name, record in records.items()}
    eligibility, formation, calibration = screen.development_sessions()
    sessions = eligibility + formation + calibration
    universe = json.loads(sources["universe"])
    members = screen.load_universe(universe)
    screen.require(universe["published"] <= records["universe"]["through"]
                   and all(member["known_on"] <= records["universe"]["through"] for member in members.values()),
                   "Universe data exceeds its stated knowledge date")
    bars = normalized_bars(sources["daily_history"], members, sessions, records["daily_history"]["through"])
    export = {
        "calendar": {"calendar": "NYSE Group development calendar v1", "sessions": list(sessions)},
        "universe": universe, "bars": bars,
    }
    if "execution" in sources:
        quotes = json.loads(sources["execution"])
        dates = set(stamp for stamp in calibration if stamp <= records["execution"]["through"])
        screen.require(isinstance(quotes, dict) and set(quotes) <= set(members)
                       and all(isinstance(rows, dict) and set(rows) <= dates for rows in quotes.values()),
                       "Execution data exceeds its stated instruments or scope")
        export["execution"] = quotes

    coverage = {key: {"observations": len(bars.get(key, {})),
                      "missing_eligibility": sum(stamp not in bars.get(key, {}) for stamp in eligibility),
                      "missing_formation": sum(stamp not in bars.get(key, {}) for stamp in formation),
                      "missing_calibration": sum(stamp not in bars.get(key, {}) for stamp in calibration),
                      "execution_dates": len(export.get("execution", {}).get(key, {}))}
                for key in sorted(members)}
    manifest = {"schema_version": 1, "classification": receipt["classification"], "through": screen.CALIBRATION_END,
                "holdout_start": screen.HOLDOUT_START, "files": {}}
    output = Path(output)
    output.mkdir(exist_ok=False)
    for name, value in export.items():
        source = records.get("daily_history" if name == "bars" else name)
        file = output / f"{name}.json"
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        file.write_bytes(raw)
        manifest["files"][name] = {"name": file.name, "sha256": sha256(raw).hexdigest(),
                                    "source": f"{source['source']} [input SHA-256 {source['sha256']}]" if source
                                    else "NYSE calendar rules in EdgeLab screen.py",
                                    "through": source["through"] if source else screen.CALIBRATION_END}
    (output / "coverage.json").write_text(json.dumps({"source_receipt": receipt, "instruments": coverage}, indent=2) + "\n",
                                          encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.receipt, args.output)


if __name__ == "__main__":
    main()
