"""Frozen, pre-aligned daily history. Planning reads metadata, not series values."""

import csv
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re

from .stat_tools import Point, Window, check_sessions, guarded_rows


_FIELDS = {"schema_version", "classification", "source", "calendar", "file", "sha256", "sessions", "window"}
_WINDOW_FIELDS = {"start", "end", "as_of", "holdout_start"}
_CLASSES = {"public", "anonymized", "synthetic"}


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Frozen export exceeds the file size limit")
    return data


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Manifest contains duplicate JSON fields")
    return value


@dataclass(frozen=True, slots=True)
class FrozenHistory:
    folder: Path
    filename: str
    manifest_sha256: str
    data_sha256: str
    classification: str
    source: str
    calendar: str
    window: Window
    sessions: tuple[str, ...]

    @classmethod
    def open(cls, path: Path) -> "FrozenHistory":
        path = path.resolve(strict=True)
        if path.suffix.casefold() != ".json" or not path.is_file():
            raise ValueError("Historical manifest must be a JSON file")
        raw = _read(path, 128 * 1024)
        try:
            doc = json.loads(raw, object_pairs_hook=_unique_pairs)
        except (UnicodeError, RecursionError, ValueError):
            raise ValueError("Historical manifest is not valid unambiguous JSON") from None
        if (not isinstance(doc, dict) or set(doc) != _FIELDS
                or type(doc["schema_version"]) is not int or doc["schema_version"] != 1):
            raise ValueError("Unsupported historical manifest schema")
        if any(not isinstance(doc[key], str) for key in ("classification", "source", "calendar", "file", "sha256")):
            raise ValueError("Historical manifest labels and digest must be strings")
        if doc["classification"] not in _CLASSES or any(
            not 1 <= len(doc[key].strip()) <= 240 or not doc[key].isprintable() for key in ("source", "calendar")
        ):
            raise ValueError("Provide a public, anonymized or synthetic data classification and provenance")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.csv", doc["file"]) or not re.fullmatch(r"[0-9a-f]{64}", doc["sha256"]):
            raise ValueError("Expected a sibling CSV filename and a lowercase SHA-256")
        if (not isinstance(doc["sessions"], list) or any(not isinstance(day, str) for day in doc["sessions"])
                or not isinstance(doc["window"], dict) or set(doc["window"]) != _WINDOW_FIELDS
                or any(not isinstance(day, str) for day in doc["window"].values())):
            raise ValueError("Historical manifest requires an explicit session calendar and window")
        window = Window(**doc["window"])
        sessions = tuple(doc["sessions"])
        check_sessions(sessions, window)
        return cls(path.parent, doc["file"], sha256(raw).hexdigest(), doc["sha256"], doc["classification"],
                   doc["source"], doc["calendar"], window, sessions)

    def identity(self) -> dict:
        # Do not persist local paths or filenames in the research journal.
        return {"manifest_sha256": self.manifest_sha256, "data_sha256": self.data_sha256,
                "classification": self.classification, "source": self.source, "calendar": self.calendar,
                "window": asdict(self.window), "sessions": list(self.sessions)}

    def sample(self) -> tuple[Point, ...]:
        path = (self.folder / self.filename).resolve(strict=True)
        if path.parent != self.folder or path.suffix.casefold() != ".csv" or not path.is_file():
            raise ValueError("Historical CSV must stay beside its manifest")
        raw = _read(path, 512 * 1024)
        if sha256(raw).hexdigest() != self.data_sha256:
            raise ValueError("Historical CSV does not match its frozen SHA-256")
        try:
            reader = csv.reader(StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
            if next(reader, None) != ["day", "a", "b"]:
                raise ValueError
            rows = []
            for row in reader:
                if len(row) != 3 or len(rows) >= len(self.sessions):
                    raise ValueError
                day, a, b = row
                rows.append(Point(day, float(a), float(b)))
        except (UnicodeError, csv.Error, ValueError):
            raise ValueError("Expected a CSV containing only day,a,b in-sample observations") from None
        return guarded_rows(tuple(rows), self.window, sessions=self.sessions)
