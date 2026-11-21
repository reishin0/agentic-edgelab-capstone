"""Join sealed raw SIP bars with independently reconciled corporate actions."""

import argparse
from collections import Counter
import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path

from .prepare_development import NORMALIZED_BAR_FIELDS
from .screen import CALIBRATION_END, day, development_sessions, positive, require


def read_pinned(path, expected):
    raw = Path(path).read_bytes()
    require(sha256(raw).hexdigest() == expected, f"Source digest mismatch for {Path(path).name}")
    return json.loads(raw)


def join(bars_packet, actions_packet):
    require(isinstance(bars_packet, dict) and type(bars_packet.get("schema")) is int
            and bars_packet["schema"] == 1 and bars_packet.get("feed") == "sip"
            and bars_packet.get("adjustment") == "raw", "Expected a raw SIP bars packet")
    window = bars_packet.get("window")
    require(isinstance(window, dict) and {"start", "end"} <= set(window)
            and day(window["start"]) <= day(window["end"]) <= CALIBRATION_END,
            "Raw bars scope must end before market holdout")
    sessions = tuple(stamp for phase in development_sessions() for stamp in phase
                     if window["start"] <= stamp <= window["end"])
    require(bool(sessions), "Bar window contains no development calendar sessions")
    dates = set(sessions)
    bars = bars_packet.get("bars")
    require(isinstance(bars, dict) and bool(bars), "Raw bars packet has no instruments")
    require(isinstance(actions_packet, dict) and set(actions_packet) == {"schema", "source", "instruments"}
            and type(actions_packet["schema"]) is int and actions_packet["schema"] == 1
            and isinstance(actions_packet["source"], str) and bool(actions_packet["source"].strip())
            and isinstance(actions_packet["instruments"], dict)
            and set(actions_packet["instruments"]) <= set(bars), "Invalid scoped action-coverage packet")

    for instrument, rows in bars.items():
        require(isinstance(instrument, str) and bool(instrument.strip()) and isinstance(rows, dict)
                and set(rows) <= dates, "Raw bars contain an invalid instrument or out-of-scope session")
        for row in rows.values():
            require(isinstance(row, dict) and set(row) == {"close", "volume"} and positive(row["close"])
                    and type(row["volume"]) is int and row["volume"] >= 0, "Invalid raw price or volume")
    certificates = {}
    for instrument, packet in actions_packet["instruments"].items():
        require(isinstance(packet, dict) and set(packet) == {"covered", "events", "unresolved"}
                and isinstance(packet["covered"], list) and isinstance(packet["events"], dict)
                and isinstance(packet["unresolved"], dict), "Invalid instrument action coverage")
        covered = set()
        for span in packet["covered"]:
            require(isinstance(span, list) and len(span) == 2 and day(span[0]) in dates and day(span[1]) in dates
                    and span[0] <= span[1], "An action-coverage span must lie in the development bar window")
            covered.update(stamp for stamp in sessions if span[0] <= stamp <= span[1])
        require(set(packet["events"]) <= covered and set(packet["unresolved"]) <= dates
                and not set(packet["events"]) & set(packet["unresolved"]), "Event conflicts with action coverage")
        for stamp, event in packet["events"].items():
            require(isinstance(event, dict) and set(event) == {"split", "distribution", "known_on", "evidence"}
                    and positive(event["split"]) and type(event["distribution"]) in (int, float)
                    and (event["distribution"] == 0 or positive(event["distribution"]))
                    and day(event["known_on"]) <= stamp and isinstance(event["evidence"], str)
                    and bool(event["evidence"].strip()), "Unverifiable effective corporate action")
        require(all(isinstance(reason, str) and bool(reason.strip()) for reason in packet["unresolved"].values()),
                "Unresolved actions need source explanations")
        certificates[instrument] = covered, packet["events"], packet["unresolved"]

    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(NORMALIZED_BAR_FIELDS)
    evidence = {}
    for instrument, rows in sorted(bars.items()):
        covered, events, unresolved = certificates.get(instrument, (set(), {}, {}))
        skipped, counts = {}, Counter()
        for stamp in sessions:
            if stamp in unresolved:
                reason = "unresolved_action"
            elif stamp not in covered:
                reason = "action_coverage_missing"
            elif stamp not in rows:
                reason = "bar_missing"
            else:
                event = events.get(stamp, {"split": 1, "distribution": 0, "known_on": stamp})
                bar = rows[stamp]
                writer.writerow((instrument, stamp, bar["close"], bar["volume"], event["split"],
                                 event["distribution"], event["known_on"]))
                counts["written"] += 1
                continue
            skipped[stamp] = {"code": reason, **({"source_reason": unresolved[stamp]} if stamp in unresolved else {})}
            counts[reason] += 1
        evidence[instrument] = {"counts": dict(counts), "skipped": skipped}
    return stream.getvalue().encode("utf-8"), evidence


def assemble(bars_path, bars_sha256, actions_path, actions_sha256, output):
    bars = read_pinned(bars_path, bars_sha256)
    actions = read_pinned(actions_path, actions_sha256)
    output = Path(output)
    normalized, coverage = join(bars, actions)
    receipt = {"schema": 1, "bars_sha256": bars_sha256, "actions_sha256": actions_sha256,
               "actions_source": actions["source"], "through": bars["window"]["end"],
               "joiner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
               "output_sha256": sha256(normalized).hexdigest(), "coverage": coverage}
    output.mkdir(exist_ok=False)
    (output / "daily_history.csv").write_bytes(normalized)
    (output / "daily_history_receipt.json").write_text(json.dumps(receipt, indent=2, allow_nan=False) + "\n",
                                                       encoding="utf-8")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bars", "actions", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("bars", "actions"):
        parser.add_argument(f"--{name}-sha256", required=True)
    args = parser.parse_args()
    assemble(args.bars, args.bars_sha256, args.actions, args.actions_sha256, args.output)


if __name__ == "__main__":
    main()
