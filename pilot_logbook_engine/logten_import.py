"""Safe, non-destructive parser for LogTen CSV/TSV exports.

This module only parses and classifies source rows. It deliberately does not
write to the canonical flights table; callers can stage its output for review.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Any
from logten_validation import default_field_mapping


@dataclass
class ParsedRow:
    source_row: int
    fingerprint: str
    raw: dict[str, str]
    classification: str
    warnings: list[str]


def _fingerprint(raw: dict[str, str]) -> str:
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_logten(content: str | bytes, filename: str = "upload") -> dict[str, Any]:
    """Parse a LogTen export, autodetecting comma/tab delimiters.

    Blank trailing lines are ignored. Original header names and cell strings
    are preserved; duplicate header names are made unique without losing data.
    """
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig")
    else:
        text = content.lstrip("\ufeff")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration:
        raise ValueError("The uploaded file is empty.")
    if not headers or not any(h.strip() for h in headers):
        raise ValueError("The uploaded file has no usable header row.")

    # Keep blank header columns too, but assign a stable name to each.
    names: list[str] = []
    seen: Counter[str] = Counter()
    for index, header in enumerate(headers, start=1):
        base = header.strip() or f"__blank_column_{index}"
        seen[base] += 1
        names.append(base if seen[base] == 1 else f"{base}__{seen[base]}")

    rows: list[ParsedRow] = []
    blank_rows = 0
    for source_row, values in enumerate(reader, start=2):
        if not any(str(v).strip() for v in values):
            blank_rows += 1
            continue
        # Retain all source cells, including any unexpected extra columns.
        padded = list(values[:len(names)]) + [""] * max(0, len(names) - len(values))
        raw = {names[i]: padded[i] for i in range(len(names))}
        for extra_index, value in enumerate(values[len(names):], start=1):
            raw[f"__extra_column_{extra_index}"] = value

        mapping = default_field_mapping(raw.keys())
        mapped = {
            target: (raw.get(column, "") or "").strip() if column else ""
            for target, column in mapping.items()
        }
        date_value = mapped.get("flight_date", "")
        departure = mapped.get("departure", "")
        arrival = mapped.get("arrival", "")
        aircraft_type = mapped.get("aircraft_type", "")
        remarks = mapped.get("remarks", "")
        warnings: list[str] = []

        is_sim = any(token in aircraft_type.upper() for token in ("SIM", "SIMULATOR"))
        if is_sim:
            classification = "simulator"
        elif not departure or not arrival:
            classification = "non_route"
            warnings.append("Missing departure or arrival location.")
        else:
            classification = "flight_candidate"

        if not date_value:
            warnings.append("Missing date.")
        if not mapped.get("total_time", ""):
            warnings.append("Missing total time.")
        if not mapped.get("out", "") and not mapped.get("in", ""):
            warnings.append("Missing OUT and IN times.")
        if remarks:
            # Remarks are retained verbatim; no semantic parsing is attempted.
            pass

        rows.append(ParsedRow(source_row, _fingerprint(raw), raw, classification, warnings))

    if not rows:
        raise ValueError("No non-empty records were found below the header.")
    return {
        "filename": filename,
        "delimiter": "tab" if delimiter == "\t" else "comma",
        "headers": names,
        "nonempty_records": len(rows),
        "blank_rows_skipped": blank_rows,
        "classification_counts": dict(Counter(r.classification for r in rows)),
        "warning_count": sum(bool(r.warnings) for r in rows),
        "rows": [asdict(r) for r in rows],
    }
